"""Pluggable fallback/source-of-truth backend.

The "base" executor the router falls back to and cross-checks against sits behind
this interface. ``DuckDBBackend`` is the only implementation today; a
``PostgresBackend`` is the reserved seam for "we also want Postgres" — same router,
same guards, a different base — and is intentionally not built yet.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from typing import Any, Optional, Protocol, Tuple

import pyarrow as pa

logger = logging.getLogger(__name__)


class Backend(Protocol):
    """The base executor: runs SQL and returns Arrow (for cross-check / fallback)."""

    def execute_arrow(self, sql: str, parameters: Any = None) -> pa.Table: ...


def _result_to_arrow(cursor: Any) -> pa.Table:
    """Materialize a DuckDB cursor result as an Arrow ``Table`` across versions."""
    if hasattr(cursor, "to_arrow_table"):
        return cursor.to_arrow_table()
    return cursor.fetch_arrow_table()  # older DuckDB


def _read_profile(path: str) -> Optional[dict]:
    """Parse DuckDB's JSON profile at ``path``; ``None`` if it holds no usable ``latency``.

    DuckDB writes an *empty* ``profiling_output`` file when it answers a query without running
    a profiled pipeline - e.g. ``SELECT count(*) FROM t`` / ``SELECT min(a) FROM t`` on a
    checkpointed on-disk table, served straight from storage statistics (issue #98). Such a
    query executed fine; there is just no profiler measurement for it.
    """
    try:
        with open(path, "r") as f:
            profile = json.load(f)
        float(profile["latency"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return profile


def execute_profiled(
    con: Any, sql: str, parameters: Any = None
) -> Tuple[pa.Table, float, Optional[dict]]:
    """Run ``sql`` once on ``con`` with JSON profiling; return ``(table, latency_ms, profile)``.

    ``latency_ms`` is DuckDB's own profiler ``latency`` (the ``EXPLAIN ANALYZE`` number, which
    excludes the client-side result fetch). When DuckDB produced no profile (see
    ``_read_profile``), it falls back to the wall-clock time of ``con.execute`` - likewise
    excluding the Arrow fetch - and ``profile`` is ``None``. Errors from executing the query
    itself propagate unchanged. Profiling is disabled again afterwards, leaving no state on the
    connection.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmp:
        con.execute("PRAGMA enable_profiling = 'json'")
        con.execute(f"PRAGMA profiling_output = '{tmp.name}'")
        try:
            start = time.perf_counter()
            cursor = (
                con.execute(sql, parameters)
                if parameters is not None
                else con.execute(sql)
            )
            wall_ms = (time.perf_counter() - start) * 1_000.0
            table = _result_to_arrow(cursor)
            profile = _read_profile(tmp.name)
        finally:
            # Restore the connection to its unprofiled state so neither later queries nor
            # the router's fallback path pay the profiling cost.
            con.execute("PRAGMA disable_profiling")
    if profile is None:
        logger.debug(
            "DuckDB produced no query profile (no profiled execution); using wall-clock time"
        )
        return table, wall_ms, None
    return table, float(profile["latency"]) * 1_000.0, profile


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

        Queries DuckDB answers from storage statistics alone (e.g. a bare ``count(*)`` on a
        checkpointed table) produce no profile; for those ``server_ms`` is the wall-clock time
        of the execute call instead (see ``execute_profiled``), so such a query still
        cross-checks and routes rather than counting as a reference failure (issue #98).
        """
        table, server_ms, _ = execute_profiled(self._con, sql, parameters)
        return table, server_ms

    @property
    def connection(self) -> Any:
        return self._con
