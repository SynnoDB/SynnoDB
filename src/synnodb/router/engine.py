"""Bespoke engine handles: the abstraction the router runs a matched query against.

``BespokeEngine`` is the seam that decouples the router from *how* an engine
executes. Implementations:

* ``LocalCallableEngine`` — in-process, backed by Python callables. Used for tests
  and for pure-Python engines; it lets the entire routing path (match → guards →
  execute → adapt → cross-check) be exercised without any C++/IPC.
* ``ProcessEngine`` (``router.process_engine``) — the warm C++ subprocess: a generated
  ``db`` binary held resident behind ``HotpatchProc``, fed one query line and replying with
  its exact Arrow result (``result_<req_id>.arrow``). Reads its data from a disk parquet
  snapshot.
* ``ShmHotLoadEngine`` (a ``ProcessEngine`` subclass) — the same warm binary, but ingested
  the connection's live DuckDB tables once as zero-copy Arrow over ``/dev/shm``
  (``SYNNODB_SHM_INGEST``); both ingest and result ride shared memory. This is the production
  serving path the router auto-discovers (see ``duckdb_compat.discovery``).

Every implementation returns a ``pyarrow.Table`` whose schema is expected to match
the binding's canonical (DuckDB) output schema; ``adapt`` turns it into a
``SynnoResult``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Protocol, runtime_checkable

import pyarrow as pa

# A pure function from bound placeholder values to a typed Arrow result.
QueryFn = Callable[[Mapping[str, Any]], pa.Table]


@runtime_checkable
class BespokeEngine(Protocol):
    """A runnable bespoke engine bound to one generated artifact."""

    engine_id: str

    def health(self) -> bool:
        """Cheap liveness probe; ``False`` makes the router fall back."""

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> pa.Table:
        """Execute one registered query with bound placeholder values."""

    def close(self) -> None:
        """Release resources (worker process, shm segments, ...)."""


class LocalCallableEngine:
    """In-process engine backed by ``{query_id: fn(placeholders) -> pa.Table}``."""

    def __init__(
        self,
        engine_id: str,
        queries: Mapping[str, QueryFn],
        *,
        healthy: bool = True,
    ) -> None:
        self.engine_id = engine_id
        self._queries: Dict[str, QueryFn] = dict(queries)
        self._healthy = healthy

    def health(self) -> bool:
        return self._healthy

    def set_healthy(self, value: bool) -> None:
        self._healthy = value

    def run(self, query_id: str, placeholders: Mapping[str, Any]) -> pa.Table:
        fn = self._queries.get(query_id)
        if fn is None:
            raise KeyError(f"engine {self.engine_id!r} has no query {query_id!r}")
        table = fn(placeholders)
        if not isinstance(table, pa.Table):
            raise TypeError(
                f"engine {self.engine_id!r} query {query_id!r} returned "
                f"{type(table).__name__}, expected pyarrow.Table"
            )
        return table

    def close(self) -> None:  # nothing to release
        pass
