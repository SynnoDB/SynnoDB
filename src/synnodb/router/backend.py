"""Pluggable fallback/source-of-truth backend.

The "base" executor the router falls back to and cross-checks against sits behind
this interface. ``DuckDBBackend`` is the only implementation today; a
``PostgresBackend`` is the reserved seam for "we also want Postgres" — same router,
same guards, a different base — and is intentionally not built yet.
"""

from __future__ import annotations

import json
import tempfile
from typing import Any, Protocol, Tuple

import pyarrow as pa


class Backend(Protocol):
    """The base executor: runs SQL and returns Arrow (for cross-check / fallback)."""

    def execute_arrow(self, sql: str, parameters: Any = None) -> pa.Table: ...


def _result_to_arrow(cursor: Any) -> pa.Table:
    """Materialize a DuckDB cursor result as an Arrow ``Table`` across versions."""
    if hasattr(cursor, "to_arrow_table"):
        return cursor.to_arrow_table()
    return cursor.fetch_arrow_table()  # older DuckDB


class DuckDBBackend:
    """Runs SQL on a real ``DuckDBPyConnection`` and returns an Arrow table.

    Used by the router to (a) execute the cross-check comparison and (b) — in modes
    where the router owns execution — produce the fallback result. The connection is
    the canonical store; this never mutates it.
    """

    def __init__(self, connection: Any) -> None:
        self._con = connection

    def execute_arrow(self, sql: str, parameters: Any = None) -> pa.Table:
        cursor = (
            self._con.execute(sql, parameters)
            if parameters is not None
            else self._con.execute(sql)
        )
        return _result_to_arrow(cursor)

    def execute_arrow_timed(
        self, sql: str, parameters: Any = None
    ) -> Tuple[pa.Table, float]:
        """Execute ``sql`` and return ``(arrow_table, server_ms)``.

        ``server_ms`` is DuckDB's *own* measurement of the query's execution time - the
        ``latency`` field of its JSON query profile, the same number ``EXPLAIN ANALYZE``
        reports. It is measured inside the engine and excludes the client-side result fetch,
        so it compares like-for-like against the bespoke engine's internal ``elapsed_ms``
        (which likewise times only kernel execution, not result serialization/transport).

        Both the Arrow result (for the correctness cross-check) and the timing come from a
        single execution, so the number reflects the exact run whose rows we verify. Profiling
        is enabled only around this call and disabled again, leaving no state on the shared
        connection and no profiling overhead on the router's plain fallback path.
        """
        con = self._con
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmp:
            con.execute("PRAGMA enable_profiling = 'json'")
            con.execute(f"PRAGMA profiling_output = '{tmp.name}'")
            try:
                cursor = (
                    con.execute(sql, parameters)
                    if parameters is not None
                    else con.execute(sql)
                )
                table = _result_to_arrow(cursor)
                with open(tmp.name, "r") as f:
                    profile = json.load(f)
            finally:
                # Restore the connection to its unprofiled state so neither later queries nor
                # the router's fallback path pay the profiling cost.
                con.execute("PRAGMA disable_profiling")
        return table, float(profile["latency"]) * 1_000.0

    @property
    def connection(self) -> Any:
        return self._con
