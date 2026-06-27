"""The QueryRouter: per-statement decision pipeline with a fallback-always contract.

``route()`` decides whether a statement is served by a bespoke engine or by DuckDB,
and produces the bespoke result when it routes. **It never raises for a routing or
engine reason** (except in ``bespoke_only`` test mode) — every failure path returns a
``RouteDecision`` telling the caller to run DuckDB. Only a genuine DuckDB execution
error (from the caller's fallback) propagates, exactly as DuckDB would.

The pipeline: policy gate → read-only block → normalize → match → guards → execute
bespoke → (sampled) cross-check against DuckDB. With the default ``mode=off`` the
router short-circuits to DuckDB immediately, guaranteeing byte-identical behavior.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .adapt import results_equal, to_synno_result
from .backend import DuckDBBackend
from .guards import GuardContext, evaluate
from .normalize import (
    extract_literals,
    has_order_by,
    has_param_markers,
    normalize_sql,
    statement_kind,
    tables_in,
    unify_and_bind,
)
from .observe import RouteTrace, emit, logger
from .policy import RouterMode, RouterPolicy
from .registry import EngineBinding, TemplateRegistry


@dataclass
class RouteDecision:
    """Outcome of ``QueryRouter.route``.

    ``routed=True`` means a bespoke ``result`` is attached and the caller should use
    it; ``routed=False`` means the caller must execute the statement on DuckDB.
    """

    routed: bool
    result: Optional[Any]            # SynnoResult when routed; None otherwise
    trace: RouteTrace
    stale_tables: Tuple[str, ...] = field(default_factory=tuple)


class QueryRouter:
    def __init__(
        self,
        policy: Optional[RouterPolicy] = None,
        registry: Optional[TemplateRegistry] = None,
    ) -> None:
        self.policy = policy or RouterPolicy()
        self.registry = registry if registry is not None else TemplateRegistry()
        self._failures: Dict[str, int] = {}
        self._rng = random.Random()

    # ---- internal helpers ----------------------------------------------
    def _finish(self, trace: RouteTrace, decision: RouteDecision) -> RouteDecision:
        emit(trace, verbose=self.policy.verbose)
        return decision

    def _fallback(self, trace: RouteTrace, reason: str, *, matched: bool = False) -> RouteDecision:
        trace.fell_back(reason)
        if matched and self.policy.mode is RouterMode.BESPOKE_ONLY:
            emit(trace, verbose=self.policy.verbose)
            raise RuntimeError(f"bespoke_only: routing failed ({reason})")
        return self._finish(trace, RouteDecision(False, None, trace))

    def _should_cross_check(self) -> bool:
        pol = self.policy
        if pol.mode is RouterMode.BESPOKE_ONLY:
            return False
        rate = pol.cross_check_rate
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return self._rng.random() < rate

    def _bind_placeholders(
        self, binding: EngineBinding, sql: str, parameters: Any
    ) -> Optional[Dict[str, Any]]:
        """Resolve the engine's named placeholders for this incoming query.

        With explicit DuckDB-style parameters, map them positionally to the template's
        placeholders (repeats must agree). Otherwise bind the query's inline literals by
        matching it against the template, which keeps a constant in the template from
        being read as a parameter and binds a repeated placeholder consistently. Returns
        ``None`` when the query does not match the template, and the router falls back.
        """
        names = [spec.name for spec in binding.placeholders]
        if parameters is not None:
            values = list(parameters) if isinstance(parameters, (list, tuple)) else [parameters]
            if len(values) != len(names):
                return None
            bound: Dict[str, Any] = {}
            for name, value in zip(names, values):
                if name in bound and bound[name] != value:
                    return None  # a repeated placeholder given two different values
                bound[name] = value
            return bound
        # Templates with explicit ?/$name markers bind by matching against the template,
        # which separates constants from parameters and handles repeated placeholders. A
        # concrete-example template (literals stand in for parameters) or a legacy binding
        # without template_sql uses positional literal extraction.
        if binding.template_sql is not None and has_param_markers(binding.template_sql):
            return unify_and_bind(binding.template_sql, sql, names)
        values = extract_literals(sql)
        return {name: (values[i] if i < len(values) else None) for i, name in enumerate(names)}

    def _record_failure(self, binding: EngineBinding) -> None:
        count = self._failures.get(binding.template_id, 0) + 1
        self._failures[binding.template_id] = count
        if count >= self.policy.breaker_threshold:
            self.registry.quarantine(binding.template_id)

    def _record_success(self, binding: EngineBinding) -> None:
        self._failures.pop(binding.template_id, None)

    # ---- the pipeline ---------------------------------------------------
    def route(self, sql: str, parameters: Any, conn: Any) -> RouteDecision:
        trace = RouteTrace(sql=sql)
        pol = self.policy

        # 1. kill switch / mode gate — fastest path, guarantees zero-config==DuckDB.
        if not pol.routing_active:
            reason = "router disabled" if not pol.enabled else f"mode={pol.mode}"
            trace.fell_back(reason)
            return self._finish(trace, RouteDecision(False, None, trace))

        # 1a. read-only block (v1): never accelerate mutations; DuckDB stays the truth.
        if statement_kind(sql) == "write":
            stale = tuple(tables_in(sql))
            if stale:
                self.registry.mark_tables_dirty(stale)
            trace.write_passthrough("write/DDL not accelerated (read-only v1)")
            return self._finish(trace, RouteDecision(False, None, trace, stale_tables=stale))

        # 2. normalize to a structural key.
        normalized = normalize_sql(sql)
        if normalized is None:
            return self._fallback(trace, "unparseable SQL")

        # 3. match a registered template.
        binding = self.registry.match(normalized)
        if binding is None:
            return self._fallback(trace, "no template match")

        # 4. guards (engine readiness, SELECT-only, dirty tables, schema, arity).
        ctx = GuardContext(sql=sql, binding=binding, conn=conn, registry=self.registry, parameters=parameters)
        ok, results = evaluate(ctx)
        for name, passed, detail in results:
            trace.add_guard(name, passed, detail)
        if not ok:
            return self._fallback(trace, results[-1][2] if results else "guard failed", matched=True)

        # 5. bind the engine's parameters by matching the query against the template. None
        #    means it matched the structural key but is not actually this template (a
        #    differing constant, a repeated placeholder with two values, a structural
        #    difference), so fall back rather than run the engine with the wrong values.
        placeholders = self._bind_placeholders(binding, sql, parameters)
        if placeholders is None:
            return self._fallback(trace, "placeholder binding failed (constant/structure mismatch)", matched=True)

        # 5b. execute bespoke.
        start = time.perf_counter()
        try:
            table = binding.engine.run(binding.query_id, placeholders)
        except Exception as exc:  # engine fault must never crash the user's query
            # Full traceback at DEBUG so a flaky engine is debuggable; the trace
            # reason (logged at INFO/WARNING via the fallback) names the cause.
            logger.debug(
                "bespoke engine error: template=%s query_id=%s placeholders=%s",
                binding.template_id, binding.query_id, placeholders, exc_info=True,
            )
            self._record_failure(binding)
            return self._fallback(trace, f"engine error: {exc!r}", matched=True)
        trace.bespoke_ms = (time.perf_counter() - start) * 1000.0
        trace.routed(binding.template_id)

        # 6. sampled cross-check against DuckDB (correctness + speedup).
        serve_table = table
        if self._should_cross_check() and conn is not None:
            try:
                backend = DuckDBBackend(conn.duckdb)
                start = time.perf_counter()
                reference = backend.execute_arrow(sql, parameters)
                trace.duckdb_ms = (time.perf_counter() - start) * 1000.0
                trace.cross_checked = True
                match = results_equal(table, reference, ordered=has_order_by(sql))
                trace.results_match = match
                if not match:
                    # Quarantine the template and serve the trusted DuckDB result.
                    self.registry.quarantine(binding.template_id)
                    serve_table = reference
            except Exception as exc:  # cross-check infra failure must not break the query
                trace.add_guard("cross_check", False, f"cross-check error: {exc!r}")

        self._record_success(binding)
        result = to_synno_result(serve_table, binding.output_schema)
        return self._finish(trace, RouteDecision(True, result, trace))
