"""The curated, documented view of a run that conversation builders receive.

``ConvContext`` is what a :class:`~synnodb.plan.ConversationPlan`'s ``stages``
callable (and each ``PerQueryLoop.build``) gets to see: the run's identity
inputs (queries, workload, model, storage), the well-known workspace filenames,
and the run tool. Helpers wrap the reference-plan and exec-settings
bootstrapping lazily, so a builder that does not need them costs nothing.

The engine also uses the context as the runtime scratchpad for declarative
items: e.g. ``MeasureBaselines(into="single_threaded_rt_ms")`` stores the
measured baselines as ``ctx.single_threaded_rt_ms`` so later stage prompts can
close over them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from synnodb.conversations.filenames import Filenames
from synnodb.conversations.profiles import LanguageProfile, get_language_profile
from synnodb.utils.utils import DBStorage, EngineLang, is_persistent_storage

if TYPE_CHECKING:
    from synnodb.llm.sdk.sdk_wrapper import SDKWrapper
    from synnodb.tools.run import RunTool
    from synnodb.tools.validate.query_validator_class import QueryValidator
    from synnodb.workloads.workload_provider import ExecSettings, Workload
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

logger = logging.getLogger(__name__)


@dataclass
class ConvContext:
    """Everything a stage builder may draw on. One instance per run."""

    query_ids: list[str]
    filenames: Filenames
    workspace_path: Path
    db_storage: DBStorage
    threads: int
    model: str
    run_tool: "RunTool"
    workload_provider: "OLAPWorkloadProvider"
    sql_dict: dict[str, str]
    workload: "Workload"
    # The language the engine is generated in. Selects the workspace scaffold,
    # the build toolchain, and the language slots of the prompts; nothing else in
    # the conversation machinery branches on it.
    language: EngineLang = EngineLang.CPP
    bespoke_storage: bool = True
    max_turns: int | None = None
    query_validator: "QueryValidator | None" = None
    # where the engine persists the conversation JSON (list of accepted prompts)
    conversation_json_path: Path | None = None
    # The run's model wrapper (backend-agnostic: litellm or native OpenAI), for a
    # stage builder that needs a one-off LLM call (e.g. an LLM-as-judge validation)
    # outside the main conversation's session/tools. None in tests that build a
    # bare ConvContext.
    agent_sdk_wrapper: "SDKWrapper | None" = None

    # Runtime scratchpad written by declarative items during execution
    # (MeasureBaselines stores {query_id: runtime_ms} here by default).
    single_threaded_rt_ms: Dict[str, float] | None = None

    # lazy caches
    _reference_plans: dict = field(default_factory=dict, repr=False)
    _sample_exec_settings: "ExecSettings | None" = field(default=None, repr=False)
    _sample_query_args: dict | None = field(default=None, repr=False)

    @property
    def persistent_storage(self) -> bool:
        return is_persistent_storage(self.db_storage)

    @property
    def lang_profile(self) -> LanguageProfile:
        """The language slots the prompt generators substitute (see profiles.py)."""
        return get_language_profile(self.language.value)

    def reference_plans(
        self, source: str = "umbra", cleanup: bool = True
    ) -> Dict[str, str | dict]:
        """Per-query reference execution plans from a reference engine.

        The workload provider is the single source of exec-configs: we produce a
        BENCHMARK-mode batch (at the provider's ``benchmark_sf``) and resolve it
        against the query-execution cache, which returns the reference engine's
        query plan for each query (executing + caching on a miss). Evaluated
        lazily and cached per (source, cleanup).
        """
        key = (source, cleanup)
        if key in self._reference_plans:
            return self._reference_plans[key]

        from synnodb.conversations.utils.cleanup_plans import (
            cleanup_duckdb_plan,
            cleanup_umbra_plan,
        )
        from synnodb.tools.run_tool_mode import RunToolMode
        from synnodb.workloads.system_factory import System

        if source == "umbra":
            system = System.UMBRA
            cleanup_fn = cleanup_umbra_plan
        elif source == "duckdb":
            system = System.DUCKDB
            cleanup_fn = cleanup_duckdb_plan
        else:
            raise ValueError(f"Unknown plan source {source} for sample plans.")

        assert self.query_validator is not None, (
            "reference_plans requires a query validator (disable_valtool is set?)"
        )

        # one BENCHMARK batch at the provider's benchmark scale factor
        batches = self.workload_provider.produce_workload(
            run_mode=RunToolMode.BENCHMARK,
            query_ids=self.query_ids,
            num_threads=1,
            core_ids=None,
        )
        assert len(batches) == 1, (
            f"BENCHMARK mode should emit exactly one batch, got {len(batches)}"
        )

        results = (
            self.query_validator.query_execution_cache.lookup_or_execute_query_batch(
                batches[0], system
            )
        )

        # take the first result per query (BENCHMARK repeats the same query)
        plans: Dict[str, str | dict] = {}
        for res in results:
            query_id = res.query_entry.query_id
            if query_id in plans:
                continue
            plan = res.plan
            assert plan is not None, (
                f"Reference engine {system} returned no plan for query {query_id}."
            )
            plans[query_id] = cleanup_fn(plan) if cleanup else plan

        self._reference_plans[key] = plans
        return plans

    def sample_exec_settings(self) -> "ExecSettings":
        """The exec settings (scale factor, storage, data location) the benchmark
        runs of this conversation execute under. Lazy, cached."""
        if self._sample_exec_settings is None:
            from synnodb.utils.get_sample_q_args import get_sample_exec_settings

            self._sample_exec_settings = get_sample_exec_settings(
                self.workload_provider
            )
        return self._sample_exec_settings

    def sample_query_args(self) -> dict[str, str]:
        """Deterministic example placeholder instantiations per query. Lazy, cached."""
        if self._sample_query_args is None:
            from synnodb.utils.get_sample_q_args import get_sample_query_args

            self._sample_query_args = get_sample_query_args(
                workload_provider=self.workload_provider
            )
        return self._sample_query_args
