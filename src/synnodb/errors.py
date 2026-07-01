"""SynnoDB error types - every failure a user can hit is one of these, and every one renders a
verbose, contextual, actionable message.

The router's contract is that a query is either served bit-identically to DuckDB or it is refused
or falls back with a clear reason; these exceptions carry the *why* so a bare ``pyarrow`` /
``duckdb`` traceback never reaches the user. They are intentionally cheap (no heavy imports) so the
light drop-in surface can raise them.

Hierarchy::

    SynnoError
      SynnoUnsupportedQuery   - a query cannot be routed exactly (types / nullability / shape)
      EngineExecutionError    - the bespoke engine subprocess failed to produce a usable result
      EngineDivergedError     - the engine's result disagreed with DuckDB on the cross-check
      EngineResourceError     - a data-plane resource problem (shm / tmpfs / subprocess)

``SynnoUnsupportedQuery`` and ``EngineDivergedError`` are recoverable by definition: the router
catches them, keeps the engine unregistered or quarantined, and serves DuckDB. They are surfaced to
the user through logs and the ``why()`` API rather than raised into ``execute()``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


class SynnoError(Exception):
    """Base for every SynnoDB-specific error. Subclasses build a verbose ``message`` and may carry
    structured fields for programmatic handling and for the ``why()`` API."""


def _bullets(reasons: Sequence[str]) -> str:
    return "\n".join(f"  - {r}" for r in reasons)


class SynnoUnsupportedQuery(SynnoError):
    """A query cannot be served bit-identically to DuckDB, so it is not routed (DuckDB serves it).

    Raised by the bind/generation exactness guard. ``reasons`` lists *every* offending column or
    property, each already phrased as a self-contained explanation with its remedy.
    """

    def __init__(
        self,
        reasons: Sequence[str],
        *,
        engine_id: Optional[str] = None,
        query_id: Optional[str] = None,
    ) -> None:
        self.reasons: List[str] = list(reasons)
        self.engine_id = engine_id
        self.query_id = query_id
        who = " / ".join(
            p for p in (engine_id, f"query {query_id}" if query_id else None) if p
        )
        head = f"{who}: " if who else ""
        n = len(self.reasons)
        super().__init__(
            f"{head}cannot route this query - {n} column(s)/property outside the exact envelope:\n"
            f"{_bullets(self.reasons)}\n"
            "  Action: this query is served by DuckDB (correct, just not accelerated)."
        )


class EngineExecutionError(SynnoError):
    """The bespoke engine ran but did not produce a usable result (crash, timeout, error channel,
    truncated/missing output). Carries the engine's own diagnostics so the failure is debuggable
    instead of surfacing as a raw pyarrow/IO error."""

    def __init__(
        self,
        message: str,
        *,
        engine_id: Optional[str] = None,
        query_id: Optional[str] = None,
        req_id: Optional[str] = None,
        response: Any = None,
        stderr: Optional[str] = None,
    ) -> None:
        self.engine_id = engine_id
        self.query_id = query_id
        self.req_id = req_id
        self.response = response
        self.stderr = stderr
        who = " / ".join(
            p
            for p in (
                engine_id,
                f"query {query_id}" if query_id else None,
                f"req {req_id}" if req_id else None,
            )
            if p
        )
        head = f"{who}: " if who else ""
        tail = ""
        if response is not None:
            tail += f"\n  engine response: {response!r}"
        if stderr:
            tail += f"\n  engine stderr (tail):\n{_indent(stderr[-2000:])}"
        super().__init__(f"{head}{message}{tail}")


class EngineDivergedError(SynnoError):
    """The engine's result disagreed with DuckDB on the cross-check. The engine is quarantined and
    DuckDB serves the query; this records exactly where they differed so the engine can be fixed."""

    # (row_index, column_name, engine_value, duckdb_value)
    Diff = Tuple[int, str, Any, Any]

    def __init__(
        self,
        diffs: Sequence["EngineDivergedError.Diff"],
        *,
        engine_id: Optional[str] = None,
        query_id: Optional[str] = None,
        total: Optional[int] = None,
    ) -> None:
        self.diffs = list(diffs)
        self.engine_id = engine_id
        self.query_id = query_id
        self.total = total if total is not None else len(self.diffs)
        who = " / ".join(
            p for p in (engine_id, f"query {query_id}" if query_id else None) if p
        )
        head = f"{who} " if who else ""
        shown = "\n".join(
            f"  - row {r}, column '{c}': engine={e!r} duckdb={d!r}"
            for (r, c, e, d) in self.diffs
        )
        more = ""
        if self.total > len(self.diffs):
            more = f"\n  ... and {self.total - len(self.diffs)} more differing cell(s)"
        super().__init__(
            f"{head}DIVERGED from DuckDB on {self.total} cell(s):\n{shown}{more}\n"
            "  Engine quarantined; DuckDB serves this query."
        )


class EngineResourceError(SynnoError):
    """A data-plane resource failure: not enough shared memory / tmpfs for a hot-load ingest, a
    subprocess that could not be started, an unwritable result directory, and so on."""

    def __init__(self, message: str, *, context: Optional[dict] = None) -> None:
        self.context = dict(context or {})
        ctx = ""
        if self.context:
            ctx = "\n" + _bullets([f"{k}: {v}" for k, v in self.context.items()])
        super().__init__(f"{message}{ctx}")


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
