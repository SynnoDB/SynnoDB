"""``SynnoConnection`` — a transparent proxy around a real ``DuckDBPyConnection``.

Composition, not subclassing: pybind11 forbids adopting an existing C connection
into a Python subclass, so ``SynnoConnection`` *holds* a real connection and
delegates everything it does not explicitly own via ``__getattr__``. Consequence:
``isinstance(con, duckdb.DuckDBPyConnection)`` is ``False`` — the ``con.duckdb``
property hands back the real connection for libraries that require it.

What is owned: the eager SQL-text entry points ``execute`` / ``executemany``. A read-only
query may route; while writes are disabled (the default) a non-read statement raises
``WriteNotSupportedError`` instead of running. What is delegated verbatim: the relational
API (``sql`` returns DuckDB's lazy relation, *not* routed), ``register`` / ``read_csv`` /
``read_parquet``, DataFrame/Arrow/Polars egress, and everything reached through the
``.duckdb`` escape hatch.

Engines are discovered automatically: if an engines directory is configured, ``execute``
scans it (throttled) and registers any newly published engine, so a base implementation
that finishes mid-session starts serving with no code change.

Cursor model mirrors DuckDB exactly: ``execute`` returns the connection and the
``fetch*`` methods read the *current* result, which is either a bespoke
``SynnoResult`` (when routed) or DuckDB's own last result (when not).
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Sequence, Tuple

from ..router.normalize import is_read_only_query
from .discovery import discover_engines
from .errors import WriteNotSupportedError, write_not_supported_message


class SynnoConnection:
    """A DuckDB-compatible connection that may route eager SQL to a bespoke engine."""

    def __init__(
        self,
        inner: Any,
        router: Any = None,
        *,
        owns_inner: bool = True,
        engines_dir: Any = None,
    ) -> None:
        # Store private state directly to avoid the proxying __getattr__/__setattr__.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_router", router)
        object.__setattr__(self, "_owns_inner", owns_inner)
        # None => fetches delegate to DuckDB; a SynnoResult => fetches read from it.
        object.__setattr__(self, "_current", None)
        # Auto-discovery state: a resolved engines dir (or None), the ids registered so far,
        # and a throttle so a hot query loop scans at most once per interval.
        object.__setattr__(self, "_engines_dir", engines_dir)
        object.__setattr__(self, "_registered", set())
        object.__setattr__(self, "_last_discover", None)
        object.__setattr__(self, "_discover_interval", 1.0)

    # ---- the intercepted entry points ----------------------------------
    def execute(self, query: str, parameters: Any = None) -> "SynnoConnection":
        router = self._router
        if router is not None and router.policy.block_writes and not is_read_only_query(query):
            router.note_blocked_write()
            raise WriteNotSupportedError(write_not_supported_message(query))
        if router is None or not router.policy.routing_active:
            self._exec_duckdb(query, parameters)
            object.__setattr__(self, "_current", None)
            return self
        self._maybe_discover()
        decision = router.route(query, parameters, self)
        if decision.routed and decision.result is not None:
            object.__setattr__(self, "_current", decision.result)
        else:
            self._exec_duckdb(query, parameters)
            object.__setattr__(self, "_current", None)
        return self

    def executemany(self, query: str, parameters: Any = None) -> "SynnoConnection":
        # Parameter-batch execution is for writes; never routed, and blocked like any write.
        router = self._router
        if router is not None and router.policy.block_writes and not is_read_only_query(query):
            router.note_blocked_write()
            raise WriteNotSupportedError(write_not_supported_message(query))
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

    # ---- engine auto-discovery -----------------------------------------
    def _discover_now(self) -> None:
        if self._engines_dir is None:
            return
        object.__setattr__(
            self, "_registered", discover_engines(self, self._engines_dir, self._registered)
        )

    def _maybe_discover(self) -> None:
        """Scan for newly published engines, throttled to once per ``_discover_interval``."""
        if self._engines_dir is None:
            return
        now = time.monotonic()
        last = self._last_discover
        if last is not None and now - last < self._discover_interval:
            return
        object.__setattr__(self, "_last_discover", now)
        self._discover_now()

    def refresh_engines(self) -> None:
        """Scan the engines directory now and register anything newly published.

        Discovery happens automatically (throttled) as queries run; call this to force it,
        e.g. immediately after a base implementation finishes.
        """
        object.__setattr__(self, "_last_discover", time.monotonic())
        self._discover_now()

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
        return SynnoConnection(
            self._inner.cursor(), self._router, owns_inner=True, engines_dir=self._engines_dir
        )

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
        """Session-level routing summary: mode, registry state, and routing counters."""
        router = self._router
        reg = getattr(router, "registry", None)
        return {
            "mode": str(router.policy.mode) if router else "off",
            "registry": reg.stats() if reg is not None else {},
            "session": router.stats() if router is not None else {},
        }

    def why(self, query: str, parameters: Any = None) -> dict:
        """Explain how *query* would be handled, without executing it.

        Returns the routing decision, the guards evaluated, and the bound parameters, so you
        can see whether a query is accelerated and, if not, why it falls back. Triggers a
        discovery scan first so a freshly published engine is reflected.
        """
        router = self._router
        if router is None:
            return {"decision": "would-fall-back", "reason": "no router",
                    "template": None, "guards": [], "placeholders": None, "normalized": None}
        self._maybe_discover()
        return router.why(query, parameters, self)

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
