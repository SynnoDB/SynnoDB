"""``ProcessEngine`` — a BespokeEngine over a *real* generated SynnoDB engine.

This is the wiring that lets the drop-in router route a query to an actual
factory-generated C++ engine. It drives the compiled ``./db`` binary through the
framework's warm-subprocess runner (``HotpatchProc``) — the same mechanism the
factory uses to execute/validate engines — feeding it one query line and reading the
result CSV the engine writes, which it returns as a ``pyarrow.Table``.

This is the **correctness/integration** path (reuses the proven engine runner). It
is heavier than the shm ``WorkerEngine`` (it imports ``synnodb.cpp_runner``, so it is
*not* part of the light runtime and is not re-exported from ``synnodb.router``).
The shm zero-copy ``WorkerEngine`` is the perf upgrade behind the same
``BespokeEngine`` interface.

Result types: the engine writes CSV (types lost in C++), so values come back via
pandas type inference. ``adapt.results_equal`` (the cross-check) is tolerant of the
int/float/decimal differences this introduces; for stricter typing pass
``output_schema`` and the columns are cast to those Arrow types.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import pyarrow as pa

log = logging.getLogger("synnodb.router.process_engine")


def _arrow_type_for(duckdb_type: str) -> "pa.DataType":
    base = duckdb_type.strip().upper().split("(")[0].split(" ")[0]
    return {
        "BIGINT": pa.int64(), "HUGEINT": pa.int64(), "INTEGER": pa.int64(), "INT": pa.int64(),
        "SMALLINT": pa.int64(), "DOUBLE": pa.float64(), "FLOAT": pa.float64(), "REAL": pa.float64(),
        "DECIMAL": pa.float64(), "BOOLEAN": pa.bool_(),
    }.get(base, pa.string())


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
        output_schema: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None:
        self.engine_id = engine_id
        self.workspace = Path(workspace)
        self.parquet_dir = str(parquet_dir).rstrip("/") + "/"  # loader expects trailing /
        self.binary = binary
        self.memory_limit_bytes = memory_limit_bytes
        self.timeout_s = timeout_s
        self.extra_env = dict(extra_env or {})
        self.output_schema = list(output_schema) if output_schema else None
        self._proc: Any = None

    # ---- BespokeEngine ------------------------------------------------
    def health(self) -> bool:
        return (self.workspace / self.binary.lstrip("./")).exists()

    def _runner(self) -> Any:
        from synnodb.cpp_runner.hotpatch.hotpatch_proc import HotpatchProc  # heavy: lazy

        if self._proc is None:
            cmd = f"{self.binary} {self.parquet_dir}"
            log.info("engine=%s starting warm runner: %s (cwd=%s)", self.engine_id, cmd, self.workspace)
            self._proc = HotpatchProc(command=cmd, cwd=self.workspace, memory_limit_bytes=self.memory_limit_bytes)
        return self._proc

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> pa.Table:
        from synnodb.workloads.workload_provider import format_args_element

        qa = format_args_element(str(query_id), dict(placeholders))
        req_id = qa.split()[1]
        results_dir = self.workspace / "results"
        results_dir.mkdir(exist_ok=True)
        csv_path = results_dir / f"result_{req_id}.csv"
        if csv_path.exists():
            csv_path.unlink()

        log.debug("engine=%s run query_id=%s line=%r", self.engine_id, query_id, qa)
        result = self._runner().run(timeout=self.timeout_s, query_lines=[qa], run_env=self.extra_env)

        for qr in (result.query_results or []):
            err = getattr(qr, "error", None)
            if err:
                raise RuntimeError(f"engine error (q{query_id}): {err}")
        if not csv_path.exists():
            raise RuntimeError(
                f"engine produced no result CSV at {csv_path}; response={result.response!r}; "
                f"stderr={(result.stderr or '')[-1000:]}"
            )
        table = self._read_csv(csv_path)
        log.debug("engine=%s query_id=%s -> %d rows, %d cols", self.engine_id, query_id, table.num_rows, table.num_columns)
        return table

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    # ---- result CSV -> Arrow ------------------------------------------
    def _read_csv(self, csv_path: Path) -> pa.Table:
        import pandas as pd

        df = pd.read_csv(csv_path, header=0, escapechar="\\", quotechar='"', doublequote=True)
        table = pa.Table.from_pandas(df, preserve_index=False)
        if self.output_schema:
            table = self._cast(table)
        return table

    def _cast(self, table: pa.Table) -> pa.Table:
        assert self.output_schema is not None
        arrays = []
        names = []
        for (name, dtype), col in zip(self.output_schema, table.columns):
            names.append(name)
            target = _arrow_type_for(dtype)
            try:
                arrays.append(col.cast(target) if col.type != target else col)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                arrays.append(col)
        return pa.table(dict(zip(names, arrays)))
