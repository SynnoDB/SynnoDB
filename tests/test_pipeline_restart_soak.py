"""A1 soak test: restarting the builder process on a source change bounds memory.

The hotpatch pipeline kept a long-lived builder that rebuilt its dataset in place
on every libbuilder.so change. Freed memory is never returned to the OS, so RSS
ratcheted one dataset copy per reload (the 480 GB SF50 incident). The fix makes
the loader restart the builder PROCESS on a build-id change; a fresh process is
the only thing that reclaims the old heap.

This drives a synthetic loader -> builder -> query engine (tests/soak_engine, built
straight on pipeline.hpp) over the real framed control protocol and pins the
production invariants:

  * test_restart_on_change_bounds_memory (the fix): on each builder-source change
    the builder PID changes, the old builder exits, and the engine's resident
    memory (summed PSS over the process tree) stays bounded.
  * test_in_place_reload_ratchets_memory (the pre-fix path / reproduction): with
    the restart policy off - exactly today's behavior on main - the builder PID is
    constant and PSS ratchets one copy per reload.

Both modes run the same binary, toggled by one bool, so the second test is the
negative control that proves the first is actually measuring the fix. Memory is
measured via PSS rather than RSS because RSS double-counts the copy-on-write
Arrow-input pages shared between loader and builder.
"""

import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Wire format mirrors ipc::MESSAGE_MAGIC / ACTION_RUN and DoneToken in pipeline.hpp.
_MAGIC = 0x31525043
_ACTION_RUN = 1
_DONE_TOKEN_BYTES = 8  # struct DoneToken { int exit_code; int term_signal; }

_SOAK_DIR = Path(__file__).parent / "soak_engine"
_HOTPATCH_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "synnodb" / "cpp_runner" / "hotpatch"
)

_HOG_MB = 24
_INPUT_MB = 8
_FLIPS = 20  # >= 20 builder-source changes, per the A1 verification plan
_RUNS = _FLIPS + 1

_CXX = os.environ.get("CXX", "g++")

pytestmark = pytest.mark.skipif(
    shutil.which(_CXX) is None
    or not sys.platform.startswith("linux")
    or not Path("/proc/self/smaps_rollup").exists(),
    reason="needs a C++ compiler and Linux /proc PSS accounting",
)


def _compile(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"compile failed: {' '.join(args)}\n{proc.stderr}")


def _build_builder_so(build_dir: Path, build_id_hex: str) -> None:
    """(Re)build libbuilder_soak.so with an explicit GNU build-id, atomically.

    A distinct --build-id is exactly what a no-op recompile of the builder source
    produces (the linker re-hashes), so flipping it is a faithful stand-in for the
    agent editing and recompiling db_loader.cpp.
    """
    tmp = build_dir / "libbuilder_soak.so.tmp"
    _compile(
        [
            _CXX,
            "-O2",
            "-std=c++20",
            "-shared",
            "-fPIC",
            "-I",
            str(_SOAK_DIR),
            f"-Wl,--build-id=0x{build_id_hex}",
            "-o",
            str(tmp),
            str(_SOAK_DIR / "builder_soak.cpp"),
        ]
    )
    os.replace(tmp, build_dir / "libbuilder_soak.so")


@pytest.fixture(scope="module")
def soak_build(tmp_path_factory) -> Path:
    """Compile the synthetic host and plugins once into a private build dir."""
    build_dir = tmp_path_factory.mktemp("soak_engine")
    _compile(
        [
            _CXX,
            "-O2",
            "-std=c++20",
            "-I",
            str(_HOTPATCH_DIR),
            "-o",
            str(build_dir / "db_soak_host"),
            str(_SOAK_DIR / "host_soak.cpp"),
            str(_HOTPATCH_DIR / "build_id.cpp"),
            "-ldl",
        ]
    )
    for name in ("loader", "query"):
        _compile(
            [
                _CXX,
                "-O2",
                "-std=c++20",
                "-shared",
                "-fPIC",
                "-I",
                str(_SOAK_DIR),
                "-Wl,--build-id",
                "-o",
                str(build_dir / f"lib{name}_soak.so"),
                str(_SOAK_DIR / f"{name}_soak.cpp"),
            ]
        )
    _build_builder_so(build_dir, f"{0:040x}")
    return build_dir


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            data = Path(f"/proc/{entry}/stat").read_text()
            # comm (field 2) may contain spaces/parens; ppid is the field after ')'.
            ppid = int(data[data.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _subtree_pss_kb(root: int) -> int:
    total = 0
    for pid in _descendants(root):
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
                    break
        except OSError:
            continue  # process exited mid-walk; its pages are already reclaimed
    return total


def _send_run(write_fd: int, batch_id: int) -> None:
    # magic(u32) action(u32) batch_id(u64) line_count(u32) env_count(u32); no payload.
    os.write(write_fd, struct.pack("<IIQII", _MAGIC, _ACTION_RUN, batch_id, 0, 0))


def _read_done(read_fd: int, timeout: float = 30.0) -> None:
    buf = b""
    while len(buf) < _DONE_TOKEN_BYTES:
        ready, _, _ = select.select([read_fd], [], [], timeout)
        if not ready:
            raise TimeoutError("engine did not emit a done token")
        chunk = os.read(read_fd, _DONE_TOKEN_BYTES - len(buf))
        if not chunk:
            raise EOFError("done pipe closed before a full token")
        buf += chunk


def _drive(build_dir: Path, restart: bool):
    """Launch the engine and drive _RUNS runs, flipping libbuilder.so each run
    after the first. Returns (builder_pids, pss_kb, old_pid_dead) per run."""
    _build_builder_so(build_dir, f"{0:040x}")
    pid_file = build_dir / "builder.pid"
    p2c_r, p2c_w = os.pipe()
    c2p_r, c2p_w = os.pipe()
    env = dict(
        os.environ,
        SOAK_READ_FD=str(p2c_r),
        SOAK_DONE_FD=str(c2p_w),
        SOAK_PID_FILE=str(pid_file),
        SOAK_HOG_MB=str(_HOG_MB),
        SOAK_INPUT_MB=str(_INPUT_MB),
    )
    if restart:
        env["SOAK_RESTART"] = "1"
    proc = subprocess.Popen(
        [str(build_dir / "db_soak_host")],
        cwd=str(build_dir),
        env=env,
        pass_fds=(p2c_r, c2p_w),
        stderr=subprocess.DEVNULL,
    )
    os.close(p2c_r)
    os.close(c2p_w)

    builder_pids: list[int] = []
    pss_kb: list[int] = []
    old_pid_dead: list[bool] = []
    try:
        for i in range(_RUNS):
            if i > 0:
                _build_builder_so(build_dir, f"{i:040x}")
            _send_run(p2c_w, i + 1)
            _read_done(c2p_r)
            builder_pids.append(int(pid_file.read_text()))
            # The loader reaps the old builder before the run completes; allow a
            # brief settle against scheduling jitter before asserting it is gone.
            if i > 0:
                prev = builder_pids[i - 1]
                dead = prev != builder_pids[i]
                for _ in range(50):
                    if not _pid_alive(prev) or not dead:
                        break
                    time.sleep(0.02)
                old_pid_dead.append(prev != builder_pids[i] and not _pid_alive(prev))
            pss_kb.append(_subtree_pss_kb(proc.pid))
    finally:
        os.close(p2c_w)
        os.close(c2p_r)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    return builder_pids, pss_kb, old_pid_dead


def test_restart_on_change_bounds_memory(soak_build):
    """The fix: every builder-source change restarts the builder process, the old
    one exits, and resident memory stays bounded at ~one dataset copy."""
    pids, pss_kb, old_pid_dead = _drive(soak_build, restart=True)

    # Each source change forks a fresh builder, so consecutive runs differ.
    for i in range(1, _RUNS):
        assert pids[i] != pids[i - 1], (i, pids)
    # The replaced builders have exited (no accumulation of live builders).
    assert all(old_pid_dead), old_pid_dead

    # Resident memory stays at ~input + one builder copy, never ratcheting.
    bound_kb = (_INPUT_MB + 2 * _HOG_MB + 64) * 1024
    assert max(pss_kb) < bound_kb, (max(pss_kb), bound_kb, pss_kb)


def test_in_place_reload_ratchets_memory(soak_build):
    """Reproduction / negative control: with the restart policy off (today's
    behavior on main), the builder reloads in place and PSS ratchets one copy per
    source change. This is the failure the fix above prevents."""
    pids, pss_kb, _ = _drive(soak_build, restart=False)

    # In-place reload keeps the single long-lived builder process.
    assert len(set(pids)) == 1, pids
    # Memory grows by roughly one builder copy per reload - the ratchet.
    growth_kb = pss_kb[-1] - pss_kb[0]
    assert growth_kb > (_FLIPS // 2) * _HOG_MB * 1024, (growth_kb, pss_kb)
