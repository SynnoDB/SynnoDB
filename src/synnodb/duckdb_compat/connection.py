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

import logging
import sys
import time
from typing import Any, List, Optional, Sequence, Tuple

import duckdb

from ..errors import SynnoError
from ..router.normalize import is_read_only_query
from .discovery import discover_engines
from .errors import WriteNotSupportedError, write_not_supported_message

log = logging.getLogger("synnodb.connection")


def _stderr_is_tty() -> bool:
    try:
        return bool(getattr(sys.stderr, "isatty", None) and sys.stderr.isatty())
    except Exception:
        return False


class _NoSpinner:
    """A shared no-op context manager for the non-interactive path (no thread, no import of the
    display module, no overhead)."""

    def __enter__(self) -> "_NoSpinner":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


_NULL_SPINNER = _NoSpinner()


class SynnoConnection:
    """A DuckDB-compatible connection that may route eager SQL to a bespoke engine."""

    def __init__(
        self,
        inner: Any,
        router: Any = None,
        *,
        owns_inner: bool = True,
        engines_dir: Any = None,
        mount: bool = False,
        owns_router: bool = True,
        engine_refcount: Optional[list] = None,
        engine_threads: Optional[int] = None,
    ) -> None:
        # Store private state directly to avoid the proxying __getattr__/__setattr__.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_router", router)
        object.__setattr__(self, "_owns_inner", owns_inner)
        object.__setattr__(self, "_owns_router", owns_router)
        # The engines registered on a router are shared by every connection that uses it (the
        # parent and any cursors it spawned). They must be closed exactly once, when the LAST
        # such handle closes - never when the parent closes while a cursor is still routing to
        # them. A shared one-element refcount (created by the first connection, passed to each
        # cursor) tracks the live handles; the one that drops it to zero releases the engines.
        if engine_refcount is None:
            engine_refcount = [0]
        engine_refcount[0] += 1
        object.__setattr__(self, "_engine_refcount", engine_refcount)
        # None => fetches delegate to DuckDB; a SynnoResult => fetches read from it.
        object.__setattr__(self, "_current", None)
        # Auto-discovery state: a resolved engines dir (or None), the ids registered so far,
        # and a throttle so a hot query loop scans at most once per interval.
        object.__setattr__(self, "_engines_dir", engines_dir)
        object.__setattr__(self, "_registered", set())
        object.__setattr__(self, "_last_discover", None)
        object.__setattr__(self, "_discover_interval", 1.0)
        # When set, discovery mounts an engine's own bundled snapshot as views if the
        # connection lacks its tables (serve the synthesized database with no live DuckDB).
        object.__setattr__(self, "_mount", mount)
        # The DuckDB-style config={'threads': N}: when set, every routed engine runs at N
        # threads, overriding the count the engine was published with. None => the engine
        # serves at its own recorded thread count (manifest.threads).
        object.__setattr__(self, "_engine_threads", engine_threads)
        # Last-query timing (for the interactive footer) and a cached interactivity flag, so the
        # spinner/display machinery is a no-op off a TTY - the non-interactive hot path pays nothing.
        object.__setattr__(self, "_last", None)
        object.__setattr__(self, "_interactive", _stderr_is_tty())

    # ---- the intercepted entry points ----------------------------------
    def execute(self, query: str, parameters: Any = None) -> "SynnoConnection":
        router = self._router
        if (
            router is not None
            and router.policy.block_writes
            and not is_read_only_query(query)
        ):
            router.note_blocked_write()
            raise WriteNotSupportedError(write_not_supported_message(query))
        with self._spinner():
            if router is None or not router.policy.routing_active:
                self._run_duckdb_timed(query, parameters)
            else:
                self._maybe_discover()
                decision = router.route(query, parameters, self)
                if decision.routed and decision.result is not None:
                    object.__setattr__(self, "_current", decision.result)
                    self._capture_routed(decision)
                else:
                    self._run_duckdb_timed(query, parameters)
        return self

    def _run_duckdb_timed(self, query: str, parameters: Any) -> None:
        t0 = time.perf_counter()
        self._exec_duckdb(query, parameters)
        object.__setattr__(self, "_current", None)
        object.__setattr__(
            self,
            "_last",
            {"served_by": "duckdb", "duckdb_ms": (time.perf_counter() - t0) * 1000.0},
        )

    def _capture_routed(self, decision: Any) -> None:
        tr = decision.trace
        if tr.served_by == "duckdb":
            # The engine ran but diverged (or could not be compared); the trusted DuckDB reference
            # was served. Report it honestly so the footer shows DuckDB, not a bogus engine speedup
            # for a result the engine did not actually produce.
            object.__setattr__(
                self, "_last", {"served_by": "duckdb", "duckdb_ms": tr.duckdb_ms}
            )
        else:
            object.__setattr__(
                self,
                "_last",
                {
                    "served_by": "engine",
                    "engine_ms": tr.bespoke_ms,
                    "duckdb_ms": tr.duckdb_ms,
                    "template": tr.template,
                },
            )

    def _spinner(self):
        # Interactive-only: off a TTY this is a shared no-op context manager (no thread, no I/O),
        # so non-interactive execution is exactly as before.
        if not self._interactive:
            return _NULL_SPINNER
        from .display import Spinner

        return Spinner.for_stream(sys.stderr)

    def executemany(self, query: str, parameters: Any = None) -> "SynnoConnection":
        # Parameter-batch execution is for writes; never routed, and blocked like any write.
        router = self._router
        if (
            router is not None
            and router.policy.block_writes
            and not is_read_only_query(query)
        ):
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
            self,
            "_registered",
            discover_engines(
                self,
                self._engines_dir,
                self._registered,
                mount=self._mount,
                threads_override=self._engine_threads,
            ),
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

    def synno_ingest_data(self) -> int:
        """Load every discovered engine's data now, so the first query each serves is warm.

        A bespoke engine loads its snapshot (and builds its in-memory database) lazily, on the
        first query routed to it - a one-time cost, seconds at scale, that otherwise lands on
        whichever query happens to arrive first and shows up as a slow first result. Call this
        once, right after :meth:`refresh_engines`, to pay that cost up front as an explicit step.

        Returns the number of engines whose data was loaded. A distinct engine is loaded once
        even though it backs several query templates. Each failure is logged and skipped - the
        engine still works, its first query just pays the load - so one bad engine never raises.
        """
        registry = getattr(self._router, "registry", None)
        if registry is None:
            return 0
        loaded = 0
        seen: set = set()
        for binding in registry.bindings():
            engine = getattr(binding, "engine", None)
            if engine is None or id(engine) in seen:
                continue
            seen.add(id(engine))
            loader = getattr(engine, "load_data", None)
            if not callable(loader):
                continue
            try:
                loader()
                loaded += 1
            except Exception as exc:
                log.warning(
                    "engine %s failed to load data (its first query will pay the load): %s",
                    getattr(engine, "engine_id", "?"),
                    exc,
                )
        return loaded

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

    # ---- output sinks: choose the format the result is written in ------
    def _result_to_write(self, sink: str) -> Any:
        """The current result as an Arrow table. Works for a routed bespoke result and a DuckDB
        fallback alike: ``_materialize_current()`` pulls an open DuckDB fallback result
        (``_current is None``) into a ``SynnoResult``. Only DuckDB's "no open result set" - no
        result-producing ``execute()``/``sql()`` was run - is rewritten into a clear error;
        every other failure (e.g. an Arrow conversion error on a real result) propagates so it is
        never hidden behind a misleading "nothing to write"."""
        try:
            return self._materialize_current().to_arrow_table()
        except duckdb.InvalidInputException as exc:
            raise SynnoError(
                f"{sink}: no result to write - run execute()/sql() that returns rows first"
            ) from exc

    def write_parquet(self, path: Any, **kwargs: Any) -> None:
        """Write the current result to a parquet file (the ETL output format). Works for a
        routed bespoke result and a DuckDB fallback alike; the result is typed Arrow."""
        import pyarrow.parquet as pq

        pq.write_table(self._result_to_write("write_parquet"), str(path), **kwargs)

    def write_csv(self, path: Any, **kwargs: Any) -> None:
        """Write the current result to a CSV file."""
        import pyarrow.csv as pacsv

        pacsv.write_csv(self._result_to_write("write_csv"), str(path), **kwargs)

    # ---- interactive display -------------------------------------------
    def _materialize_current(self) -> Any:
        """The current result as a materialized ``SynnoResult``. A routed query already has one; a
        DuckDB fallback pulls its result into memory once (and routes later fetches through it) so
        it can be shown without re-running. Only ``show()``/``repr`` call this - the programmatic
        ``fetch*`` path stays lazy."""
        if self._current is None:
            from .result import SynnoResult

            inner = self._inner
            to_arrow = getattr(inner, "to_arrow_table", None) or inner.fetch_arrow_table
            object.__setattr__(self, "_current", SynnoResult(to_arrow()))
        return self._current

    def _footer(self) -> str:
        last = self._last
        if not last:
            return ""
        from .display import QueryTiming, format_footer

        est = None
        if (
            last.get("served_by") == "engine"
            and last.get("duckdb_ms") is None
            and last.get("template")
            and self._router is not None
        ):
            try:
                est = self._router.last_duckdb_ms(last["template"])
            except Exception:
                est = None
        return format_footer(
            QueryTiming(
                served_by=last.get("served_by", "duckdb"),
                engine_ms=last.get("engine_ms"),
                duckdb_ms=last.get("duckdb_ms"),
                duckdb_ms_estimated=est,
            )
        )

    def _render(self, *, max_rows: int = 20) -> str:
        from .display import render_table

        body = render_table(
            self._materialize_current().to_arrow_table(), max_rows=max_rows
        )
        footer = self._footer()
        return body + ("\n" + footer if footer else "")

    def show(self, *, max_rows: int = 20) -> None:
        """Print the current result as a table with a query-time / speedup footer (for interactive
        use). The programmatic ``fetch*`` / ``df`` / ``arrow`` methods are unchanged."""
        print(self._render(max_rows=max_rows))

    def __repr__(self) -> str:
        # In a REPL, ``con.execute(sql)`` returns the connection; render the result + timing. repr
        # must never raise, so fall back to a plain label on any error.
        try:
            if self._last is not None:
                return self._render()
        except Exception:
            pass
        return "<SynnoConnection (DuckDB drop-in router)>"

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
        # The cursor shares the router, its registry, and therefore its engines: it joins the
        # shared refcount so the engines outlive whichever of parent/cursor closes first.
        return SynnoConnection(
            self._inner.cursor(),
            self._router,
            owns_inner=True,
            engines_dir=self._engines_dir,
            mount=self._mount,
            owns_router=False,
            engine_refcount=self._engine_refcount,
            engine_threads=self._engine_threads,
        )

    def _close_engines(self) -> None:
        """Release the engines this connection discovered (warm subprocess + shm segments)."""
        registry = getattr(self._router, "registry", None)
        if registry is None:
            return
        seen: set = set()
        for binding in registry.bindings():
            engine = getattr(binding, "engine", None)
            if engine is None or id(engine) in seen:
                continue
            seen.add(id(engine))
            closer = getattr(engine, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    def close(self) -> None:
        # Release the shared engines only when this is the last live handle on the router, so
        # closing a parent never tears down engines a still-open cursor (or vice versa) is using.
        self._engine_refcount[0] -= 1
        if self._engine_refcount[0] <= 0:
            self._close_engines()
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
            return {
                "decision": "would-fall-back",
                "reason": "no router",
                "template": None,
                "guards": [],
                "placeholders": None,
                "normalized": None,
            }
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
            cols = (
                ",".join(f"{name}:{dtype}" for name, dtype in rows)
                if rows
                else "<missing>"
            )
            parts.append(f"{table}({cols})")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # ---- catch-all passthrough -----------------------------------------
    def __getattr__(self, name: str) -> Any:
        # Only invoked when normal attribute lookup fails -> delegate to DuckDB.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)
