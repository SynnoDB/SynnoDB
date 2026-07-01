"""``ProcessEngine`` - a BespokeEngine over a *real* generated SynnoDB engine.

This is the wiring that lets the drop-in router route a query to an actual
factory-generated C++ engine. It drives the compiled ``./db`` binary through the
framework's warm-subprocess runner (``HotpatchProc``) - the same mechanism the
factory uses to execute/validate engines - feeding it one query line and reading back
the engine's result as a ``pyarrow.Table``.

The engine writes its result as exact, typed Arrow IPC (``result_<req_id>.arrow``,
built with ``cpp_helpers/column_egress.hpp`` - decimal128 straight from the engine's
``__int128`` accumulators), so the result is bit-identical to DuckDB with no CSV /
double round-trip. ``ShmHotLoadEngine`` (below) is the zero-copy hot-load over
``/dev/shm`` behind the same ``BespokeEngine`` interface.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

import pyarrow as pa

from ..errors import EngineExecutionError, EngineResourceError

log = logging.getLogger("synnodb.router.process_engine")


class ProcessEngine:
    """Runs a compiled engine in ``workspace`` against ``parquet_dir`` via HotpatchProc."""

    def __init__(
        self,
        engine_id: str,
        workspace: "str | Path",
        parquet_dir: "str | Path",
        *,
        binary: str = "./db",
        memory_limit_bytes: Optional[int] = None,
        timeout_s: int = 1800,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.engine_id = engine_id
        self.workspace = Path(workspace)
        self.parquet_dir = (
            str(parquet_dir).rstrip("/") + "/"
        )  # loader expects trailing /
        self.binary = binary
        self.memory_limit_bytes = memory_limit_bytes
        self.timeout_s = timeout_s
        self.extra_env = dict(extra_env or {})
        self._proc: Any = None
        self._closed = False
        # Where the engine writes its exact Arrow IPC result (result_<req_id>.arrow). Subclasses
        # (the shm hot-load) point this at /dev/shm for zero-copy egress.
        self._result_dir = self.workspace / "results"
        # Effective working directory for the engine process. Replaced by a per-user scratch
        # directory when workspace is owned by another user (see _ensure_writable_cwd).
        self._cwd = self.workspace

    # ---- BespokeEngine ------------------------------------------------
    def health(self) -> bool:
        return not self._closed and (self.workspace / self.binary.lstrip("./")).exists()

    def _ensure_writable_cwd(self) -> None:
        """Set up a per-user scratch workspace when the engine directory isn't writable.

        The engine's C++ hot-reload code writes to ./build/.reload/ relative to cwd. When
        the engine was published by a different user, that directory is not writable. We create
        a scratch directory in /tmp with symlinks to all engine files and a real writable
        build/.reload/, so any user can run a shared engine without touching the original.
        """
        reload_dir = self.workspace / "build" / ".reload"
        build_dir = self.workspace / "build"

        # Fast path: build/.reload already writable (common case: same user who built it).
        if os.access(reload_dir, os.W_OK):
            return
        # Also fine if build/ is writable and .reload doesn't exist yet (fresh engine).
        if not reload_dir.exists() and os.access(build_dir, os.W_OK):
            return

        uid = os.getuid()
        scratch = Path(tempfile.gettempdir()) / f"synnodb-{uid}" / self.engine_id
        scratch.mkdir(parents=True, exist_ok=True)

        # Symlink every top-level item except build/ itself.
        for name in os.listdir(self.workspace):
            if name == "build":
                continue
            dst = scratch / name
            if not dst.exists():
                dst.symlink_to(self.workspace / name)

        # Create a real build/ dir in scratch with per-file symlinks (so .so files are
        # reachable) but a real writable .reload/ dir owned by the current user.
        scratch_build = scratch / "build"
        scratch_build.mkdir(exist_ok=True)
        (scratch_build / ".reload").mkdir(exist_ok=True)
        if build_dir.exists():
            for name in os.listdir(build_dir):
                if name == ".reload":
                    continue
                dst = scratch_build / name
                if not dst.exists():
                    dst.symlink_to(build_dir / name)

        # Also give this user a writable results dir in the scratch workspace.
        (scratch / "results").mkdir(exist_ok=True)

        log.debug(
            "engine=%s: workspace not writable by uid=%d; using scratch cwd=%s",
            self.engine_id,
            uid,
            scratch,
        )
        self._cwd = scratch
        self._result_dir = scratch / "results"

    def _runner(self) -> Any:
        from synnodb.cpp_runner.hotpatch.hotpatch_proc import (
            HotpatchProc,
        )  # heavy: lazy

        if self._proc is None:
            self._ensure_writable_cwd()
            cmd = f"{self.binary} {self.parquet_dir}"
            log.info(
                "engine=%s starting warm runner: %s (cwd=%s)",
                self.engine_id,
                cmd,
                self._cwd,
            )
            self._proc = HotpatchProc(
                command=cmd, cwd=self._cwd, memory_limit_bytes=self.memory_limit_bytes
            )
        return self._proc

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> pa.Table:
        from synnodb.workloads.workload_provider import format_args_element

        qa = format_args_element(str(query_id), dict(placeholders))
        req_id = qa.split()[1]
        # _runner() must come first: it may update self._result_dir via _ensure_writable_cwd.
        runner = self._runner()
        self._result_dir.mkdir(parents=True, exist_ok=True)
        arrow_path = self._result_dir / f"result_{req_id}.arrow"
        if arrow_path.exists():
            arrow_path.unlink()  # never validate a stale result left by a crashed prior run

        run_env = {**self.extra_env, "SYNNODB_RESULT_DIR": str(self._result_dir)}
        log.debug("engine=%s run query_id=%s line=%r", self.engine_id, query_id, qa)
        result = runner.run(timeout=self.timeout_s, query_lines=[qa], run_env=run_env)

        for qr in result.query_results or []:
            err = getattr(qr, "error", None)
            if err:
                raise EngineExecutionError(
                    f"engine reported an error for query {query_id}: {err}",
                    engine_id=self.engine_id,
                    query_id=str(query_id),
                    req_id=req_id,
                    response=result.response,
                    stderr=result.stderr,
                )
        if not arrow_path.exists():
            raise EngineExecutionError(
                "engine produced no result file (result_<req_id>.arrow)",
                engine_id=self.engine_id,
                query_id=str(query_id),
                req_id=req_id,
                response=result.response,
                stderr=result.stderr,
            )
        try:
            # Exact, typed: the engine built Arrow (decimal128 from its int128 accumulators),
            # bit-identical to DuckDB.
            table = self._read_arrow(arrow_path)
        except Exception as exc:
            # A truncated/corrupt result (engine crashed mid-write) must surface the engine's own
            # diagnostics, not an opaque pyarrow/IO error from deep in the read.
            raise EngineExecutionError(
                f"failed to read engine result: {exc}",
                engine_id=self.engine_id,
                query_id=str(query_id),
                req_id=req_id,
                response=result.response,
                stderr=result.stderr,
            ) from exc
        # A 0-row table is a legitimate empty answer (not an error); the cross-check compares it to
        # DuckDB by row content, so an empty engine result vs a non-empty DuckDB result mismatches.
        log.debug(
            "engine=%s query_id=%s -> %d rows, %d cols",
            self.engine_id,
            query_id,
            table.num_rows,
            table.num_columns,
        )
        return table

    def _read_arrow(self, path: Path) -> pa.Table:
        # Own the result: read the IPC file through an OSFile (a copy into in-process buffers) and
        # close it, so the returned Table is a true snapshot. A memory_map would instead alias the
        # live file, and a later run() (which unlinks+rewrites the same req_id) or close()/rmtree
        # could mutate or shrink it under an already-returned Table - reading corrupt/garbage data.
        # Results are small (aggregations), so the copy is negligible next to that correctness risk.
        with pa.OSFile(str(path), "r") as source:
            return pa.ipc.open_file(source).read_all()

    def close(self) -> None:
        self._closed = True
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def __enter__(self) -> "ProcessEngine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Last-resort cleanup if the engine is dropped without an explicit close() (an exception
        # between ingest and registration, a GC'd connection, ...). Releases the warm subprocess
        # and - for the shm hot-load - its /dev/shm segments, so neither is leaked. close() is
        # idempotent and polymorphic; __del__ must never raise, including during interpreter
        # shutdown where module globals may already be gone.
        try:
            self.close()
        except Exception:
            pass


_INGEST_PREFIX = "synno-ingest-"


def _proc_start_time(pid: int) -> Optional[str]:
    """The owner process's start time (``/proc/<pid>/stat`` field 22, in clock ticks since boot),
    used to tell a recycled PID from the original owner. None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
        # field 2 (comm) is parenthesized and may contain spaces/parens; split after the last ')'.
        post = data[data.rfind(")") + 2 :].split()
        return post[19]  # field 22 overall -> index 19 among the post-comm fields
    except (OSError, IndexError):
        return None


def _sweep_ingest_orphans(base: Path) -> int:
    """Remove ``synno-ingest-<pid>-<starttime>-*`` ingest dirs whose owner is gone, so a SIGKILL'd
    connection does not leak shm. Reaps when the PID is dead OR its current start time differs from
    the tag (the PID was recycled into an unrelated live process) - PID-alive alone would keep a
    genuine orphan forever after PID reuse. Only touches our own prefix."""
    from .shm_transport import _pid_alive

    removed = 0
    try:
        children = list(base.glob(f"{_INGEST_PREFIX}*"))
    except OSError:
        return 0
    for path in children:
        parts = path.name[len(_INGEST_PREFIX) :].split("-")
        pid_str = parts[0] if parts else ""
        tagged_start = parts[1] if len(parts) > 1 else None
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        alive = _pid_alive(pid)
        # Reap if dead, or if alive but the start time no longer matches (PID recycled). When the
        # tag or /proc has no start time, fall back to the dead-PID check alone.
        if alive and (
            tagged_start is None or _proc_start_time(pid) in (None, tagged_start)
        ):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


class ShmHotLoadEngine(ProcessEngine):
    """A bespoke engine fed its data zero-copy from ``/dev/shm`` Arrow - the hot-load plane.

    The connection hands it the live DuckDB tables as Arrow once via :meth:`ingest` (the
    connect-time step); the engine writes each as an Arrow-IPC segment under ``/dev/shm`` and
    the loader maps them (via ``SYNNODB_SHM_INGEST``) on its first ``run``. The result rides the
    same ``/dev/shm`` back as Arrow - both directions are zero-copy Arrow over shared memory.
    """

    def __init__(
        self,
        engine_id: str,
        workspace: "str | Path",
        *,
        shm_dir: "str | Path | None" = None,
        **kwargs: Any,
    ) -> None:
        # parquet_dir is unused on this plane (the loader ignores argv[1] when
        # SYNNODB_SHM_INGEST is set); it is replaced by the ingest dir at ingest time.
        super().__init__(engine_id, workspace, shm_dir or "/dev/shm", **kwargs)
        self._shm_base = Path(shm_dir) if shm_dir is not None else None
        self._ingest_dir: Optional[Path] = None
        self._loaded = False

    def health(self) -> bool:
        return super().health() and self._loaded

    def ingest(self, tables: Mapping[str, pa.Table]) -> None:
        """Stage the engine's data snapshot as ``/dev/shm`` Arrow segments. Call once before
        the first query; the loader maps them on its first run and holds them resident. Safe to
        call again (the previous snapshot is reclaimed first); a write failure leaves nothing."""
        import pyarrow.ipc as ipc

        from .shm_transport import SHM_DIR

        if (
            self._ingest_dir is not None
        ):  # idempotent: drop a prior snapshot before re-ingesting
            shutil.rmtree(self._ingest_dir, ignore_errors=True)
            self._ingest_dir = None
            self._loaded = False
        base = self._shm_base or SHM_DIR
        base.mkdir(parents=True, exist_ok=True)
        _sweep_ingest_orphans(base)
        # Budget check: the hot-load copies every table into shared memory (tmpfs = RAM) while
        # DuckDB still holds its own copy, so refuse up front if the snapshot will not fit - a
        # mid-write ENOSPC would leave a partial segment - keeping a reserve free for everything
        # else on the box.
        needed = sum(int(t.nbytes) for t in tables.values())
        usage = shutil.disk_usage(base)
        reserve = max(64 * 1024 * 1024, usage.total // 20)
        if needed + reserve > usage.free:
            raise EngineResourceError(
                f"not enough shared memory to hot-load this database into {base}",
                context={
                    "needed_MiB": round(needed / 1048576, 1),
                    "free_MiB": round(usage.free / 1048576, 1),
                    "reserve_MiB": round(reserve / 1048576, 1),
                    "hint": "use the parquet (standalone) plane, or free space in /dev/shm",
                },
            )
        pid = os.getpid()
        start = _proc_start_time(pid) or "0"
        ingest_dir = Path(
            tempfile.mkdtemp(prefix=f"{_INGEST_PREFIX}{pid}-{start}-", dir=base)
        )
        try:
            total = 0
            for name, table in tables.items():
                seg = ingest_dir / f"{name}.arrow"
                with pa.OSFile(str(seg), "wb") as sink:
                    with ipc.new_file(sink, table.schema) as writer:
                        writer.write_table(table)
                nbytes = seg.stat().st_size
                total += nbytes
                log.debug(
                    "engine=%s shm ingest table=%s rows=%d bytes=%d",
                    self.engine_id,
                    name,
                    table.num_rows,
                    nbytes,
                )
        except Exception:
            shutil.rmtree(ingest_dir, ignore_errors=True)
            raise
        self._ingest_dir = ingest_dir
        # argv[1] becomes the ingest dir (unused for loading); the env switches on the plane.
        self.parquet_dir = str(ingest_dir).rstrip("/") + "/"
        # Egress the result into the same /dev/shm dir, so a result_<req_id>.arrow is read back
        # zero-copy (input and output both ride shared memory - Arrow everywhere).
        self._result_dir = ingest_dir
        self.extra_env = {**self.extra_env, "SYNNODB_SHM_INGEST": str(ingest_dir)}
        self._loaded = True
        log.info(
            "engine=%s ingested %d table(s), %.1f MiB via shm -> %s",
            self.engine_id,
            len(tables),
            total / 1048576,
            ingest_dir,
        )

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> pa.Table:
        if not self._loaded:
            raise RuntimeError(f"engine {self.engine_id}: run() called before ingest()")
        return super().run(query_id, placeholders)

    def close(self) -> None:
        super().close()
        if self._ingest_dir is not None:
            shutil.rmtree(self._ingest_dir, ignore_errors=True)
            self._ingest_dir = None
        self._loaded = False
