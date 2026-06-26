"""Routing guards: the stack of assertions a query must pass to be bespoke-served.

Each guard is a pure function ``(GuardContext) -> (ok, detail)``. They are evaluated
in order; the first failure stops evaluation and the query falls back to DuckDB.
Every guard result (pass or the first fail) is recorded on the ``RouteTrace`` so a
fallback always says *why*.

The guarantee: a guard failure is never an error — it is a fallback. Guards exist to
keep the bespoke path inside the envelope an engine was built and validated for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from .normalize import extract_literals, is_select
from .registry import EngineBinding, TemplateRegistry

GuardOutcome = Tuple[bool, str]


@dataclass
class GuardContext:
    sql: str
    binding: EngineBinding
    conn: Any                        # SynnoConnection (exposes .duckdb, schema introspection)
    registry: TemplateRegistry
    parameters: Optional[Any] = None  # bound params, if the user passed them


Guard = Callable[[GuardContext], GuardOutcome]


def engine_ready_guard(ctx: GuardContext) -> GuardOutcome:
    if not ctx.binding.runnable:
        return False, "no live engine worker bound"
    health = getattr(ctx.binding.engine, "health", None)
    if callable(health):
        try:
            if not health():
                return False, "engine unhealthy"
        except Exception as exc:
            return False, f"engine health check failed: {exc}"
    return True, "engine ready"


def select_only_guard(ctx: GuardContext) -> GuardOutcome:
    if is_select(ctx.sql):
        return True, "SELECT"
    return False, "not a plain SELECT"


def placeholder_arity_guard(ctx: GuardContext) -> GuardOutcome:
    """The number of bound values must match the template's placeholder count."""
    expected = len(ctx.binding.placeholders)
    if ctx.parameters is not None:
        params = ctx.parameters
        actual = len(params) if isinstance(params, (list, tuple)) else 1
    else:
        actual = len(extract_literals(ctx.sql))
    if actual == expected:
        return True, f"{actual} placeholders"
    return False, f"placeholder arity {actual} != expected {expected}"


def dirty_table_guard(ctx: GuardContext) -> GuardOutcome:
    if ctx.registry.is_dirty(ctx.binding):
        return False, "a bound table was modified since ingest"
    return True, "tables clean"


def schema_match_guard(ctx: GuardContext) -> GuardOutcome:
    """Compare the engine's build-time schema fingerprint to the live one.

    Phase 2 computes the live fingerprint from the wrapped DuckDB connection. Until a
    live fingerprint is available, this passes with an explicit note (an engine with
    no fingerprint cannot be schema-bound yet, so it simply won't be registered).
    """
    live = getattr(ctx.conn, "schema_fingerprint", None)
    if callable(live):
        try:
            current = live(ctx.binding.tables)
        except Exception as exc:  # never raise out of a guard
            return False, f"schema introspection failed: {exc}"
        if current != ctx.binding.schema_fingerprint:
            return False, "schema fingerprint mismatch vs engine build"
        return True, "schema matches"
    return True, "schema check not enforced (no live fingerprint)"


# Order matters: cheapest / most-common-fail first.
DEFAULT_GUARDS: Tuple[Guard, ...] = (
    engine_ready_guard,
    select_only_guard,
    dirty_table_guard,
    schema_match_guard,
    placeholder_arity_guard,
)


def evaluate(
    ctx: GuardContext, guards: Tuple[Guard, ...] = DEFAULT_GUARDS
) -> Tuple[bool, List[Tuple[str, bool, str]]]:
    """Run guards in order, stopping at the first failure.

    Returns ``(all_passed, results)`` where ``results`` is the list of
    ``(name, ok, detail)`` evaluated so far (for the trace).
    """
    results: List[Tuple[str, bool, str]] = []
    for guard in guards:
        try:
            ok, detail = guard(ctx)
        except Exception as exc:  # a buggy guard must fall back, never crash
            results.append((guard.__name__, False, f"guard raised: {exc}"))
            return False, results
        results.append((guard.__name__, ok, detail))
        if not ok:
            return False, results
    return True, results
