import asyncio
import logging
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from agents import (
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
    custom_span,
)

from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.sandbox import SandboxConfig, sandbox_shell_async
from synnodb.utils import utils

logger = logging.getLogger(__name__)


class ShellCacheType:
    def __init__(
        self,
        outputs: list[ShellCommandOutput],
        hash_payload: str,
        runtime_seconds: float,
        snapshot_hash: str,
    ):
        self.outputs = outputs
        self.hash_payload = hash_payload
        self.runtime_seconds = runtime_seconds
        self.snapshot_hash = snapshot_hash


class ShellExecutor:
    """Executes shell commands with optional approval."""

    def __init__(
        self,
        cwd: Path,
        snapshotter: GitSnapshotter,
        cache_dir: Path,
        do_not_cache: bool,
        only_from_cache: bool,
        run_stats_collector: RunStatsCollector,
        untracked_cpp_runner_content: str,
        runtime_tracker: Optional[RuntimeTracker] = None,
        default_timeout_ms: Optional[int] = 2 * 60 * 1000,
        shell_output_limit: Optional[
            int
        ] = 150000,  # will return eror if output exceeds this limit (not truncating, instead return only error)
        readonly_files: set[str] = set(),
    ) -> None:
        if default_timeout_ms is not None and default_timeout_ms < 0:
            raise ValueError("default_timeout_ms must be non-negative")

        self.cwd = cwd
        self.snapshotter = snapshotter
        self.cache_dir = cache_dir
        self.do_not_cache = do_not_cache
        self.run_stats_collector = run_stats_collector
        self.runtime_tracker = runtime_tracker
        self.shell_output_limit = shell_output_limit
        self.default_timeout_ms = default_timeout_ms
        self.readonly_files = [self.cwd / f for f in readonly_files]
        self.only_from_cache = only_from_cache
        self.untracked_cpp_runner_content = untracked_cpp_runner_content

        utils.create_dir_and_set_permissions(self.cache_dir)

    def _cache_path_for(self, hash: str) -> Path:
        return self.cache_dir / f"{hash}.pkl"

    def _timeout_ms_for(self, request: ShellCommandRequest) -> Optional[int]:
        timeout_ms = request.data.action.timeout_ms
        if timeout_ms is None:
            timeout_ms = self.default_timeout_ms
        return timeout_ms

    def _timeout_seconds_for(self, request: ShellCommandRequest) -> Optional[float]:
        timeout_ms = self._timeout_ms_for(request)
        return (timeout_ms or 0) / 1000 or None

    def _kill_timed_out_process(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                return

    async def _get_outputs(
        self, request: ShellCommandRequest
    ) -> list[ShellCommandOutput]:
        action = request.data.action
        timeout = self._timeout_seconds_for(request)
        try:
            await self.require_approval(action.commands)
        except SudoInShellError as e:
            return [
                ShellCommandOutput(
                    command=cmd,
                    stdout="",
                    stderr=str(e),
                    outcome=ShellCallOutcome(type="exit", exit_code=None),
                )
                for cmd in action.commands
            ]

        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            cfg = SandboxConfig(
                writable_roots=[str(self.cwd), "/tmp"],
                cwd=str(self.cwd),
                cpu_seconds=None,
                nproc=None,
                readonly_files=list(self.readonly_files),
            )
            timed_out = False
            async with sandbox_shell_async(
                command,
                cfg=cfg,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ) as proc:
                communicate_task = asyncio.create_task(proc.communicate())
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        asyncio.shield(communicate_task), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.info(f"Command timed out ({timeout:.3f}s): {command}")
                    timed_out = True
                    self._kill_timed_out_process(proc)
                    timeout_msg = (
                        f"Command timed out after {timeout:.3f} seconds"
                    ).encode("utf-8")
                    try:
                        stdout_bytes, stderr_bytes = await asyncio.wait_for(
                            communicate_task, timeout=5
                        )
                    except asyncio.TimeoutError:
                        communicate_task.cancel()
                        stdout_bytes, stderr_bytes = b"", timeout_msg
                    else:
                        stderr_bytes = (
                            stderr_bytes + b"\n" + timeout_msg
                            if stderr_bytes
                            else timeout_msg
                        )

            stdout = stdout_bytes.decode("utf-8", errors="ignore")
            stderr = stderr_bytes.decode("utf-8", errors="ignore")
            outputs.append(
                ShellCommandOutput(
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    outcome=ShellCallOutcome(
                        type="timeout" if timed_out else "exit",
                        exit_code=getattr(proc, "returncode", None),
                    ),
                )
            )

            if timed_out:
                break

        return outputs

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        payload = {
            "snapshotter_hash": self.snapshotter.current_hash,
            "commands": request.data.action.commands,
            "timeout_ms": self._timeout_ms_for(request),
            "untracked_cpp_runner_content": self.untracked_cpp_runner_content,
            # These are different per user!
            # "cwd": str(self.cwd),
            # "env": os.environ.copy(),
        }
        hash_payload = utils.stable_json(payload)
        hash = utils.sha256(hash_payload)
        cache_path = self._cache_path_for(hash)

        abbr = request.data.action.commands[0][:75] + (
            "..." if len(request.data.action.commands[0]) > 75 else ""
        )

        # shorten cmd
        shorted_cmds = "\n".join([c[:100] for c in request.data.action.commands])
        shorted_cmds = shorted_cmds[:1000]

        with custom_span(f'shell command ("{abbr}")', {"commands": shorted_cmds}):
            if cache_path.exists():
                cached = utils.load_pickle(cache_path, ShellCacheType)
                assert cached is not None
                outputs = cached.outputs
                if self.runtime_tracker is not None:
                    self.runtime_tracker.add_skipped_time(cached.runtime_seconds)
                logger.debug(
                    f"Read shell output for ({abbr}) from cache: {os.path.basename(cache_path)}"
                )

                # restore snapshot if snapshot hash is available
                self.snapshotter.restore(cached.snapshot_hash)

            else:
                if self.only_from_cache:
                    raise ValueError(
                        f"Shell command output not found in cache and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
                    )
                start_time = time.perf_counter()
                outputs = await self._get_outputs(request)

                # create snapshot of current source code - use response hash as snapshot name
                if not self.do_not_cache:
                    _, commit = self.snapshotter.snapshot(hash)
                    assert commit is not None, (
                        "Failed to create git snapshot for shell command execution"
                    )

                    if cache_path is not None:
                        utils.dump_pickle(
                            cache_path,
                            ShellCacheType(
                                outputs=outputs,
                                hash_payload=hash_payload,
                                runtime_seconds=time.perf_counter() - start_time,
                                snapshot_hash=commit,
                            ),
                            do_not_cache=self.do_not_cache,
                        )

        # check if output exceeding limit, if not return: "output too large to display"
        if self.shell_output_limit is not None:
            tmp_outputs = []
            # check limit for each command output
            for out in outputs:
                total_size = len(out.stdout) + len(out.stderr)
                if total_size >= self.shell_output_limit:
                    tmp_outputs.append(
                        ShellCommandOutput(
                            command=out.command,
                            stdout=f"output too large to display (size {total_size} chars exceeds {self.shell_output_limit} chars limit)",
                            stderr=f"output too large to display (size {total_size} chars exceeds {self.shell_output_limit} chars limit)",
                            outcome=out.outcome,
                        )
                    )
                else:
                    tmp_outputs.append(out)
            outputs = tmp_outputs

        # shorten output
        output_str = "\n".join(
            f"$ {out.command}\nstdout: {out.stdout[:4000]}\nstderr: {out.stderr[:4000]}"
            for out in outputs
        )
        output_truncated = len(output_str) > 20000
        output_str = output_str[:20000]

        # report stats
        log_cmd_list = [c[:500] for c in request.data.action.commands]
        self.run_stats_collector.log_metrics_callback(
            {
                "type": "shell",
                "shell/num_commands": len(request.data.action.commands),
                "shell/commands": log_cmd_list,
                "shell/outputs": output_str,
                "shell/truncated": output_truncated,
            },
            log_and_increment=True,
        )
        self.run_stats_collector.add_to_activity_summary(f"Shell Tool called: {abbr}")

        with custom_span(f'shell command result ("{abbr}")', {"outputs": output_str}):
            return ShellResult(
                output=outputs,
                provider_data={"working_directory": str(self.cwd)},
            )

    async def require_approval(self, commands: Sequence[str]) -> None:
        for entry in commands:
            lines = entry.splitlines()
            max_lines = 20
            if len(lines) > max_lines:
                # show only first 20 lines
                tmp_str = (
                    "\n".join(lines[:max_lines]) + f"\n... (total {len(lines)} lines)"
                )
            else:
                tmp_str = entry

            if tmp_str.count("\n") > 1:
                # show in newline
                logger.debug(f"Running:\n{tmp_str}")
            else:
                # show in single line
                logger.debug(f"Running: {tmp_str}")

            if "sudo" in entry:
                logger.warning(
                    "Command contains 'sudo', which is not allowed for security reasons. We will not execute this command."
                )
                raise SudoInShellError(entry)


class SudoInShellError(RuntimeError):
    def __init__(self, cmd: str):
        super().__init__(
            f"Command contains 'sudo', which is not allowed for security reasons. Command: {cmd}"
        )
