import ctypes
import json
import logging
import os
import select
import shlex
import signal as _signal
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from synnodb.cpp_runner.hotpatch.cgroup import (
    RunnerCgroup,
    delegation_available,
    delegation_failure_reason,
    shared_parent_configured,
)
from synnodb.cpp_runner.hotpatch.db_launch import db_launch_binary
from synnodb.cpp_runner.utils.proc_utils import ProcTreeTimeoutKiller
from synnodb.tools.sandbox import _set_rlimits
from synnodb.tools.validate.query_validator_class import QueryResult

logger = logging.getLogger(__name__)

# Safety-net deadline for the build/load stages of a RUN. The `timeout` argument
# only bounds the query stage; a livelock in the build/loader (e.g. a buggy
# parallel sort in db_loader) never reaches the query stage, so without a build
# bound it runs unbounded. Sized generously above the one-time cold in-memory
# load (observed ~17 min at sf20; hotpatched rebuilds are seconds) so legitimate
# builds finish while pathological hangs are still killed.
BUILD_STAGE_TIMEOUT_S = 40 * 60

# prctl(PR_SET_PDEATHSIG, sig) asks the kernel to deliver `sig` to this process
# when its parent dies — even when the parent is SIGKILL'd, which bypasses
# atexit handlers. Without this, a hard-killed Python orchestrator leaves the
# C++ ./db tree orphaned and reparented to init.
_PR_SET_PDEATHSIG = 1
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _libc = None


def _install_pdeathsig_and_new_session() -> None:
    """Fallback launch path only: run in the child after fork() when the db_launch
    + cgroup path is not used. Ties the child's lifetime to the Python parent via
    PR_SET_PDEATHSIG (with the standard captured-PPID race check) and starts a new
    session so the tree can be reaped with killpg() as a last resort.

    The hardened, production parent-death handling lives in db_launch (the cgroup
    path); this is the best-effort fallback. It is NOT the complete A3 reaping
    treatment, but it fails closed: if PR_SET_PDEATHSIG cannot be armed (no libc, or
    prctl error), it refuses to launch rather than leave a child that would survive a
    SIGKILL'd orchestrator.
    """
    if _libc is None:
        # No libc/prctl: we cannot tie the child to the parent's death. Refuse to
        # launch rather than orphan it. Only reachable on a non-glibc host, where the
        # (glibc-targeted) engine cannot run anyway.
        raise OSError("libc unavailable; cannot arm PR_SET_PDEATHSIG")
    ppid_before = os.getppid()
    if _libc.prctl(_PR_SET_PDEATHSIG, _signal.SIGKILL, 0, 0, 0) != 0:
        # Could not arm the parent-death signal; refuse to launch a child that would
        # not be reaped if the orchestrator is SIGKILL'd.
        raise OSError("prctl(PR_SET_PDEATHSIG) failed")
    # If the parent died between getppid() and prctl(), the signal we armed references
    # a parent that is already gone; abort rather than run orphaned.
    if os.getppid() != ppid_before:
        raise OSError("parent exited during launch setup")
    try:
        os.setsid()
    except OSError:
        pass


def _signal_name(sig: int) -> str:
    """Return a descriptive name for a POSIX signal number, e.g. 'SIGSEGV'."""
    if sig <= 0:
        return ""
    try:
        return _signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


# Magic word and action codes for the framed binary control protocol.
# Must stay in sync with ipc::MESSAGE_MAGIC / ACTION_* constants in pipeline.hpp.
# Wire layout:
#   [uint32 magic][uint32 action][uint64 batch_id][uint32 line_count][uint32 env_count]
# followed by line_count × [uint32 len][utf-8 bytes]
# and env_count × key/value string pairs.
_MESSAGE_MAGIC = 0x31525043  # "CPR1" little-endian
_ACTION_RUN = 1
_ACTION_TERMINATE = 2


@dataclass
class HotpatchProcRunResult:
    response: str
    stdout: str
    stderr: str
    query_results: list[QueryResult]


class HotpatchProc:
    """Manages a long-lived child process (the "hotpatch" DB runner) that communicates
    over two custom file-descriptor pipes (p2c and c2p) in addition to the standard
    stdin/stdout/stderr streams.

    Communication protocol:
      - Parent → child control channel (p2c): plain text commands, e.g. "run\\n".
      - Child → parent result channel (c2p): the child writes a single newline-terminated
        response line when it has finished processing a command.
      - stdin: used to stream SQL/data lines into the child (see `send`).
      - stdout/stderr: captured as ordinary log/output text.
    """

    def __init__(
        self,
        command: str,
        *,
        echo_output: bool = False,
        cwd: Path,
        extra_env: Optional[Dict[str, str]] = None,
        memory_limit_bytes: Optional[int] = None,
        memory_max_bytes: Optional[int] = None,
        require_cgroup: bool = False,
    ) -> None:
        """
        Args:
            command: Shell command string (or path) used to launch the child process.
            echo_output: If True, mirror the child's stdout/stderr to the parent's own
                         stdout (fd 1) and stderr (fd 2) in real time.
            cwd: Working directory for the child process.
            extra_env: Additional environment variables merged on top of os.environ
                       before being passed to the child.
            memory_limit_bytes: Virtual-memory ceiling enforced via RLIMIT_AS.
                                 If None, RLIMIT_AS is not set and the child
                                 inherits the parent's (typically unlimited) limit.
            memory_max_bytes: Hard resident-memory ceiling enforced via a per-runner
                              cgroup v2 ``memory.max``. When set, the engine is
                              launched through ``db_launch`` into a dedicated cgroup,
                              so a breach is OOM-killed as a group without touching
                              the host. When None, no cgroup is used.
            require_cgroup: If True and cgroup delegation is unavailable, refuse to
                            launch (fail closed) rather than silently dropping to
                            RLIMIT_AS only. Production large-memory runs set this.
        """
        self._command = command
        self._echo_output = echo_output
        self._cwd = cwd
        self._extra_env = extra_env or {}
        self._memory_limit_bytes = memory_limit_bytes
        self._memory_max_bytes = memory_max_bytes
        self._require_cgroup = require_cgroup

        # Per-runner cgroup for the current child (None when not using the cgroup
        # path). Created in _start(), removed in _clear_proc_state().
        self._cgroup: Optional[RunnerCgroup] = None
        self._launch_counter = 0

        # subprocess.Popen handle; None when the child is not running.
        self._proc: subprocess.Popen[bytes] | None = None

        # Write end of the parent→child control pipe (p2c).
        # The child inherits the read end (p2c_r) and reads commands from it.
        self._p2c_w: int | None = None

        # File object wrapping the read end of the child→parent result pipe (c2p).
        self._c2p_file = None
        # Raw fd number for the c2p read end — kept separately for use with select().
        self._c2p_r: int | None = None

        # Raw fd numbers for the child's stdout and stderr pipes (non-blocking).
        self._stdout_fd: int | None = None
        self._stderr_fd: int | None = None

        # Buffered stdin pipe connected to the child's stdin.
        self._stdin = None

        # Wall-clock time (ms) of the most recent ingest operation; -1 = never run.
        self.last_ingest_time_ms: float = -1
        self._next_batch_id: int = 1

    def _clear_proc_state(self) -> None:
        """Close all open file descriptors and reset instance state after the child exits."""
        # Close the write end of the parent→child control pipe.
        if self._p2c_w is not None:
            try:
                os.close(self._p2c_w)
            except OSError:
                pass
            self._p2c_w = None

        # Close the child→parent result pipe (file-object wrapper).
        if self._c2p_file is not None:
            try:
                self._c2p_file.close()
            except Exception:
                pass
            self._c2p_file = None
        self._c2p_r = None  # fd was owned by _c2p_file; already closed above

        # Close the buffered stdin pipe to the child.
        if self._stdin is not None:
            try:
                self._stdin.close()
            except Exception:
                pass
            self._stdin = None

        # Close the subprocess's stdout/stderr pipe handles.
        if self._proc is not None:
            if self._proc.stdout is not None:
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
            if self._proc.stderr is not None:
                try:
                    self._proc.stderr.close()
                except Exception:
                    pass
        self._stdout_fd = None
        self._stderr_fd = None
        self._proc = None

        # Remove the per-runner cgroup, if any. The child is reaped by the time this
        # runs, so the cgroup is empty and rmdir succeeds; remove() is best-effort.
        if self._cgroup is not None:
            self._cgroup.remove()
            self._cgroup = None

    def is_running(self) -> bool:
        """True when the managed child process is currently alive. After a ``run()`` this
        distinguishes a healthy warm runner (child still resident, ready for the next batch)
        from one whose child exited mid-batch (a loader/builder crash)."""
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        """Launch the child process if it is not already running.

        If a previous instance exited, its state is cleaned up before a new one
        is created.  If the process is still alive, this is a no-op.

        Pipe topology after _start():

            parent                       child
            ──────                       ─────
            p2c_w  ──(p2c pipe)──►  p2c_r   (env var P2C_FD)
            c2p_r  ◄─(c2p pipe)──   c2p_w   (env var C2P_FD)
            stdin  ──(pipe)──────►  stdin
            stdout ◄─(pipe)──────   stdout
            stderr ◄─(pipe)──────   stderr
        """
        if self._proc is not None:
            if self._proc.poll() is None:
                # Child is still alive — nothing to do.
                return
            self._clear_proc_state()

        # AddressSanitizer reserves a huge virtual address space (its shadow memory,
        # ~20 TB), so an RLIMIT_AS cap would kill the child at startup. When the sanitize
        # build profile is active, skip the virtual-memory cap.
        sanitize_active = bool(os.environ.get("SYNNO_SANITIZE", "").strip())
        as_bytes = None if sanitize_active else self._memory_limit_bytes

        # Decide the launch mechanism before allocating any resources, so a
        # fail-closed production run aborts cleanly. Exactly one mechanism runs:
        #   - cgroup path: launch via db_launch, which joins a per-runner cgroup
        #     (hard memory.max) and owns pdeathsig/setsid/RLIMIT_AS in compiled code;
        #   - fallback path: the in-Python _preexec, used only when no cgroup ceiling
        #     is requested or delegation is unavailable in a dev/test run.
        use_cgroup = self._memory_max_bytes is not None and delegation_available()
        if self._memory_max_bytes is not None and not use_cgroup:
            # A configured shared parent (SYNNO_CGROUP_PARENT) makes the cgroup ceiling
            # mandatory regardless of require_cgroup: the operator explicitly demanded the
            # aggregate slice, so a setup failure must raise, never silently fall back to
            # RLIMIT_AS (which defeats the aggregate guarantee).
            if self._require_cgroup or shared_parent_configured():
                reason = delegation_failure_reason()
                detail = f" ({reason})" if reason else ""
                raise RuntimeError(
                    f"cgroup v2 memory ceiling is required but unavailable{detail}; "
                    "refusing to launch a large-memory run without a hard memory ceiling. "
                    "Set up cgroup delegation (e.g. a systemd unit with Delegate=yes), fix "
                    "the configured SYNNO_CGROUP_PARENT slice, or clear it / require_cgroup "
                    "for dev/test."
                )
            logger.warning(
                "cgroup v2 delegation unavailable; falling back to RLIMIT_AS only "
                "(no hard RSS ceiling). Acceptable for dev/test, not production."
            )

        # Build the base argv list from the command string.
        if isinstance(self._command, str):
            cmd = self._command.strip()
            cmd = cmd if cmd else "./db"
            argv = shlex.split(cmd)
            if not argv:
                argv = ["./db"]
        else:
            argv = [str(self._command)]

        def _preexec():
            # Fallback path only: applied in the child after fork() before exec().
            # Sets RLIMIT_AS, ties the child's lifetime to this process
            # (PR_SET_PDEATHSIG), and starts a new session for killpg() reaping.
            if as_bytes is not None:
                _set_rlimits(
                    cpu_seconds=None,
                    as_bytes=as_bytes,
                    fsize_bytes=None,
                    nofile=None,
                    nproc=None,
                )
            _install_pdeathsig_and_new_session()

        # Create the two custom control pipes.
        # p2c (parent-to-child): parent writes commands; child reads them.
        p2c_r, p2c_w = os.pipe()
        # c2p (child-to-parent): child writes response lines; parent reads them.
        c2p_r, c2p_w = os.pipe()

        # Everything that allocates an OS resource (the runner cgroup and the child
        # process) is created under one guard: if any step raises, the cgroup and the
        # four pipe fds we own are reclaimed before re-raising, so a failed launch
        # leaks nothing. (self._proc stays None on failure, so _clear_proc_state would
        # otherwise never run.) db_launch execs ./db in place, so Popen still sees the
        # engine as its direct child and the pass_fds control pipes survive unchanged.
        try:
            if use_cgroup:
                self._launch_counter += 1
                self._cgroup = RunnerCgroup.create(
                    self._memory_max_bytes,
                    name=f"{os.getpid()}-{id(self) & 0xFFFF:x}-{self._launch_counter}",
                )
                prefix = [str(db_launch_binary()), "--cgroup", self._cgroup.procs_dir]
                if as_bytes is not None:
                    prefix += ["--as-limit", str(as_bytes)]
                argv = [*prefix, "--", *argv]
                preexec = None  # db_launch owns the per-process setup
            else:
                preexec = _preexec

            self._proc = subprocess.Popen(
                argv,
                # Explicitly pass the custom pipe fds so they survive the exec().
                pass_fds=(p2c_r, c2p_w),
                preexec_fn=preexec,
                env={
                    **os.environ,
                    # Tell the child which fd numbers to use for the control pipes.
                    "P2C_FD": str(p2c_r),
                    "C2P_FD": str(c2p_w),
                    **self._extra_env,
                },
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,  # Raw bytes — we decode manually with error handling.
            )
        except BaseException:
            if self._cgroup is not None:
                self._cgroup.remove()
                self._cgroup = None
            for fd in (p2c_r, p2c_w, c2p_r, c2p_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

        # Close the child-side ends in the parent process; they are only needed
        # inside the child and keeping them open here would prevent EOF detection.
        os.close(p2c_r)
        os.close(c2p_w)

        # Retain the parent-side ends.
        self._p2c_w = p2c_w  # parent writes "run\n" etc. here
        self._c2p_r = c2p_r  # parent reads the child's single-line response here
        # Make c2p non-blocking so select() can drive it without hanging.
        os.set_blocking(c2p_r, False)
        self._c2p_file = os.fdopen(c2p_r, "rb", buffering=0)

        self._stdin = self._proc.stdin  # buffered write end of the stdin pipe

        # Store raw fd numbers for stdout/stderr so select() can wait on them.
        if self._proc.stdout is not None:
            self._stdout_fd = self._proc.stdout.fileno()
            os.set_blocking(self._stdout_fd, False)
        if self._proc.stderr is not None:
            self._stderr_fd = self._proc.stderr.fileno()
            os.set_blocking(self._stderr_fd, False)

    def _write_control_message(
        self,
        action: int,
        batch_id: int = 0,
        query_lines: list[str] | None = None,
        run_env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Write a framed control message to the p2c pipe.

        Replaces the old plain-text "run\\n" / "stop\\n" writes that could leave
        stale bytes in the kernel pipe buffer and be consumed by the next call.
        query_lines and run_env are bundled atomically with the RUN action so
        they are always associated with the correct invocation.
        """
        if self._p2c_w is None:
            raise RuntimeError("control pipe not available")

        def write_all(data: bytes) -> None:
            assert self._p2c_w is not None
            view = memoryview(data)
            while view:
                written = os.write(self._p2c_w, view)
                if written == 0:
                    raise RuntimeError("failed to write control message")
                view = view[written:]

        lines = query_lines or []
        env_items = list((run_env or {}).items())
        if action != _ACTION_RUN:
            lines = []
            env_items = []
        # Header: magic(u32) action(u32) batch_id(u64) line_count(u32)
        # env_count(u32) — little-endian.
        header = struct.pack(
            "<IIQII",
            _MESSAGE_MAGIC,
            action,
            batch_id,
            len(lines),
            len(env_items),
        )
        write_all(header)
        for line in lines:
            encoded = line.encode("utf-8")
            write_all(struct.pack("<I", len(encoded)))
            write_all(encoded)
        for key, value in env_items:
            for field in (key, value):
                encoded = str(field).encode("utf-8")
                write_all(struct.pack("<I", len(encoded)))
                write_all(encoded)

    def run(
        self,
        timeout: int = 0,
        query_lines: list[str] | None = None,
        run_env: Optional[Dict[str, str]] = None,
        echo_output: Optional[bool] = None,
    ) -> HotpatchProcRunResult:
        """Send a "run" command to the child and collect its response.

        Starts the child if necessary, writes a framed RUN message containing
        `query_lines` to the control pipe, then loops with select() until the
        child writes a newline-terminated JSON response line back on the c2p
        pipe (or until the pipe closes, indicating the child exited).

        Args:
            timeout: Seconds before the child process tree is forcibly killed.
                     0 means no timeout.
            run_env: Environment variables to apply inside the query stage for
                     this specific invocation.

        Returns:
            A HotpatchProcRunResult:
              - response:      The raw JSON line the child wrote to c2p, or an
                               error/timeout message if the child died unexpectedly.
              - stdout:        Everything the child wrote to stdout during this call.
              - stderr:        Everything the child wrote to stderr, plus any exit-code
                               or OOM annotations appended by this method.
              - query_results: Raw list of per-query dicts with "trace" and
                               "elapsed_ms" keys, or [] when not in trace mode.
        """
        self._start()
        if self._p2c_w is None or self._c2p_file is None or self._c2p_r is None:
            raise RuntimeError("runner not initialized")

        # Signal the child to start processing this exact batch.
        batch_id = self._next_batch_id
        self._next_batch_id += 1
        self._write_control_message(
            _ACTION_RUN,
            batch_id=batch_id,
            query_lines=query_lines or [],
            run_env=run_env,
        )

        # Accumulation buffers for the three output streams.
        out_buf = bytearray()  # child's stdout
        err_buf = bytearray()  # child's stderr (+ injected error messages)
        resp_buf = bytearray()  # child's c2p response (single line expected)

        # Optional watchdog for the query stage.  The process tree is:
        # db -> loader stage -> builder stage -> query stage.  Starting the
        # timer only once that leaf exists prevents a query timeout from
        # killing a long-running/reloading builder before the query starts.
        killer = (
            ProcTreeTimeoutKiller(
                self._proc.pid,
                timeout,
                min_descendant_depth=3,
                build_timeout=BUILD_STAGE_TIMEOUT_S,
            )
            if (timeout > 0 or BUILD_STAGE_TIMEOUT_S > 0) and self._proc is not None
            else None
        )

        while True:
            # Build the fd list to watch: always the c2p result pipe, plus stdout/stderr.
            fds = [self._c2p_r]
            if self._stdout_fd is not None:
                fds.append(self._stdout_fd)
            if self._stderr_fd is not None:
                fds.append(self._stderr_fd)

            # Poll at most every 1 s when a killer is active so we can call enforce()
            # promptly; otherwise block indefinitely until data arrives.
            select_timeout = 1.0 if killer is not None else None
            rlist, _, _ = select.select(fds, [], [], select_timeout)

            # Let the killer check whether the deadline has passed and kill if needed.
            if killer is not None:
                killer.enforce()

            try:
                for fd in rlist:
                    if fd == self._c2p_r:
                        # Data on the child→parent result pipe.
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            # EOF on c2p means the child closed the pipe (i.e. exited).
                            rc = self._proc.wait() if self._proc is not None else None
                            if rc is not None:
                                if rc < 0 and (
                                    self._memory_limit_bytes is not None
                                    or self._memory_max_bytes is not None
                                ):
                                    # Drain stderr/stdout so the actual crash message is visible.
                                    while self._stdout_fd is not None:
                                        more = os.read(self._stdout_fd, 4096)
                                        if not more:
                                            break
                                        out_buf.extend(more)
                                    while self._stderr_fd is not None:
                                        more = os.read(self._stderr_fd, 4096)
                                        if not more:
                                            break
                                        err_buf.extend(more)
                                    sig = -rc
                                    stderr_snippet = err_buf.decode(
                                        "utf-8", errors="replace"
                                    ).strip()
                                    # If the C++ side surfaced a memory-budget
                                    # related failure (mmap ENOMEM, BufferPool
                                    # error, std::bad_alloc), label it
                                    # explicitly — these are typically RLIMIT_AS
                                    # rejections, not arbitrary aborts.
                                    budget_markers = (
                                        "ENOMEM",
                                        "RLIMIT_AS",
                                        "BufferPool::mmap_col",
                                        "std::bad_alloc",
                                        "Cannot allocate memory",
                                    )
                                    budget_hit = any(
                                        m in stderr_snippet for m in budget_markers
                                    )
                                    limit_mb = (
                                        self._memory_limit_bytes // 1024 // 1024
                                        if self._memory_limit_bytes is not None
                                        else None
                                    )
                                    # On the cgroup path, memory.events is the
                                    # authoritative OOM signal (read before the cgroup
                                    # is torn down in _clear_proc_state).
                                    cg_events = (
                                        self._cgroup.memory_events()
                                        if self._cgroup is not None
                                        else {}
                                    )
                                    cgroup_oom = cg_events.get("oom_kill", 0) > 0
                                    max_mb = (
                                        self._memory_max_bytes // 1024 // 1024
                                        if self._memory_max_bytes is not None
                                        else None
                                    )
                                    # Signal 9 = SIGKILL (OOM killer); signal 6 = SIGABRT
                                    # (std::terminate / abort inside the process).
                                    if cgroup_oom:
                                        cause = (
                                            f"cgroup OOM (memory.max={max_mb} MB, "
                                            f"oom_kill={cg_events.get('oom_kill', 0)}, "
                                            f"oom_group_kill={cg_events.get('oom_group_kill', 0)})"
                                        )
                                    elif sig == 9:
                                        cause = f"likely OOM, limit={limit_mb} MB"
                                    elif budget_hit:
                                        cause = (
                                            f"likely memory budget exceeded "
                                            f"(RLIMIT_AS={limit_mb} MB)"
                                        )
                                    elif sig == 6:
                                        cause = "SIGABRT — process called abort() or std::terminate()"
                                    else:
                                        cause = f"signal {sig}"
                                    detail = (
                                        f"\nSTDERR:\n{stderr_snippet}"
                                        if stderr_snippet
                                        else ""
                                    )
                                    raise MemoryError(
                                        f"hotpatch process killed by signal {sig} ({cause}){detail}"
                                    )
                                err_buf.extend(
                                    f"process exited with code {rc}\n".encode("utf-8")
                                )
                            # Drain any remaining stdout/stderr before returning.
                            while self._stdout_fd is not None:
                                more = os.read(self._stdout_fd, 4096)
                                if not more:
                                    break
                                out_buf.extend(more)
                            while self._stderr_fd is not None:
                                more = os.read(self._stderr_fd, 4096)
                                if not more:
                                    break
                                err_buf.extend(more)
                            response = resp_buf.decode("utf-8", errors="replace")
                            out = out_buf.decode("utf-8", errors="replace")
                            err = err_buf.decode("utf-8", errors="replace")
                            if killer is not None and killer.killed:
                                response = f"{response}\nTerminated after {timeout} seconds due to timeout."
                            # The child has exited (c2p EOF). Reclaim its cgroup, pipes
                            # and proc state now rather than leaving a dead runner (and
                            # an empty cgroup) cached until the next launch.
                            self._clear_proc_state()
                            return HotpatchProcRunResult(
                                response=response,
                                stdout=out,
                                stderr=err,
                                query_results=[],
                            )
                        resp_buf.extend(chunk)

                    elif fd == self._stdout_fd:
                        # Data on the child's stdout pipe.
                        chunk = os.read(fd, 4096)
                        if chunk:
                            out_buf.extend(chunk)
                            # first check the per-call echo_output override, then fall back to the global setting if not set
                            if (echo_output is not None and echo_output) or (
                                echo_output is None and self._echo_output
                            ):
                                os.write(1, chunk)  # mirror to parent's stdout (fd 1)

                    elif fd == self._stderr_fd:
                        # Data on the child's stderr pipe.
                        chunk = os.read(fd, 4096)
                        if chunk:
                            err_buf.extend(chunk)
                            # first check the per-call echo_output override, then fall back to the global setting if not set
                            if (echo_output is not None and echo_output) or (
                                echo_output is None and self._echo_output
                            ):
                                os.write(2, chunk)  # mirror to parent's stderr (fd 2)

            except MemoryError as e:
                # Return OOM (or similar signal-kill) error as the response string.
                response = str(e)
                out = out_buf.decode("utf-8", errors="replace")
                err = err_buf.decode("utf-8", errors="replace")
                # The child was signal-killed (cgroup OOM / abort); memory.events was
                # already read into `response` above, so reclaim the cgroup now.
                self._clear_proc_state()
                return HotpatchProcRunResult(
                    response=response, stdout=out, stderr=err, query_results=[]
                )

            # Happy path: the child wrote a complete response line ending with "\n".
            if b"\n" in resp_buf:
                # Take only the first line; ignore any trailing data (shouldn't happen).
                line, _, _ = resp_buf.partition(b"\n")
                response = line.decode("utf-8", errors="replace")
                out = out_buf.decode("utf-8", errors="replace")
                err = err_buf.decode("utf-8", errors="replace")
                if killer is not None and killer.killed:
                    response = f"{response}\nTerminated after {timeout} seconds due to timeout."
                # Parse the JSON response to extract per-query trace data.
                query_results: list[QueryResult] = []
                try:
                    # load payload and parse response
                    payload = json.loads(response)
                    # Verify the child echoed back the same batch_id we sent.
                    # A mismatch would mean we read a stale response from a
                    # previous run that was still sitting in the c2p pipe buffer.
                    returned_batch_id = payload.get("batch_id")
                    if returned_batch_id != batch_id:
                        return HotpatchProcRunResult(
                            response=(
                                "batch id mismatch: "
                                f"sent {batch_id}, got {returned_batch_id}"
                            ),
                            stdout=out,
                            stderr=err,
                            query_results=[],
                        )
                    query_results = [
                        QueryResult(
                            trace=qr.get("trace", "missing"),
                            elapsed_ms=qr.get("elapsed_ms", -1),
                            error=qr.get("error", "missing"),
                            query_id=qr.get("query_id", "missing"),
                            req_id=qr.get("req_id", "missing"),
                        )
                        for qr in payload.get("query_results", [])
                    ]
                    sig = int(payload.get("signal", 0) or 0)
                    sig_label = f"{sig} ({_signal_name(sig)})" if sig else "0"
                    response = f"exit_code: {payload['exit_code']} signal: {sig_label}"
                    # Stage-level error is set when api.query() threw (caught
                    # by the outer try/catch in db.cpp's query stage lambda)
                    # OR synthesised from term_signal when the child was
                    # killed by a signal (segfault, abort, OOM-kill, etc.).
                    stage_error = payload.get("stage_error", "") or ""
                    if stage_error:
                        response = f"ERROR: {stage_error}\n{response}"

                    per_query_errors = [qr.error for qr in query_results if qr.error]
                    if per_query_errors:
                        response = (
                            response
                            + "\nPer-query errors:\n  "
                            + "\n  ".join(per_query_errors)
                        )
                except (json.JSONDecodeError, KeyError):
                    pass

                return HotpatchProcRunResult(
                    response=response,
                    stdout=out,
                    stderr=err,
                    query_results=query_results,
                )

    def send(self, line: str) -> None:
        """Write a single line to the child's stdin.

        Starts the child if necessary.  The newline is appended automatically.
        Typically used to stream SQL statements or data rows into the child before
        calling `run()`.
        """
        self._start()
        if self._stdin is None:
            raise RuntimeError("stdin not available")
        self._stdin.write((line + "\n").encode("utf-8"))
        self._stdin.flush()

    def close_stdin(self) -> None:
        """Close the stdin pipe to the child, signalling EOF on its stdin."""
        if self._stdin is not None:
            self._stdin.close()
            self._stdin = None

    def terminate(self) -> None:
        """Gracefully stop the child process.

        Sends a framed TERMINATE message over the control pipe so the child can
        shut down cleanly, then waits for it to exit. If the child does not
        exit within a short grace period — e.g. because it is wedged inside
        a blocking syscall like sync() — escalate to SIGKILL on the entire
        process group so the atexit path cannot hang indefinitely.

        Raises RuntimeError if the child exits with a non-zero return code that
        is not the result of our own SIGKILL escalation.
        """
        if self._proc is None:
            return
        if self._p2c_w is not None:
            try:
                self._write_control_message(_ACTION_TERMINATE)
            except Exception:
                pass
        killed_by_us = False
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            killed_by_us = True
            logger.warning(
                "HotpatchProc child pid=%s did not exit after TERMINATE; "
                "escalating to SIGKILL on its process group",
                self._proc.pid,
            )
            try:
                os.killpg(os.getpgid(self._proc.pid), _signal.SIGKILL)
            except OSError:
                # Fall back to per-pid kill if the group is gone or we lack
                # permission for whatever reason.
                self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error(
                    "HotpatchProc child pid=%s still alive after SIGKILL; giving up",
                    self._proc.pid,
                )
        returncode = self._proc.returncode
        self._clear_proc_state()
        if returncode not in (0, None) and not killed_by_us:
            raise RuntimeError(f"process exited with code {returncode}")
