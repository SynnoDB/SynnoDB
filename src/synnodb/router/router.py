"""The QueryRouter: per-statement decision pipeline with a fallback-always contract.

``route()`` decides whether a statement is served by a bespoke engine or by DuckDB,
and produces the bespoke result when it routes. **It never raises for a routing or
engine reason** (except in ``bespoke_only`` test mode) — every failure path returns a
``RouteDecision`` telling the caller to run DuckDB. Only a genuine DuckDB execution
error (from the caller's fallback) propagates, exactly as DuckDB would.

The pipeline: policy gate → empty-registry short-circuit → normalize → match → guards →
execute bespoke → (sampled) cross-check against DuckDB. With ``mode=off``, or with no
engines registered, the router short-circuits to DuckDB immediately (and without parsing),
guaranteeing byte-identical behavior at no per-query cost.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..errors import EngineDivergedError
from .adapt import results_diff, results_equal, to_synno_result
from .backend import DuckDBBackend
from .guards import GuardContext, evaluate
from .normalize import (
    bind_template,
    binding_groups,
    extract_literals,
    has_order_by,
    has_param_markers,
    is_read_only_query,
    merge_split,
    normalize_sql,
    order_by_key_indices,
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
    result: Optional[Any]  # SynnoResult when routed; None otherwise
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
        # Per-template count of bespoke executions reached, for burn-in verification (the first
        # verify_first_n executions of each template are always cross-checked).
        self._exec_counts: Dict[str, int] = {}
        self._rng = random.Random()
        # Best-effort session counters (advisory; not locked). Surfaced via stats().
        self.counters: Dict[str, int] = {
            "routed": 0,
            "fell_back": 0,
            "cross_checked": 0,
            "cross_check_mismatch": 0,
            "cross_check_error": 0,
            "blocked_writes": 0,
        }
        self._fallback_reasons: Dict[str, int] = {}
        # Last (engine_ms, duckdb_ms) per template from a cross-check, so a routed-but-not-sampled
        # query can still show an estimated speedup without re-running DuckDB.
        self._template_timing: Dict[str, Tuple[float, float]] = {}

    def last_duckdb_ms(self, template_id: str) -> Optional[float]:
        """The DuckDB time from this template's most recent cross-check, if any (for a speedup
        estimate). None if it has never been cross-checked."""
        pair = self._template_timing.get(template_id)
        return pair[1] if pair else None

    # ---- internal helpers ----------------------------------------------
    def _finish(self, trace: RouteTrace, decision: RouteDecision) -> RouteDecision:
        self._tally(trace)
        emit(trace, verbose=self.policy.verbose)
        return decision

    def _tally(self, trace: RouteTrace) -> None:
        c = self.counters
        if trace.decision == "bespoke":
            if trace.served_by == "duckdb":
                # The engine ran but we served DuckDB's verified result (a divergence or a
                # comparison failure). This is not a successful engine serve; record the
                # cross-check outcome instead of inflating the routed count.
                c["cross_checked"] += 1
                if trace.results_match is False:
                    c["cross_check_mismatch"] += 1
                return
            c["routed"] += 1
            if trace.cross_checked:
                c["cross_checked"] += 1
                if trace.results_match is False:
                    c["cross_check_mismatch"] += 1
        elif trace.decision == "fallback":
            c["fell_back"] += 1
            self._fallback_reasons[trace.reason] = (
                self._fallback_reasons.get(trace.reason, 0) + 1
            )

    def note_blocked_write(self) -> None:
        """Record a write the connection refused (writes are blocked at the connection)."""
        self.counters["blocked_writes"] += 1

    def stats(self) -> Dict[str, Any]:
        """A snapshot of the session routing counters plus the fallback-reason breakdown."""
        return {**self.counters, "fallback_reasons": dict(self._fallback_reasons)}

    def _fallback(
        self, trace: RouteTrace, reason: str, *, matched: bool = False
    ) -> RouteDecision:
        trace.fell_back(reason)
        if matched and self.policy.mode is RouterMode.BESPOKE_ONLY:
            emit(trace, verbose=self.policy.verbose)
            raise RuntimeError(f"bespoke_only: routing failed ({reason})")
        return self._finish(trace, RouteDecision(False, None, trace))

    def _should_cross_check(self, template_id: str) -> bool:
        pol = self.policy
        if pol.mode is RouterMode.BESPOKE_ONLY:
            return False
        rate = pol.cross_check_rate
        if rate <= 0.0:
            # The operator has explicitly opted out of all verification; burn-in is skipped too.
            return False
        # Burn-in: always verify a template's first verify_first_n executions, so a brand-new (or
        # freshly republished) engine cannot serve a single unverified result before proving out.
        if self._exec_counts.get(template_id, 0) <= pol.verify_first_n:
            return True
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
        specs = binding.placeholders
        if parameters is not None:
            values = (
                list(parameters)
                if isinstance(parameters, (list, tuple))
                else [parameters]
            )
            # One value per binding group (`?`), which may unpack into several engine parameters
            # when a literal packs them (Q13). A LIKE affix carries the whole pattern (`%BRASS`);
            # merge_split peels it so the engine receives the parameter it expects (`BRASS`).
            groups = binding_groups(specs)
            if len(values) != len(groups):
                return None
            return merge_split(groups, values)
        # Templates with explicit ?/$name markers bind by matching against the template,
        # which separates constants from parameters and handles repeated placeholders. A
        # concrete-example template (literals stand in for parameters) or a legacy binding
        # without template_sql uses positional literal extraction.
        if binding.template_sql is not None and has_param_markers(binding.template_sql):
            return bind_template(binding.template_sql, sql, specs)
        # The positional path binds whole literals only. A spec embedded in a literal
        # (affix or group) needs the template to peel its constants; binding it whole
        # would hand the engine the raw pattern ('%y' instead of y), so refuse.
        if any(s.prefix or s.suffix or s.group >= 0 for s in specs):
            return None
        names = [spec.name for spec in specs]
        values = extract_literals(sql)
        return {
            name: (values[i] if i < len(values) else None)
            for i, name in enumerate(names)
        }

    def _record_failure(self, binding: EngineBinding) -> None:
        count = self._failures.get(binding.template_id, 0) + 1
        self._failures[binding.template_id] = count
        if count >= self.policy.breaker_threshold and not self.registry.is_quarantined(
            binding.template_id
        ):
            # Make the breaker trip visible: the engine is now permanently sidelined for this
            # session, so a query the operator expects to be accelerated will always fall back.
            logger.warning(
                "engine %s quarantined after %d consecutive failures (template %s); this query "
                "now always falls back to DuckDB until the engine is re-registered",
                binding.engine_id,
                count,
                binding.template_id,
            )
            self.registry.quarantine(binding.template_id)

    def _record_success(self, binding: EngineBinding) -> None:
        self._failures.pop(binding.template_id, None)

    def _serve_reference(
        self, trace: RouteTrace, reference: Any, binding: EngineBinding
    ) -> RouteDecision:
        """Serve the trusted DuckDB reference already computed during the cross-check, instead of
        the engine result (on a divergence or a comparison failure). ``decision.routed`` stays True
        because the caller should use this result rather than re-running DuckDB, but ``served_by`` is
        marked ``duckdb`` so the timing footer and counters report DuckDB honestly - never a bogus
        engine speedup for a result the engine did not actually produce."""
        trace.served_by = "duckdb"
        result = to_synno_result(reference, binding.output_schema)
        return self._finish(trace, RouteDecision(True, result, trace))

    # ---- the pipeline ---------------------------------------------------
    def route(self, sql: str, parameters: Any, conn: Any) -> RouteDecision:
        trace = RouteTrace(sql=sql)
        pol = self.policy

        # 1. kill switch / mode gate — fastest path, guarantees zero-config==DuckDB.
        if not pol.routing_active:
            reason = "router disabled" if not pol.enabled else f"mode={pol.mode}"
            trace.fell_back(reason)
            return self._finish(trace, RouteDecision(False, None, trace))

        # 1a. Nothing registered -> nothing can route. Short-circuit before parsing so an
        #     engine-less connection pays no per-query cost and stays byte-identical to DuckDB.
        if len(self.registry) == 0:
            trace.fell_back("no engines registered")
            return self._finish(trace, RouteDecision(False, None, trace))

        # Writes are refused at the connection (SynnoConnection.execute) while writes are
        # disabled, so a write does not reach route() on the normal path. The previous
        # write -> DuckDB passthrough (mark bound tables dirty, then run on DuckDB) is kept
        # here, disabled, to re-enable when write support lands:
        #
        #   if statement_kind(sql) == "write":
        #       stale = tuple(tables_in(sql))
        #       if stale:
        #           self.registry.mark_tables_dirty(stale)
        #       trace.write_passthrough("write/DDL not accelerated")
        #       return self._finish(trace, RouteDecision(False, None, trace, stale_tables=stale))

        # 2. normalize to a structural key.
        normalized = normalize_sql(sql)
        if normalized is None:
            return self._fallback(trace, "unparseable SQL")

        # 3. match a registered template.
        binding = self.registry.match(normalized)
        if binding is None:
            return self._fallback(trace, "no template match")

        # 4. guards (engine readiness, SELECT-only, dirty tables, schema, arity).
        ctx = GuardContext(
            sql=sql,
            binding=binding,
            conn=conn,
            registry=self.registry,
            parameters=parameters,
        )
        ok, results = evaluate(ctx)
        for name, passed, detail in results:
            trace.add_guard(name, passed, detail)
        if not ok:
            return self._fallback(
                trace, results[-1][2] if results else "guard failed", matched=True
            )

        # 5. bind the engine's parameters by matching the query against the template. None
        #    means it matched the structural key but is not actually this template (a
        #    differing constant, a repeated placeholder with two values, a structural
        #    difference), so fall back rather than run the engine with the wrong values.
        placeholders = self._bind_placeholders(binding, sql, parameters)
        if placeholders is None:
            return self._fallback(
                trace,
                "placeholder binding failed (constant/structure mismatch)",
                matched=True,
            )

        # 5b. execute bespoke.
        start = time.perf_counter()
        try:
            table = binding.engine.run(binding.query_id, placeholders)
        except Exception as exc:  # engine fault must never crash the user's query
            # Surface the FIRST fault per template at WARNING: a registered engine that crashes
            # silently degrades to DuckDB, so without this the operator has no signal that the
            # bespoke binary they built is not accelerating. Repeats stay DEBUG to avoid spam; the
            # full traceback is always at DEBUG. (EngineExecutionError already carries the engine's
            # own diagnostics - stderr, req id - in its message.)
            first_fault = binding.template_id not in self._failures
            (logger.warning if first_fault else logger.debug)(
                "bespoke engine error (template=%s query_id=%s): %s",
                binding.template_id,
                binding.query_id,
                exc,
            )
            logger.debug(
                "bespoke engine error traceback (template=%s)",
                binding.template_id,
                exc_info=True,
            )
            self._record_failure(binding)
            return self._fallback(trace, f"engine error: {exc!r}", matched=True)
        trace.bespoke_ms = (time.perf_counter() - start) * 1000.0
        trace.routed(binding.template_id)
        self._exec_counts[binding.template_id] = (
            self._exec_counts.get(binding.template_id, 0) + 1
        )

        # 6. cross-check against DuckDB (correctness + speedup). Sampled by cross_check_rate, with a
        #    burn-in: a template's first verify_first_n executions are always checked. The invariant
        #    here is FAIL-CLOSED - whenever a check is attempted we never serve an unverified engine
        #    result. If we hold the trusted DuckDB reference we serve THAT on any doubt (divergence
        #    or a comparison error); if we could not even obtain a reference we fall back so the
        #    caller runs DuckDB itself, exactly as an un-routed query would.
        if self._should_cross_check(binding.template_id) and conn is not None:
            try:
                backend = DuckDBBackend(conn.duckdb)
                start = time.perf_counter()
                reference = backend.execute_arrow(sql, parameters)
                trace.duckdb_ms = (time.perf_counter() - start) * 1000.0
            except Exception as exc:
                # No trusted reference: DuckDB itself failed on the user's query. Do not serve the
                # engine result unverified - fall back so the caller runs DuckDB and surfaces the
                # genuine error (or a transient recovery). Honors the "any doubt -> DuckDB" contract.
                self.counters["cross_check_error"] += 1
                logger.warning(
                    "cross-check reference execution failed for template=%s; falling back to DuckDB "
                    "instead of serving the engine result unverified: %s",
                    binding.template_id,
                    exc,
                )
                return self._fallback(
                    trace, f"cross-check reference error: {exc!r}", matched=True
                )

            trace.cross_checked = True
            if (
                trace.bespoke_ms and trace.duckdb_ms
            ):  # remember for later speedup estimates
                self._template_timing[binding.template_id] = (
                    trace.bespoke_ms,
                    trace.duckdb_ms,
                )
            ordered = has_order_by(sql)
            order_keys = (
                order_by_key_indices(sql, reference.column_names) if ordered else None
            )
            try:
                match = results_equal(
                    table, reference, ordered=ordered, order_keys=order_keys
                )
            except Exception as exc:
                # The reference is in hand but the comparison itself failed. Serve the VERIFIED
                # DuckDB result (never the unverified engine result, as the old fail-open did) and
                # record an engine failure so a persistently un-comparable engine trips the breaker.
                self.counters["cross_check_error"] += 1
                logger.warning(
                    "cross-check comparison failed for template=%s; serving the verified DuckDB "
                    "result and recording an engine failure: %s",
                    binding.template_id,
                    exc,
                )
                trace.add_guard(
                    "cross_check", False, f"cross-check comparison error: {exc!r}"
                )
                self._record_failure(binding)
                return self._serve_reference(trace, reference, binding)

            trace.results_match = match
            if not match:
                # The engine disagreed with DuckDB. Surface exactly which cells/rows diverged (a
                # silent quarantine hides a correctness bug from the operator), quarantine the
                # template, and serve the trusted DuckDB result.
                diffs, total = results_diff(
                    table, reference, ordered=ordered, order_keys=order_keys
                )
                diverged = EngineDivergedError(
                    diffs,
                    engine_id=binding.engine_id,
                    query_id=binding.query_id,
                    total=total,
                )
                logger.warning("%s", diverged)
                trace.add_guard("cross_check", False, str(diverged))
                self.registry.quarantine(binding.template_id)
                return self._serve_reference(trace, reference, binding)

        # Served by the engine: either not sampled this time, or sampled and verified equal.
        self._record_success(binding)
        trace.served_by = "engine"
        result = to_synno_result(table, binding.output_schema)
        return self._finish(trace, RouteDecision(True, result, trace))

    # ---- inspection -----------------------------------------------------
    def why(self, sql: str, parameters: Any = None, conn: Any = None) -> Dict[str, Any]:
        """Explain how *sql* would be handled, without executing anything.

        Runs the same decision steps as ``route`` (mode gate, write check, normalize, match,
        guards, placeholder bind) but never calls the engine or DuckDB. Returns a dict with
        ``decision`` (``would-route`` / ``would-fall-back`` / ``blocked``), a ``reason``, the
        matched ``template``, the ``guards`` evaluated, the bound ``placeholders``, and the
        ``normalized`` key. The answer to "why is my query not accelerated?".
        """
        pol = self.policy
        out: Dict[str, Any] = {
            "decision": "would-fall-back",
            "reason": "",
            "template": None,
            "guards": [],
            "placeholders": None,
            "normalized": None,
        }
        if not pol.routing_active:
            out["reason"] = "router disabled" if not pol.enabled else f"mode={pol.mode}"
            return out
        if pol.block_writes and not is_read_only_query(sql):
            out["decision"], out["reason"] = "blocked", "writes are not supported"
            return out
        if len(self.registry) == 0:
            out["reason"] = "no engines registered"
            return out
        normalized = normalize_sql(sql)
        out["normalized"] = normalized
        if normalized is None:
            out["reason"] = "unparseable SQL"
            return out
        binding = self.registry.match(normalized)
        if binding is None:
            quarantined = self.registry.quarantined_binding(normalized)
            if quarantined is not None:
                out["template"] = quarantined.template_id
                out["reason"] = (
                    "engine quarantined after repeated failures or a cross-check "
                    "mismatch; DuckDB is serving this query until it is re-registered"
                )
                return out
            out["reason"] = "no template match"
            return out
        out["template"] = binding.template_id
        ctx = GuardContext(
            sql=sql,
            binding=binding,
            conn=conn,
            registry=self.registry,
            parameters=parameters,
        )
        ok, results = evaluate(ctx)
        out["guards"] = [{"name": n, "ok": p, "detail": d} for n, p, d in results]
        if not ok:
            out["reason"] = results[-1][2] if results else "guard failed"
            return out
        placeholders = self._bind_placeholders(binding, sql, parameters)
        if placeholders is None:
            out["reason"] = "placeholder binding failed (constant/structure mismatch)"
            return out
        out["decision"], out["reason"], out["placeholders"] = (
            "would-route",
            "matches template",
            placeholders,
        )
        return out
