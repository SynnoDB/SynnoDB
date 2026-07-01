"""Router policy: the single, immutable knob set that governs routing.

A ``RouterPolicy`` is attached to each ``SynnoConnection``. It is deliberately
conservative by default and fully overridable from the environment, so a user can
disable or tune routing without touching code. The top-level invariant it serves:
**with no engines (or ``mode=off``) behavior is byte-identical to DuckDB.**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import FrozenSet, Optional


class RouterMode(str, Enum):
    """How the router treats a query that matches a registered template.

    Fallback to DuckDB is unconditional in every mode when there is no match, no
    healthy engine, or a failing guard — the mode only decides what happens on the
    *happy* path.
    """

    OFF = "off"  # never route; pure DuckDB passthrough
    SAMPLED = "sampled"  # serve bespoke; cross-check a fraction against DuckDB
    BESPOKE_ONLY = "bespoke_only"  # tests only: raise instead of falling back

    def __str__(self) -> str:  # nicer logs / reprs
        return self.value


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    low = raw.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RouterPolicy:
    """Immutable routing configuration (per connection)."""

    # Default SAMPLED: routing is live, and with no engines registered every query still
    # falls back, so behavior stays byte-identical to DuckDB until an engine appears. The
    # router additionally skips all parsing when nothing is registered (see route()), so an
    # engine-less connection pays no per-query cost. SYNNODB_ROUTER=off disables it entirely.
    mode: RouterMode = RouterMode.SAMPLED
    enabled: bool = True  # hard kill switch (env SYNNODB_ROUTER=off)

    # Writes are not supported yet: a non-read statement on a SynnoConnection raises rather
    # than running on DuckDB. Flip to False (or SYNNODB_BLOCK_WRITES=off) to pass writes
    # through to DuckDB unaccelerated.
    block_writes: bool = True

    cross_check_rate: float = 0.1  # fraction of routed queries also run on DuckDB
    # Burn-in: always cross-check the first N executions of each template, regardless of
    # cross_check_rate, so a freshly built (or freshly republished) engine cannot serve a single
    # unverified result before it has proven itself. A systematically wrong engine is then caught
    # and quarantined on its first queries instead of leaking wrong answers until a sampled check
    # happens to hit one. Set to 0 to disable burn-in (pure sampling). When cross_check_rate is 0
    # the operator has explicitly opted out of all verification, and burn-in is skipped too.
    verify_first_n: int = 10
    select_only: bool = True  # only SELECT-family statements may route
    require_schema_match: bool = True
    require_sf_match: bool = True

    engine_timeout_ms: int = 30_000
    breaker_threshold: int = 3  # consecutive failures before quarantine
    max_result_bytes: int = 512 * 1024 * 1024  # result-arena ceiling; over → fall back

    allow_templates: Optional[FrozenSet[str]] = None  # None = all registered
    deny_templates: FrozenSet[str] = field(default_factory=frozenset)

    verbose: bool = True  # one-line per routed/cross-checked query

    def with_(self, **overrides) -> "RouterPolicy":
        """A derived policy (immutable update)."""
        if "mode" in overrides and isinstance(overrides["mode"], str):
            overrides["mode"] = RouterMode(overrides["mode"])
        return replace(self, **overrides)

    @property
    def routing_active(self) -> bool:
        """True when the router should even look at a query."""
        return self.enabled and self.mode is not RouterMode.OFF

    @classmethod
    def from_env(cls, **overrides) -> "RouterPolicy":
        """Build a policy from defaults, the environment, then explicit overrides.

        Recognized env vars:
          ``SYNNODB_ROUTER``        off/on   -> mode / kill switch
          ``SYNNODB_VERBOSE``       on/off   -> verbose
          ``SYNNODB_CROSS_CHECK``   float    -> cross_check_rate
          ``SYNNODB_VERIFY_FIRST_N`` int     -> verify_first_n (burn-in)
          ``SYNNODB_BLOCK_WRITES``  on/off   -> block_writes
        """
        base = cls()
        router_env = os.getenv("SYNNODB_ROUTER")
        mode = base.mode
        enabled = base.enabled
        if router_env is not None:
            low = router_env.strip().lower()
            if low in _FALSE or low == "off":
                enabled, mode = False, RouterMode.OFF
            elif low in {m.value for m in RouterMode}:
                mode = RouterMode(low)
            elif low in _TRUE:
                mode = RouterMode.SAMPLED
        policy = replace(
            base,
            mode=mode,
            enabled=enabled,
            verbose=_env_bool("SYNNODB_VERBOSE", base.verbose),
            cross_check_rate=_env_float("SYNNODB_CROSS_CHECK", base.cross_check_rate),
            verify_first_n=_env_int("SYNNODB_VERIFY_FIRST_N", base.verify_first_n),
            block_writes=_env_bool("SYNNODB_BLOCK_WRITES", base.block_writes),
        )
        return policy.with_(**overrides) if overrides else policy
