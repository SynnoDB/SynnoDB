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
from .engine import TimedTable

log = logging.getLogger("synnodb.router.process_engine")


def read_result_table(path: Path) -> pa.Table:
    """Read an Arrow-IPC result file into an owning in-process Table.

    Reads through an OSFile (a copy into in-process buffers), NOT a memory_map: a result is
    deleted the instant it is read (it has exactly one consumer), and a memory_map would alias
    the live file so a later unlink/rewrite of the same req_id, or a close()/rmtree, could mutate
    or shrink it under an already-returned Table. Results are small (aggregations), so the copy is
    negligible next to that correctness risk.
    """
    with pa.OSFile(str(path), "r") as source:
        return pa.ipc.open_file(source).read_all()


def read_and_delete_result(path: Path) -> pa.Table:
    """Read a result file into memory and delete it - a result has exactly one consumer, so it
    must never outlive the read (deleted on a read failure too). The serving path and the
    validation path both funnel through here so the read semantics and the delete stay in one
    place - a future result reader inherits both instead of silently reopening the stale-file leak.
    """
    try:
        return read_result_table(path)
    finally:
        path.unlink(missing_ok=True)


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
        # Whether the warm subprocess has already loaded its data (loader + builder ran). The
        # first run() pays that one-time cost; load_data() pays it up front instead. Reset on close().
        self._loaded_data = False
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

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> TimedTable:
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

        # Walk the per-query results once: surface any engine error, and pick up the engine's own
        # server-side execution time. The C++ kernel measures each query with steady_clock (see
        # assemble_query_impl) and reports it as `elapsed_ms`, excluding result serialization and
        # transport; the router uses this in preference to its external wall clock so the reported
        # speedup reflects pure engine execution. A negative `elapsed_ms` is the sentinel for "no
        # query matched", so only a value >= 0 for our req_id counts as a real measurement.
        server_ms: Optional[float] = None
        for qr in result.query_results or []:
            if qr.error:
                raise EngineExecutionError(
                    f"engine reported an error for query {query_id}: {qr.error}",
                    engine_id=self.engine_id,
                    query_id=str(query_id),
                    req_id=req_id,
                    response=result.response,
                    stderr=result.stderr,
                )
            if qr.req_id == req_id and qr.elapsed_ms >= 0:
                server_ms = float(qr.elapsed_ms)

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
            # bit-identical to DuckDB. read_and_delete_result deletes the file the instant it is
            # read, so nothing is left on disk once it has been handed back to the caller.
            table = read_and_delete_result(arrow_path)
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
        return table, server_ms

    def load_data(self) -> None:
        """Load this engine's data now, so the first query it serves is already fast.

        The engine loads its snapshot (parquet plane) or maps its ``/dev/shm`` segments (hot-load
        plane) and builds its in-memory database lazily, on the first ``run`` - a one-time cost
        (seconds at scale) that otherwise lands on whichever query arrives first. Sending an empty
        query batch drives the same loader -> builder pipeline with no query to execute, leaving
        the data resident so every later query is served warm. Idempotent: the load runs once, so a
        second call is a cheap round-trip. Best-effort - a crash here does not corrupt the engine
        (the next ``run`` restarts it); it is raised so the caller can report a broken engine."""
        if self._loaded_data:
            return
        # _runner() must come first: it may update self._result_dir via _ensure_writable_cwd.
        runner = self._runner()
        self._result_dir.mkdir(parents=True, exist_ok=True)
        run_env = {**self.extra_env, "SYNNODB_RESULT_DIR": str(self._result_dir)}
        log.info("engine=%s loading data", self.engine_id)
        result = runner.run(timeout=self.timeout_s, query_lines=[], run_env=run_env)
        # An empty batch produces no result file to check; the signal that the loader/builder
        # completed is the child still being alive (a crash mid-load leaves it dead).
        if not runner.is_running():
            raise EngineExecutionError(
                "engine crashed during data loading",
                engine_id=self.engine_id,
                query_id="<load_data>",
                req_id="<load_data>",
                response=result.response,
                stderr=result.stderr,
            )
        self._loaded_data = True

    def _read_arrow(self, path: Path) -> pa.Table:
        # Instance-method shim over the module-level owning read (kept for the adversarial
        # snapshot tests); see read_result_table for why this must not memory_map.
        return read_result_table(path)

    def close(self) -> None:
        self._closed = True
        self._loaded_data = False
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


# Start-time reader and orphan sweep live in shm_transport now (shared with the synthesis-time
# shm_stage); these keep the historical private names for callers/tests that import them.
from .shm_transport import proc_start_time as _proc_start_time  # noqa: E402


def _sweep_ingest_orphans(base: Path) -> int:
    """Remove ``synno-ingest-<pid>-<starttime>-*`` ingest dirs whose owner is gone (dead PID, or a
    recycled PID whose start time no longer matches), so a SIGKILL'd connection does not leak shm."""
    from .shm_transport import sweep_ingest_orphans

    return sweep_ingest_orphans(base, _INGEST_PREFIX)


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
        from .shm_transport import (
            SHM_DIR,
            check_shm_budget,
            write_arrow_segments,
        )

        if (
            self._ingest_dir is not None
        ):  # idempotent: drop a prior snapshot before re-ingesting
            shutil.rmtree(self._ingest_dir, ignore_errors=True)
            self._ingest_dir = None
            self._loaded = False
            self._loaded_data = (
                False  # new data means the process must reload before it is warm
            )
        base = self._shm_base or SHM_DIR
        base.mkdir(parents=True, exist_ok=True)
        _sweep_ingest_orphans(base)
        # Budget check: the hot-load copies every table into shared memory (tmpfs = RAM) while
        # DuckDB still holds its own copy, so refuse up front if the snapshot will not fit - a
        # mid-write ENOSPC would leave a partial segment - keeping a reserve free for everything
        # else on the box.
        needed = sum(int(t.nbytes) for t in tables.values())
        fits, free, reserve = check_shm_budget(base, needed)
        if not fits:
            raise EngineResourceError(
                f"not enough shared memory to hot-load this database into {base}",
                context={
                    "needed_MiB": round(needed / 1048576, 1),
                    "free_MiB": round(free / 1048576, 1),
                    "reserve_MiB": round(reserve / 1048576, 1),
                    "hint": "use the parquet (standalone) plane, or free space in /dev/shm",
                },
            )
        pid = os.getpid()
        start = _proc_start_time(pid) or "0"
        ingest_dir = Path(
            tempfile.mkdtemp(prefix=f"{_INGEST_PREFIX}{pid}-{start}-", dir=base)
        )
        total = write_arrow_segments(ingest_dir, tables)
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

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> TimedTable:
        if not self._loaded:
            raise RuntimeError(f"engine {self.engine_id}: run() called before ingest()")
        return super().run(query_id, placeholders)

    def load_data(self) -> None:
        # The hot-load plane has nothing to load until its data has been staged into /dev/shm.
        if not self._loaded:
            raise RuntimeError(
                f"engine {self.engine_id}: load_data() called before ingest()"
            )
        super().load_data()

    def close(self) -> None:
        super().close()
        if self._ingest_dir is not None:
            shutil.rmtree(self._ingest_dir, ignore_errors=True)
            self._ingest_dir = None
        self._loaded = False
