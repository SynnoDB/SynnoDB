"""Observability for the router: a structured per-query decision record + logging.

Verbosity is bounded by design: a one-line INFO summary is emitted only for queries
that were *routed* or *cross-checked* (the interesting ones). Plain fallbacks are
logged at DEBUG so a production loop is not spammed. Full detail is always available
at DEBUG as the trace's ``as_dict()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("synnodb.router")

# Guard outcomes: (name, ok, detail)
GuardResult = Tuple[str, bool, str]


def _short_sql(sql: str, limit: int = 120) -> str:
    one_line = " ".join(sql.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


@dataclass
class RouteTrace:
    """Everything decided about one statement. Built incrementally during routing."""

    sql: str
    decision: str = "pending"            # "bespoke" | "fallback" | "write-passthrough"
    reason: str = ""                     # human-readable cause
    template: Optional[str] = None
    guard_results: List[GuardResult] = field(default_factory=list)
    bespoke_ms: Optional[float] = None
    duckdb_ms: Optional[float] = None
    cross_checked: bool = False
    results_match: Optional[bool] = None  # cross-check verdict, when run

    # ---- mutation helpers (return self for fluent use) ------------------
    def fell_back(self, reason: str) -> "RouteTrace":
        self.decision = "fallback"
        self.reason = reason
        return self

    def write_passthrough(self, reason: str) -> "RouteTrace":
        self.decision = "write-passthrough"
        self.reason = reason
        return self

    def routed(self, template: str, reason: str = "template match") -> "RouteTrace":
        self.decision = "bespoke"
        self.template = template
        self.reason = reason
        return self

    def add_guard(self, name: str, ok: bool, detail: str = "") -> "RouteTrace":
        self.guard_results.append((name, ok, detail))
        return self

    @property
    def speedup(self) -> Optional[float]:
        if self.bespoke_ms and self.duckdb_ms and self.bespoke_ms > 0:
            return self.duckdb_ms / self.bespoke_ms
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "template": self.template,
            "guards": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.guard_results],
            "bespoke_ms": self.bespoke_ms,
            "duckdb_ms": self.duckdb_ms,
            "cross_checked": self.cross_checked,
            "results_match": self.results_match,
            "speedup": self.speedup,
            "sql": _short_sql(self.sql),
        }

    def summary_line(self) -> str:
        if self.decision == "bespoke":
            head = f"routed {self.template or '?'}"
            if self.cross_checked:
                match = "✓" if self.results_match else "✗ MISMATCH"
                sp = f"{self.speedup:.1f}×" if self.speedup else "?"
                return (
                    f"{head}: bespoke {self.bespoke_ms:.1f}ms vs duckdb "
                    f"{self.duckdb_ms:.1f}ms → {sp} speedup, results {match}"
                )
            ms = f"{self.bespoke_ms:.1f}ms" if self.bespoke_ms is not None else "?"
            return f"{head}: bespoke {ms}"
        if self.decision == "write-passthrough":
            return f"write/DDL → DuckDB ({self.reason}); engines marked stale"
        return f"fallback → DuckDB ({self.reason})"


_DEBUG_HANDLER_TAG = "synnodb-debug-handler"


def enable_debug_logging(level: int = logging.DEBUG, stream=None) -> None:
    """Turn on verbose SynnoDB logging — the fast way to chase routing/engine/shm errors.

        import synnodb
        synnodb.enable_debug_logging()   # then run your queries; every decision,
                                         # guard, engine call and shm segment is logged

    Idempotent (won't stack handlers). Also nudges the worker subprocess to log
    (``SYNNODB_WORKER_LOG``) so its failures surface on stderr.
    """
    import os

    root = logging.getLogger("synnodb")
    if not any(getattr(h, "_synnodb_tag", None) == _DEBUG_HANDLER_TAG for h in root.handlers):
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
        handler._synnodb_tag = _DEBUG_HANDLER_TAG  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    os.environ.setdefault("SYNNODB_WORKER_LOG", logging.getLevelName(level))


def emit(trace: RouteTrace, *, verbose: bool) -> None:
    """Log a finished trace at the right level."""
    interesting = trace.decision in ("bespoke", "write-passthrough")
    if trace.results_match is False:
        # A correctness divergence is always loud.
        logger.warning("router: %s :: %s", trace.summary_line(), _short_sql(trace.sql))
    elif interesting and verbose:
        logger.info("router: %s", trace.summary_line())
    else:
        logger.debug("router: %s :: %s", trace.summary_line(), _short_sql(trace.sql))
    logger.debug("router-detail: %s", trace.as_dict())
