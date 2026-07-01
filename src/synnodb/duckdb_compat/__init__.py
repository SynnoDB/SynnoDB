"""DuckDB-compatible drop-in surface for SynnoDB.

``import synnodb as duckdb`` works because this package re-exports DuckDB's entire
public namespace (exceptions, ``typing``, ``__version__``, type/relation classes …)
and overrides **only** the eager SQL-text entry points ``connect`` / ``sql`` /
``execute``. Everything else is literally DuckDB.

With the default policy (``mode=off`` until engines exist), this is byte-identical
to DuckDB — the router is inert and adds no behavior.
"""

from __future__ import annotations

from typing import Any, Optional

import duckdb as _duckdb

from ..router import QueryRouter, RouterPolicy, TemplateRegistry
from .connection import SynnoConnection
from .discovery import resolve_engines_dir
from .errors import WriteNotSupportedError
from .result import SynnoResult

# ---------------------------------------------------------------------------
# Re-export DuckDB's public surface, then override the few names we own.
# ---------------------------------------------------------------------------
_OWNED = {"connect", "sql", "execute"}
_reexported: list[str] = []
for _name in dir(_duckdb):
    if _name.startswith("__") or _name in _OWNED:
        continue
    globals()[_name] = getattr(_duckdb, _name)
    _reexported.append(_name)

# Mirror DuckDB's version so code/libraries that feature-gate on ``duckdb.__version__``
# keep working after the search-and-replace. SynnoDB's own version is separate.
__duckdb_version__ = getattr(_duckdb, "__version__", None)
__version__ = __duckdb_version__


# ---------------------------------------------------------------------------
# Owned entry points
# ---------------------------------------------------------------------------
def connect(
    database: str = ":memory:",
    read_only: bool = False,
    config: Optional[dict] = None,
    *,
    policy: Optional[RouterPolicy] = None,
    registry: Optional[TemplateRegistry] = None,
    engines: Any = None,
    mount: bool = False,
    **kwargs: Any,
) -> SynnoConnection:
    """Open a DuckDB connection wrapped by the SynnoDB router.

    ``database`` is passed straight to ``duckdb.connect`` with DuckDB's exact semantics
    (``":memory:"`` is a fresh empty in-memory database; a file path opens that file). A
    shm-capable engine hot-loads its tables as Arrow over shared memory regardless of whether
    the connection is in-memory or on a file, so no special flag is needed: ``connect("my.db",
    engines=...)`` routes matching queries to a ``synno-my`` engine that serves the file's data.

    ``policy`` / ``registry`` / ``engines`` / ``mount`` are SynnoDB extensions; everything else
    is passed straight to ``duckdb.connect``. With no registered engines (the default) this
    behaves exactly like ``duckdb.connect``.

    ``engines`` is the directory of published bespoke engines to auto-discover and route to
    (default: ``SYNNO_ENGINES_DIR`` or ``$SYNNO_DATA_DIR/engines``; ``None`` everywhere
    disables discovery). ``mount`` lets discovery expose an engine's own bundled snapshot as
    views when the connection has no such tables - querying the synthesized database with no
    DuckDB of your own.
    """
    config = config or {}
    inner = _duckdb.connect(
        database=database, read_only=read_only, config=config, **kwargs
    )
    router = QueryRouter(policy or RouterPolicy.from_env(), registry)
    # ``config={'threads': N}`` configures inner DuckDB (above, unchanged) AND fixes the thread
    # count of every routed bespoke engine, so a query served by the engine runs at the same
    # parallelism DuckDB would - exactly the DuckDB knob, applied end-to-end.
    engine_threads = config.get("threads")
    return SynnoConnection(
        inner,
        router,
        engines_dir=resolve_engines_dir(engines),
        mount=mount,
        engine_threads=int(engine_threads) if engine_threads is not None else None,
    )


_default_conn: Optional[SynnoConnection] = None


def default_connection() -> SynnoConnection:
    """The lazily-created default in-memory connection (mirrors ``duckdb``'s)."""
    global _default_conn
    if _default_conn is None:
        _default_conn = connect(":memory:")
    return _default_conn


def sql(query: str, *args: Any, **kwargs: Any):
    """Module-level ``sql`` on the default connection (relational; never routed)."""
    return default_connection().sql(query, *args, **kwargs)


def execute(query: str, parameters: Any = None):
    """Module-level eager ``execute`` on the default connection (may route)."""
    return default_connection().execute(query, parameters)


__all__ = [
    *_reexported,
    "connect",
    "sql",
    "execute",
    "default_connection",
    "SynnoConnection",
    "SynnoResult",
    "RouterPolicy",
    "TemplateRegistry",
    "QueryRouter",
    "WriteNotSupportedError",
    "__duckdb_version__",
]
