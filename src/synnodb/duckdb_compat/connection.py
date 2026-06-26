"""``SynnoConnection`` — a transparent proxy around a real ``DuckDBPyConnection``.

Composition, not subclassing: pybind11 forbids adopting an existing C connection
into a Python subclass, so ``SynnoConnection`` *holds* a real connection and
delegates everything it does not explicitly own via ``__getattr__``. Consequence:
``isinstance(con, duckdb.DuckDBPyConnection)`` is ``False`` — the ``con.duckdb``
property hands back the real connection for libraries that require it.

What is owned (and may route): the eager SQL-text entry points ``execute`` /
``executemany``. What is delegated verbatim: the relational API (``sql`` returns
DuckDB's lazy relation, *not* routed), ``register`` / ``read_csv`` / ``read_parquet``,
DataFrame/Arrow/Polars egress, ``PRAGMA``/``SET``, transactions — everything.

Cursor model mirrors DuckDB exactly: ``execute`` returns the connection and the
``fetch*`` methods read the *current* result, which is either a bespoke
``SynnoResult`` (when routed) or DuckDB's own last result (when not).
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


class SynnoConnection:
    """A DuckDB-compatible connection that may route eager SQL to a bespoke engine."""

    def __init__(self, inner: Any, router: Any = None, *, owns_inner: bool = True) -> None:
        # Store private state directly to avoid the proxying __getattr__/__setattr__.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_router", router)
        object.__setattr__(self, "_owns_inner", owns_inner)
        # None => fetches delegate to DuckDB; a SynnoResult => fetches read from it.
        object.__setattr__(self, "_current", None)

    # ---- the intercepted entry points ----------------------------------
    def execute(self, query: str, parameters: Any = None) -> "SynnoConnection":
        router = self._router
        if router is None or not router.policy.routing_active:
            self._exec_duckdb(query, parameters)
            object.__setattr__(self, "_current", None)
            return self
        decision = router.route(query, parameters, self)
        if decision.routed and decision.result is not None:
            object.__setattr__(self, "_current", decision.result)
        else:
            self._exec_duckdb(query, parameters)
            object.__setattr__(self, "_current", None)
        return self

    def executemany(self, query: str, parameters: Any = None) -> "SynnoConnection":
        # Parameter-batch execution is for writes; never routed.
        if parameters is None:
            self._inner.executemany(query)
        else:
            self._inner.executemany(query, parameters)
        object.__setattr__(self, "_current", None)
        return self

    def _exec_duckdb(self, query: str, parameters: Any) -> None:
        if parameters is None:
            self._inner.execute(query)
        else:
            self._inner.execute(query, parameters)

    # ---- cursor-style fetching (current result or DuckDB) --------------
    def _fetch_target(self) -> Any:
        return self._current if self._current is not None else self._inner

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._fetch_target().fetchone()

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return self._fetch_target().fetchall()

    def fetchmany(self, size: int = 1) -> List[Tuple[Any, ...]]:
        return self._fetch_target().fetchmany(size)

    def fetchnumpy(self):
        return self._fetch_target().fetchnumpy()

    def fetchdf(self, *args: Any, **kwargs: Any):
        return self._fetch_target().fetchdf(*args, **kwargs)

    def df(self, *args: Any, **kwargs: Any):
        return self._fetch_target().df(*args, **kwargs)

    def arrow(self, *args: Any, **kwargs: Any):
        return self._fetch_target().arrow(*args, **kwargs)

    def fetch_arrow_table(self, *args: Any, **kwargs: Any):
        return self._fetch_target().fetch_arrow_table(*args, **kwargs)

    def to_arrow_table(self, *args: Any, **kwargs: Any):
        return self._fetch_target().to_arrow_table(*args, **kwargs)

    def to_df(self, *args: Any, **kwargs: Any):
        return self._fetch_target().to_df(*args, **kwargs)

    def pl(self, *args: Any, **kwargs: Any):
        return self._fetch_target().pl(*args, **kwargs)

    def fetch_record_batch(self, *args: Any, **kwargs: Any):
        return self._fetch_target().fetch_record_batch(*args, **kwargs)

    @property
    def description(self):
        return self._fetch_target().description

    def __iter__(self):
        return iter(self._fetch_target())

    # ---- relational API: delegated verbatim, never routed --------------
    def sql(self, query: str, *args: Any, **kwargs: Any):
        return self._inner.sql(query, *args, **kwargs)

    def query(self, query: str, *args: Any, **kwargs: Any):
        return self._inner.query(query, *args, **kwargs)

    # ---- lifecycle ------------------------------------------------------
    def cursor(self) -> "SynnoConnection":
        return SynnoConnection(self._inner.cursor(), self._router, owns_inner=True)

    def close(self) -> None:
        if self._owns_inner:
            self._inner.close()

    def __enter__(self) -> "SynnoConnection":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    # ---- escape hatch + introspection ----------------------------------
    @property
    def duckdb(self) -> Any:
        """The wrapped real ``DuckDBPyConnection`` (for libraries that require it)."""
        return self._inner

    @property
    def router(self) -> Any:
        return self._router

    def router_stats(self) -> dict:
        """Session-level routing summary (registry + policy)."""
        reg = getattr(self._router, "registry", None)
        return {
            "mode": str(self._router.policy.mode) if self._router else "off",
            "registry": reg.stats() if reg is not None else {},
        }

    def schema_fingerprint(self, tables: Sequence[str]) -> str:
        """Fingerprint of *tables* in the live DuckDB catalog (name+type per column).

        Used by the schema-match guard. Deterministic and cheap; missing tables are
        encoded as such so a mismatch (engine expects a table that isn't there) fails
        the guard rather than raising.
        """
        import hashlib

        parts: List[str] = []
        for table in sorted(t.lower() for t in tables):
            try:
                rows = self._inner.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE lower(table_name) = ? ORDER BY ordinal_position",
                    [table],
                ).fetchall()
            except Exception:
                rows = []
            cols = ",".join(f"{name}:{dtype}" for name, dtype in rows) if rows else "<missing>"
            parts.append(f"{table}({cols})")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # ---- catch-all passthrough -----------------------------------------
    def __getattr__(self, name: str) -> Any:
        # Only invoked when normal attribute lookup fails -> delegate to DuckDB.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)
