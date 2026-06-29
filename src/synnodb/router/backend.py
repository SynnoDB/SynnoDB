"""Pluggable fallback/source-of-truth backend.

The "base" executor the router falls back to and cross-checks against sits behind
this interface. ``DuckDBBackend`` is the only implementation today; a
``PostgresBackend`` is the reserved seam for "we also want Postgres" — same router,
same guards, a different base — and is intentionally not built yet.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

import pyarrow as pa


class Backend(Protocol):
    """The base executor: runs SQL and returns Arrow (for cross-check / fallback)."""

    def execute_arrow(self, sql: str, parameters: Any = None) -> pa.Table:
        ...


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
        cursor = self._con.execute(sql, parameters) if parameters is not None else self._con.execute(sql)
        return _result_to_arrow(cursor)

    @property
    def connection(self) -> Any:
        return self._con
