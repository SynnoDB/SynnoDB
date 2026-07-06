"""The conversation engine: executes a declarative stage list against the LLM.

One engine for every conversation - predefined and user-assembled alike. A
:class:`Conversation` is constructed with a ``plan_stages`` callable (from a
``ConversationPlan``) and a :class:`~synnodb.conversations.conv_context.ConvContext`;
``run()`` builds the stage list, registers it with the supervision agent, and
executes item by item (markers, prompt stages with measurement/revert
monitoring, and the ``PerQueryLoop`` composite).

The interactive u/r/i/c machinery (confirm / replace / insert-before /
compaction on every prompt) lives in the private :class:`_InteractiveConsole`
collaborator; auto modes (``auto_u`` / ``replay_cache``) bypass it.
"""

import asyncio
import inspect
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import is_multiline
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.stage_items import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
    AssertCorrect,
    DynamicStageConfig,
    MarkerItem,
    MeasureBaselines,
    PerQueryLoop,
    PromptStage,
    StageItem,
    SupervisionHorizon,
)
from synnodb.conversations.supervision_agent import SupervisionAgent
from synnodb.llm.sdk.sdk_wrapper import SDKWrapper
from synnodb.observability.logging.notify import send_notification
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.run import RunTool, delete_result_files
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.utils import atomic_write, create_parent_and_set_permissions

logger = logging.getLogger(__name__)

NOTIFY_AFTER_SEC = 60

# Markers that should not trigger supervision-agent review.
_SUPERVISION_SKIP_MARKERS = {
    COMPACTION_MARKER,
    BENCHMARK_MARKER,
    VALIDATE_ON,
    VALIDATE_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
}

MAX_SUPERVISION_RETRIES = 3

# The no-op turn a PerQueryLoop with branch_anchor=True emits before creating
# the per-query conversation branches. The SDK's create_branch_from_turn copies
# turns strictly BEFORE the branch turn (see tests/test_sdk_branch_semantics.py),
# so the turn at the branch point is sacrificed from every branch - this anchor
# makes sure that sacrificed turn is a disposable one.
BRANCH_ANCHOR_PROMPT = (
    "We are about to create one conversation branch per query for the "
    "multi-threading tuning loop. The base implementation is already "
    "parallel-ready through the shared query pool. Do not inspect files, "
    "do not use tools, and do not change code. Reply exactly: Ready for "
    "per-query branches."
)

# Display labels for each choice key (order is preserved in the prompt).
_CHOICE_LABELS: dict[str, str] = {
    "u": "<b>[u]</b>se",
    "r": "<b>[r]</b>eplace",
    "i": "<b>[i]</b>nsert before",
    "c": "<b>[c]</b>ompaction",
}


@dataclass
class StageResult:
    name: str
    rt_before_s: float | None
    rt_after_s: float
    speedup_vs_duckdb: float

    @property
    def improved(self) -> bool:
        if self.rt_before_s is None:
            # always improved if we don't know previous runtime
            return True
        return self.rt_after_s < self.rt_before_s

    @property
    def improvement_factor(self) -> float:
        if self.rt_after_s > 0 and self.rt_before_s is not None:
            return self.rt_before_s / self.rt_after_s
        return float("inf")


class ValidationStillFailsException(Exception):
    pass


class _InteractiveConsole:
    """The interactive prompt UI: choice selection and multiline input.

    Private collaborator of the engine - only consulted when no auto mode
    resolves the choice.
    """

    def __init__(self, allowed_choices: Tuple[str, ...], notify: bool):
        self.allowed_choices = allowed_choices
        self.notify = notify

        kb = KeyBindings()

        @kb.add("c-d", filter=is_multiline)
        def _(event):
            event.app.current_buffer.validate_and_handle()

        self._session = PromptSession(key_bindings=kb)

    async def ask_choice(self, prompt: str) -> str:
        labels = " / ".join(
            _CHOICE_LABELS[c] for c in self.allowed_choices if c in _CHOICE_LABELS
        )
        prompt_text = HTML(f"{labels} ? ")

        notified = False
        notify_msg = (
            f"**LLM requires action on prompt:**\n```quote\n{prompt[:1000]}\n```"
        )

        while True:
            if not notified and self.notify:
                send_notification(notify_msg, check_tmux=True)

            prompt_task = asyncio.create_task(self._session.prompt_async(prompt_text))

            while True:
                try:
                    raw = await asyncio.wait_for(
                        asyncio.shield(prompt_task),
                        timeout=NOTIFY_AFTER_SEC,
                    )
                except asyncio.TimeoutError:
                    if self.notify and not notified:
                        send_notification(notify_msg, check_tmux=False)
                        notified = True
                    continue

                choice = (raw or "").strip().lower()
                if choice in self.allowed_choices:
                    return choice

                # invalid input: restart a fresh prompt
                notified = False
                break

    async def ask_multiline(self, label: str) -> str:
        text = await self._session.prompt_async(
            HTML(f"<b>{label}</b> "),
            multiline=True,
        )
        return text.strip()


class Conversation:
    """Executes one conversation plan to completion."""

    def __init__(
        self,
        *,
        plan_stages: "Callable[[ConvContext], list[StageItem]]",
        conv_context: ConvContext,
        run_tool: RunTool,
        git_snapshotter: GitSnapshotter,
        run_stats_collector: RunStatsCollector,
        supervision_agent: SupervisionAgent | None,
        gen_incorrect_output_prompt_fn: Callable,
        agent_sdk_wrapper: SDKWrapper,
        callback: Callable[[str, Optional[str], int, Optional[int], bool], str],
        finish_interactive: bool = False,
        debug_category: str | None = None,
        prompt_pretext: str | None = None,
        notify: bool = False,
        auto_finish: bool = False,
        auto_u: bool = False,
        replay_cache: bool = False,
        runtime_tracker: Optional[RuntimeTracker] = None,
    ):
        self.plan_stages = plan_stages
        self.conv_context = conv_context
        self.run_tool = run_tool
        self.git_snapshotter = git_snapshotter
        self.run_stats_collector = run_stats_collector
        self.supervision_agent = supervision_agent
        self.gen_incorrect_output_prompt_fn = gen_incorrect_output_prompt_fn
        self.agent_sdk_wrapper = agent_sdk_wrapper
        self.callback = callback
        # Whether run() ends by offering the interactive add-more-prompts loop
        # (ask_to_finish_and_save); a no-op under auto_finish.
        self.finish_interactive = finish_interactive
        # DebugLogger category; None disables per-run debug logging.
        self.debug_category = debug_category
        # Prefix prepended to every prompt-stage prompt (None = no prefix).
        self.prompt_pretext = prompt_pretext
        self.notify = notify
        self.auto_finish = auto_finish
        self.runtime_tracker = runtime_tracker

        assert conv_context.conversation_json_path is not None, (
            "ConvContext.conversation_json_path is required by the engine"
        )
        self.conversation_json_path: Path = conv_context.conversation_json_path
        self.all_query_ids = conv_context.query_ids

        # create cache dir if not existing
        create_parent_and_set_permissions(self.conversation_json_path)

        allowed_choices: Tuple[str, ...] = ("u", "i")

        # create auto mode callbacks
        if auto_u:
            logger.warning(
                "Auto-U mode enabled: automatically proceeding with all prompts without asking for user confirmation. Make sure this is what you want!"
            )
            assert not replay_cache, "auto_u and replay_cache cannot both be enabled"
            self.get_choice = lambda: "u"
        elif replay_cache:
            # auto-approve if last LLM response was cached, otherwise ask user (same as auto_u but only for cached responses - executes only the cached prompts and the first non-cached prompt, then stops and waits for user input for the rest)
            self.get_choice = lambda: (
                "u" if self.agent_sdk_wrapper.last_llm_call_was_cached() else None
            )
        else:
            self.get_choice = None

        self._console = _InteractiveConsole(allowed_choices, notify)

        # accepted prompts, in order; reset by run()
        self.used: List[str] = []
        self.query_rt_log: Dict[str, float] = dict()
        self._stage_nr = 0

    # ---------- run template ----------

    def build_items(self) -> List[StageItem]:
        """The declarative stage list this conversation executes."""
        return self.plan_stages(self.conv_context)

    def _register_supervision(self, items: List[StageItem]) -> None:
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info(items)

    def _register_planned_stages(self, items: List[StageItem]) -> None:
        """Publish previews of the whole scheduled stage list to the drains so
        the dashboard can show the upcoming (not-yet-executed) prompts."""
        from synnodb.conversations.stage_preview import build_stage_previews

        self.run_stats_collector.register_planned_stages(build_stage_previews(items))

    def _next_stage_nr(self) -> int:
        """The runner-owned monotonic stage counter: one number per executed
        item, composites increment through it. Feeds only logging/dashboard."""
        nr = self._stage_nr
        self._stage_nr += 1
        return nr

    @contextmanager
    def _debug_logging(self):
        """Install a DebugLogger on the collector for the duration of the run."""
        if self.debug_category is None:
            yield
            return
        from synnodb.observability.logging.debug_logger import DebugLogger
        from synnodb.utils.utils import storage_label

        self.run_stats_collector.debug_logger = DebugLogger(
            category=self.debug_category,
            storage=storage_label(self.conv_context.db_storage),
            model=self.run_stats_collector.model,
            base_dir=self.conv_context.workspace_path / "debug_logs",
        )
        try:
            yield
        finally:
            self.run_stats_collector.debug_logger = None

    async def run(self) -> Optional[List[str]]:
        self.used = []
        self._stage_nr = 0
        with self._debug_logging():
            items = self.build_items()
            self._register_supervision(items)
            self._register_planned_stages(items)
            await self._run_stages(items, prompt_pretext=self.prompt_pretext)
        if self.finish_interactive:
            return await self.ask_to_finish_and_save()
        return self.used

    # ---------- helpers ----------

    @property
    def _olap_provider(self):
        """The workload provider backing the run tool (source of exec settings / SFs)."""
        return self.run_tool.workload_provider

    def _run_tool_benchmark(
        self,
        query_ids=None,
        trace_mode: bool = False,
    ):
        """Thin wrapper around run_tool.run with standard benchmark parameters."""
        return self.run_tool.run(
            mode=RunToolMode.BENCHMARK,
            optimize=True,
            query_ids=query_ids,
            trace_mode=trace_mode,
            external_call=True,
        )

    def _assemble_stage_prompt(
        self,
        stage_config: PromptStage,
        rt_before_s: float | None,
        tracing_data: str | None,
    ) -> str:
        """Build the prompt for a static stage from its get_prompt callback.

        Exactly one of ``get_prompt`` / ``get_prompt_with_tracing`` must be set.
        The callback receives the stage's exec settings and the previous impl
        runtime in milliseconds (0 if not yet measured).
        """
        rt_before_ms = rt_before_s * 1000 if rt_before_s is not None else 0

        if stage_config.get_prompt is not None:
            assert stage_config.get_prompt_with_tracing is None, (
                "get_prompt and get_prompt_with_tracing cannot both be set. Please choose one."
            )
            return stage_config.get_prompt(stage_config.exec_settings, rt_before_ms)

        if stage_config.get_prompt_with_tracing is not None:
            assert tracing_data is not None, (
                "Tracing data is required for get_prompt_with_tracing but is None."
            )
            return stage_config.get_prompt_with_tracing(
                stage_config.exec_settings, rt_before_ms, tracing_data
            )

        raise ValueError("Either get_prompt or get_prompt_with_tracing must be set.")

    @contextmanager
    def _benchmark_sf_override(self, item: StageItem):
        """Apply an item's benchmark_sf on the workload provider for the item's
        duration, restoring the previous value afterwards (also on exception)."""
        sf = getattr(item, "benchmark_sf", None)
        if sf is None:
            yield
            return
        from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

        provider = self._olap_provider
        assert isinstance(provider, OLAPWorkloadProvider), (
            "benchmark_sf overrides require an OLAP workload provider"
        )
        if sf == "large_check":
            # Large-scale check SF travels with the workload (TPC-H: 100,
            # CEB: 10); fall back to the configured benchmark SF for a workload
            # that declares no large_check_sf.
            resolved = provider.spec.large_check_sf
            sf = provider.benchmark_sf if resolved is None else resolved
        previous = provider.benchmark_sf
        provider.set_benchmark_sf(sf)
        try:
            yield
        finally:
            provider.set_benchmark_sf(previous)

    async def _run_stages(
        self,
        stage_list: list[StageItem],
        prompt_pretext: str | None = None,
    ) -> None:
        """Iterate a stage list, executing each item in order.

        Marker items are lowered to their legacy marker strings and passed to
        _exec (the strings remain the wire/persistence format). PromptStage
        entries are run via _run_stage_with_revert_monitoring; PerQueryLoop is
        the only composite and increments the stage counter through its stages.
        SupervisionHorizon entries execute nothing but still occupy a stage
        number, so numbering matches the supervisor's registered list.
        """
        for stage in stage_list:
            with self._benchmark_sf_override(stage):
                await self._run_item(stage, prompt_pretext)

    async def _run_item(self, stage: StageItem, prompt_pretext: str | None) -> None:
        if isinstance(stage, SupervisionHorizon):
            self._next_stage_nr()
            return
        if isinstance(stage, MarkerItem):
            await self._exec(stage.marker, None, current_stage_nr=self._next_stage_nr())
        elif isinstance(stage, AssertCorrect):
            self._next_stage_nr()
            qids = (
                list(stage.query_ids)
                if stage.query_ids is not None
                else self.all_query_ids
            )
            correct, _, _ = self._check_correctness(qids, trace_mode=False)
            assert correct, (
                "Initial implementation does not produce correct results according to the validation tool. Please fix the implementation until it is correct before starting with optimization."
            )
        elif isinstance(stage, MeasureBaselines):
            self._next_stage_nr()
            setattr(
                self.conv_context, stage.into, self._measure_all_query_runtimes_ms()
            )
        elif isinstance(stage, PerQueryLoop):
            await self._run_per_query_loop(stage)
        elif isinstance(stage, PromptStage):
            await self._run_stage_with_revert_monitoring(
                stage_config=stage,
                prompt_pretext=prompt_pretext,
                current_stage_nr=self._next_stage_nr(),
            )
        elif isinstance(stage, DynamicStageConfig):
            stage_nr = self._next_stage_nr()
            while True:
                prompt = stage.next_prompt()
                if prompt is None:
                    break
                await self._exec(
                    prompt,
                    stage.descriptor,
                    max_turns=stage.max_turns,
                    current_stage_nr=stage_nr,
                )
        else:
            raise ValueError(f"Unsupported stage-list entry: {stage!r}")

    def _measure_all_query_runtimes_ms(self) -> Dict[str, float]:
        """Benchmark all queries and return {query_id: impl runtime in ms}."""
        _, metrics, _ = self.run_tool.run(
            mode=RunToolMode.BENCHMARK,
            optimize=True,
            query_ids=self.all_query_ids,
            external_call=True,
        )
        assert metrics is not None
        runtimes_ms: Dict[str, float] = {}
        for query_id in self.all_query_ids:
            q_3d_str = query_id.zfill(3)
            key = f"validation/query_{q_3d_str}/bespoke_runtime_ms"
            assert key in metrics, (
                f"Expected metric {key} not found in {metrics.keys()}"
            )
            runtimes_ms[query_id] = metrics[key]
        return runtimes_ms

    def _measure_query_rt_s(self, query_id: str) -> float:
        """Measure one query's current runtime (s), updating query_rt_log.

        Falls back to the last logged runtime when the metrics of this
        measurement are unusable (e.g. the run was killed/timed out).
        """
        _, metrics, _ = self.run_tool.run(
            mode=RunToolMode.BENCHMARK, query_ids=[query_id], optimize=True
        )
        assert metrics is not None

        try:
            bespoke_rt_s, _, _ = extract_speedup_of_last_snapshot(
                statistics=metrics,
                query=query_id,
            )
            self.query_rt_log[query_id] = bespoke_rt_s
        except AssertionError as e:
            logger.warning(f"Failed to extract speedup for query {query_id}: {e}")
            # lookup runtime from a past run
            bespoke_rt_s = self.query_rt_log[query_id]
        return bespoke_rt_s

    def _collect_tracing(self, query_ids: list[str] | None) -> str:
        """Collect fresh tracing data for the given queries (None = all)."""
        _, _, tracing_output = self.run_tool.run(
            mode=RunToolMode.BENCHMARK,
            query_ids=query_ids,
            trace_mode=True,
            optimize=True,
        )
        if tracing_output is None:
            logger.warning(
                f"Trace-mode run for queries {query_ids} produced no output (likely crashed). Using placeholder."
            )
            tracing_output = "(Tracing data unavailable -- the trace-mode run crashed.)"
        return tracing_output

    async def _run_per_query_loop(self, loop: PerQueryLoop) -> None:
        """Execute a PerQueryLoop item: one conversation branch per query,
        stages executed stage-major (ring by ring) across all queries."""
        # delete stale result files before the loop starts
        delete_result_files(self.run_tool.cwd)
        # clear the supervisor's stage horizon - inside the loop there is no
        # flat stage list whose indices could match the stage numbers
        self._register_supervision([])

        query_ids = self.all_query_ids

        if loop.branch_anchor:
            assert loop.conversation_branching, (
                "branch_anchor requires conversation_branching"
            )
            # The SDK branch helper copies turns strictly before the requested
            # turn. Branching from this no-op anchor keeps the anchor out of
            # per-query branches.
            await self._exec(
                BRANCH_ANCHOR_PROMPT,
                "Branch Anchor",
                current_stage_nr=self._next_stage_nr(),
                max_turns=5,
            )

        per_query_stages: Dict[str, List[StageItem]] = {}
        per_query_branch: Dict[str, str] = {}

        last_turn_nr = await self.agent_sdk_wrapper.get_conversation_turns()

        logger.debug(
            f"Collect statistics of initial implementation for all queries ({len(query_ids)})."
        )
        for query_id in query_ids:
            per_query_stages[query_id] = loop.build(query_id, self.conv_context)

            if not loop.conversation_branching:
                continue

            # Switch back to main branch
            await self.agent_sdk_wrapper.switch_to_conversation_branch("main")

            # create conversation branches for each query
            try:
                per_query_branch[
                    query_id
                ] = await self.agent_sdk_wrapper.create_conversation_branch_from_turn(
                    turn_nr=last_turn_nr, branch_name=f"query_{query_id}_{last_turn_nr}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to create conversation branch for query {query_id} from turn {last_turn_nr}: {e}"
                )
                logger.error(await self.agent_sdk_wrapper.get_conversation_turns())
                raise e

        logger.debug(f"Branches created for all queries: {per_query_branch}")

        num_stages = len(per_query_stages[query_ids[0]])
        assert all(len(s) == num_stages for s in per_query_stages.values()), (
            "PerQueryLoop.build must yield the same number of stages for every query"
        )

        # clear query rt log
        self.query_rt_log = dict()

        stage_end_msg = None
        for stage_id in range(num_stages):
            for query_id in query_ids:
                if loop.conversation_branching:
                    # switch to the conversation branch for this query
                    await self.agent_sdk_wrapper.switch_to_conversation_branch(
                        per_query_branch[query_id]
                    )

                stage = per_query_stages[query_id][stage_id]
                current_stage_nr = self._next_stage_nr()

                assert isinstance(stage, PromptStage), (
                    f"PerQueryLoop stages must be PromptStage items, got {stage!r}"
                )
                # run the stage - includes automatic reverts if regressions are
                # detected; the runtime measured just before the stage and fresh
                # tracing data reach the prompt callbacks
                with self._benchmark_sf_override(stage):
                    await self._run_stage_with_revert_monitoring(
                        query_id=query_id,
                        stage_config=stage,
                        prompt_pretext=None,
                        measure_rt_before=True,
                        current_stage_nr=current_stage_nr,
                    )

                # delete prior result files
                delete_result_files(self.run_tool.cwd)

                await self._exec(
                    COMPACTION_MARKER, "compaction", current_stage_nr=current_stage_nr
                )

            if loop.end_of_ring_benchmark:
                # perform full benchmarking across all queries at the end of the stage
                delete_result_files(self.run_tool.cwd)
                stage_end_msg, _, _ = self.run_tool.run(
                    mode=RunToolMode.BENCHMARK, query_ids=None, optimize=True
                )

        logger.info(f"Final validation metrics after per-query loop: {stage_end_msg}")

    # ---------- core stage execution ----------

    async def _run_stage_with_revert_monitoring(
        self,
        stage_config: PromptStage,
        current_stage_nr: int,
        prompt_pretext: str | None,
        query_id: Optional[str] = None,
        measure_rt_before: bool = False,
    ) -> StageResult | None:
        """Execute one optimization stage and return its measured outcome.

        With ``measure_rt_before`` the stage's query is benchmarked just before
        the stage so the prompt callback receives the current runtime; tracing
        data is collected fresh whenever the stage sets
        ``get_prompt_with_tracing``.
        """

        # check correct stage config
        if stage_config.feedback_on_incorrect:
            assert stage_config.measure_performance_after_stage, (
                "feedback_on_incorrect is true, but measure_performance_after_stage is required for this"
            )
        if stage_config.auto_revert_on_regression:
            assert stage_config.measure_performance_after_stage, (
                "auto_revert_on_regression is true, but measure_performance_after_stage is required for this"
            )

        # extract current git snapshot
        current_snapshot = self.git_snapshotter.current_hash
        assert current_snapshot is not None, "Current git snapshot is None."

        # collect the current runtime for the prompt (loop stages)
        rt_before_s: float | None = None
        if measure_rt_before:
            assert query_id is not None, (
                "measure_rt_before requires a query_id to benchmark"
            )
            rt_before_s = self._measure_query_rt_s(query_id)

        # collect fresh tracing data whenever the prompt needs it
        tracing_data: str | None = None
        if stage_config.get_prompt_with_tracing is not None:
            tracing_data = self._collect_tracing(
                [query_id] if query_id is not None else None
            )

        pretext_str = ""
        if prompt_pretext is not None and prompt_pretext.strip() != "":
            pretext_str = prompt_pretext.strip() + "\n"

        # assemble and execute the stage prompt
        prompt = self._assemble_stage_prompt(stage_config, rt_before_s, tracing_data)

        if self.run_stats_collector.debug_logger:
            effective_query_id = (
                stage_config.measure_perf_qid
                if stage_config.measure_perf_qid is not None
                else query_id
            )
            self.run_stats_collector.debug_logger.log_stage_start(
                current_stage_nr,
                stage_config.descriptor,
                rt_before_s=rt_before_s,
                query_id=effective_query_id,
            )

        await self._exec(
            pretext_str + prompt,
            stage_config.descriptor,
            max_turns=stage_config.max_turns,
            current_stage_nr=current_stage_nr,
        )

        # overwrite query_id if passed via stage config
        if stage_config.measure_perf_qid is not None:
            assert query_id is None
            query_id = stage_config.measure_perf_qid

        if stage_config.measure_performance_after_stage:
            assert query_id is not None, (
                "query_id must be provided if measure_performance_after_stage is True"
            )
            try:
                # measure performance after LLM interaction for this stage
                msg, metrics, tracing_output = self._run_tool_benchmark(
                    query_ids=[query_id],
                )

                assert metrics is not None, (
                    f"Metrics is None after running stage '{stage_config.descriptor}' for query {query_id}. Message: {msg}"
                )

                if (
                    stage_config.feedback_on_incorrect
                    and not metrics["validation/correct"]
                ):
                    # go into feedback loop
                    metrics = await self.check_and_feedback_correctness(
                        [query_id],
                        trace_modes=[False],
                        current_stage_nr=current_stage_nr,
                        gen_incorrect_output_prompt_fn=self.gen_incorrect_output_prompt_fn,
                    )

                assert metrics["validation/correct"], (
                    f"Implementation is not correct after stage '{stage_config.descriptor}' for query {query_id}. Metrics: {metrics}. {msg}"
                )

                rt_after_s, _, speedup = extract_speedup_of_last_snapshot(
                    metrics, query_id
                )
                self.query_rt_log[query_id] = rt_after_s
            except ValidationStillFailsException as ve:
                # correctness check failed after multiple attempts
                logger.error(
                    f"Validation check still fails after multiple attempts for stage '{stage_config.descriptor}' and query {query_id}. Exception: {ve}"
                )
                raise ve
            except Exception:
                # hit a timeout (or any other measurement failure)
                rt_after_s = float("inf")
                speedup = 0.0
                logger.exception(
                    f"Error while measuring performance after stage '{stage_config.descriptor}' for query {query_id}."
                )

            assert stage_config.descriptor is not None, (
                "Stage descriptor should not be None here."
            )
            result = StageResult(
                name=stage_config.descriptor,
                rt_before_s=rt_before_s,
                rt_after_s=rt_after_s,
                speedup_vs_duckdb=speedup,
            )

            rt_before_str = f"{rt_before_s:.3f}s" if rt_before_s is not None else "N/A"
            logger.info(
                f"Query {query_id} | Stage '{stage_config.descriptor}': "
                f"{rt_before_str} -> {rt_after_s:.3f}s "
                f"({'improved x' + f'{result.improvement_factor:.2f}' if result.improved else 'no improvement'}), "
                f"speedup vs DuckDB: {speedup:.2f}x"
            )

            if not result.improved and stage_config.auto_revert_on_regression:
                await self._revert_stage(
                    stage_config, current_snapshot, query_id, current_stage_nr
                )
            elif not result.improved:
                logger.warning(
                    f"Keeping changes from stage '{stage_config.descriptor}' for query {query_id} despite no improvement."
                )
        else:
            result = None

        if stage_config.post_stage_validate is not None:
            await self._run_post_stage_validate(stage_config, current_stage_nr)

        # On the validation-raise path above we deliberately log no stage end:
        # the partial log (header + prompts/turns/tools up to the failure) is the
        # useful artifact for debugging that abort.
        if self.run_stats_collector.debug_logger:
            if result is not None:
                self.run_stats_collector.debug_logger.log_stage_end(
                    rt_after_s=result.rt_after_s,
                    speedup_after=result.speedup_vs_duckdb,
                )
            else:
                self.run_stats_collector.debug_logger.log_stage_end()

        return result

    async def _revert_stage(
        self,
        stage_config: PromptStage,
        current_snapshot: str,
        query_id: str,
        current_stage_nr: int,
    ) -> None:
        """Roll back to ``current_snapshot`` and re-measure, updating query_rt_log.

        Used when a stage regressed (or didn't improve) and the stage is
        configured with ``auto_revert_on_regression``.
        """
        logger.warning(
            f"Reverting changes from stage '{stage_config.descriptor}' for query {query_id} due to no improvement (revert to: {current_snapshot}). Turn: {self.run_stats_collector.last_turn}"
        )

        # clear all untracked & tracked changes
        self.git_snapshotter.reset_changes()
        self.git_snapshotter.clear_untracked()
        self.git_snapshotter.restore(current_snapshot)

        # measure and log performance after the rollback
        out_str, metrics, _ = self._run_tool_benchmark(query_ids=[query_id])
        assert metrics is not None, (
            f"Metrics is None after reverting stage '{stage_config.descriptor}' for query {query_id}."
        )

        if not metrics["validation/correct"]:
            logger.warning(
                f"Reverted stage '{stage_config.descriptor}' for query {query_id} but the reverted version is not correct ('{out_str}'). This should not happen!"
            )
            await self._exec(
                f"I rolled back your changes since the output was not correct. But after rollback, the results are still wrong ('{out_str}'). Please re-evaluate your implementation of query {query_id} (and also with all queries query_id=None) and make sure that it is correct for all scale-factors!",
                stage_config.descriptor,
                max_turns=stage_config.max_turns,
                current_stage_nr=current_stage_nr,
            )
            _, metrics, _ = self._run_tool_benchmark(query_ids=[query_id])
            assert metrics is not None, (
                f"Metrics is None after reverting stage '{stage_config.descriptor}' for query {query_id}."
            )

        rt_after_s, _, _ = extract_speedup_of_last_snapshot(metrics, query_id)
        self.query_rt_log[query_id] = rt_after_s

    async def _run_post_stage_validate(
        self,
        stage_config: PromptStage,
        current_stage_nr: int,
    ) -> None:
        """Run the stage's post_stage_validate callback, re-prompting on feedback.

        Loops until the callback returns None (passing) or the attempt budget is
        exhausted, in which case it raises.
        """
        assert stage_config.post_stage_validate is not None
        max_validate_attempts = 10
        attempts = max_validate_attempts
        while True:
            attempts -= 1
            feedback = stage_config.post_stage_validate()
            if feedback is None:
                break

            logger.info(
                f"Stage '{stage_config.descriptor}': validate callback returned feedback, re-prompting LLM."
            )
            # After 3 failed attempts, add explicit hint about optimize/trace flags
            if attempts <= max_validate_attempts - 3:
                feedback += (
                    "\nIMPORTANT: You have failed this check multiple times. "
                    "Have you checked whether the issue is related to running with or without tracing enabled (trace_mode=True/False)? "
                    "Make sure to test with trace_mode=True and False (and optimize=True) in your run tool calls to reproduce the issue. "
                )
            await self._exec(
                feedback,
                stage_config.descriptor,
                max_turns=stage_config.max_turns,
                current_stage_nr=current_stage_nr,
            )

            if attempts == 0:
                raise Exception(
                    f"Stage '{stage_config.descriptor}': validate callback still returns feedback after {max_validate_attempts} attempts. Please investigate. This can be just the model behaving not well, a problem on the model provider side, or a problemm with the framework (in case you have edits under test)."
                )

    async def _exec(
        self,
        task_prompt: str,
        prompt_descriptor: Optional[str],
        current_stage_nr: int,
        max_turns: Optional[int] = None,
    ) -> str | None:
        prompt = task_prompt

        # reset activity monitoring at the beginning of the stage
        if self.supervision_agent is not None:
            self.supervision_agent.reset_activity_monitoring()

        supervision_retries = 0

        while True:
            # execute the prompt and get the outcome
            user_choice, executed_prompt, last_outcome = await self.process_prompt(
                prompt, prompt_descriptor, max_turns
            )

            if user_choice == "i":
                # prompt injected before — re-execute with the injected prompt
                continue
            if user_choice not in ("u", "r"):
                raise Exception(
                    f"Unexpected user choice: {user_choice!r}. Expected 'u' or 'r'."
                )

            # apply supervision agent if available
            if (
                self.supervision_agent is not None
                and prompt not in _SUPERVISION_SKIP_MARKERS
            ):
                if last_outcome is None:
                    logger.debug("Supervision Agent skipped: output is empty")
                elif supervision_retries >= MAX_SUPERVISION_RETRIES:
                    logger.error(
                        f"Supervision loops exceeded ({supervision_retries} attempts). Continuing."
                    )
                else:
                    supervision_retries += 1
                    supervision_feedback = await self.supervision_agent.get_supervision(
                        prompt=executed_prompt,
                        llm_output=last_outcome,
                        current_stage_nr=current_stage_nr,
                    )
                    if supervision_feedback is not None:
                        logger.warning(
                            f"❌Supervision agent not satisfied. Re-prompting LLM with feedback. (attempt {supervision_retries})"
                        )
                        prompt = f"Supervision Agent Feedback:\n{supervision_feedback}"
                        continue
                    else:
                        logger.info(
                            f"✅Supervision agent approved stage. ({supervision_retries - 1} retries)"
                        )

            return last_outcome

    async def check_and_feedback_correctness(
        self,
        qids: List[str],
        current_stage_nr: int,
        gen_incorrect_output_prompt_fn: Callable,
        trace_modes=[False, True],
    ) -> Dict:
        metrics = None
        for tracing_mode in trace_modes:
            attempts = 0
            while True:
                # check correctness
                success, metrics, msg = self._check_correctness(
                    qids, trace_mode=tracing_mode
                )
                if not success:
                    # incorrect
                    assert msg is not None
                    await self._exec(
                        gen_incorrect_output_prompt_fn(
                            tracing_mode,
                            qids,
                            msg,
                        ),
                        prompt_descriptor=f"Fix Correctness (tracing_mode={tracing_mode}, qids={qids})",
                        current_stage_nr=current_stage_nr,
                    )
                else:
                    break

                attempts += 1
                if attempts >= 3:
                    raise ValidationStillFailsException(
                        f"Validation check still fails after {attempts} attempts to fix it for trace_mode={tracing_mode}, qids={qids}. Please investigate the issue."
                    )

        assert metrics is not None, (
            "Metrics should not be None after correctness check."
        )
        return metrics

    def _check_correctness(
        self,
        qids: List[str],
        trace_mode: bool,
    ) -> tuple[bool, Dict | None, str | None]:
        msg, metrics, trace_output = self._run_tool_benchmark(qids, trace_mode)
        if metrics is None or not metrics["validation/correct"]:
            logger.error(
                f"Validation check reported results are incorrect (with trace_mode={trace_mode}, qids={qids})."
            )
            return False, metrics, msg
        return True, metrics, None

    # ---------- interaction ----------

    async def process_prompt(
        self,
        prompt: str,
        prompt_descriptor: Optional[
            str
        ] = None,  # short description of the prompt, used for logging and callbacks
        max_turns: Optional[int] = None,
        additional_out_str: Optional[str] = None,
    ) -> Tuple[str, str, str | None]:
        """
        Handle one interaction round for `prompt`.

        Resolves the user choice by consulting `self.get_choice` first (set by
        auto_u / replay_cache modes), then falling back to interactive input.
        Executes the chosen action, appends to `used`, and persists via `_save`.
        """

        # Show the prompt before asking for the choice, so user can see what they're acting on while deciding.
        self._show_prompt(prompt, additional_out_str)

        choice = self.get_choice() if self.get_choice else None
        if choice is None:
            t1 = time.time()
            choice = await self._console.ask_choice(prompt)
            if self.runtime_tracker is not None:
                self.runtime_tracker.add_wait_time(
                    time.time() - t1
                )  # if user took 30s to respond, add 30s to wait time so that it's not counted in the agent's runtime

        last_output = None
        if choice == "u":
            self.used.append(prompt)
            last_output = await self._maybe_await_callback(
                prompt,
                prompt_descriptor,
                len(self.used) - 1,
                max_turns,
                prompt_already_printed=True,
            )

        elif choice == "r":
            new_prompt = await self._console.ask_multiline(
                "Replacement (Ctrl+D to submit)"
            )
            if new_prompt.strip():
                self.used.append(new_prompt)
                last_output = await self._maybe_await_callback(
                    new_prompt, new_prompt[:20], len(self.used) - 1, max_turns
                )

        elif choice == "i":
            new_prompt = await self._console.ask_multiline(
                "Insert before (Ctrl+D to submit)",
            )
            if new_prompt.strip():
                self.used.append(new_prompt)
                self._save(self.used)  # save progress before the callback
                last_output = await self._maybe_await_callback(
                    new_prompt, new_prompt[:20], len(self.used) - 1, max_turns
                )

        elif choice == "c":
            self.used.append(COMPACTION_MARKER)
            self._save(self.used)  # save progress before the callback
            last_output = await self._maybe_await_callback(
                COMPACTION_MARKER, "compaction", len(self.used) - 1, max_turns
            )

        else:
            raise ValueError(f"Unexpected choice: {choice!r}")

        # Save progress after each accepted prompt.
        self._save(self.used)

        # return choice, last prompt, last output
        return choice, self.used[-1], last_output

    async def ask_to_finish_and_save(self) -> List[str]:
        if not self.auto_finish:
            logger.info(
                "\nAdd new prompts (Ctrl+D to submit, empty submits nothing and finishes):"
            )
            while True:
                text = await self._console.ask_multiline("> ")
                if not text.strip():
                    break
                self.used.append(text)
                self._save(self.used)
                await self._maybe_await_callback(text, text[:20], len(self.used) - 1)

            self._save(self.used)

        return self.used

    # ---------- persistence ----------

    def _save(self, prompts: List[str]) -> None:
        atomic_write(
            path=self.conversation_json_path,
            data=(json.dumps(prompts, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    # ---------- UI / callback plumbing ----------

    def _show_prompt(self, prompt: str, additional_info: Optional[str] = None) -> None:
        logger.info(
            "=" * 20
            + f" Prompt {additional_info if additional_info is not None else ''}"
            + "=" * 20
        )
        logger.info(prompt)
        logger.info("=" * 60)

    async def _maybe_await_callback(
        self,
        prompt: str,
        prompt_descriptor: Optional[str],  # short description of the prompt
        index: int,
        max_turns: Optional[int] = None,
        prompt_already_printed: bool = False,  # whether the prompt has already been printed in the current flow (to avoid duplicate prints in some flows, e.g. use)
    ) -> str:
        res = self.callback(
            text=prompt,  # type: ignore
            short_desc=prompt_descriptor,
            idx=index,
            max_turns=max_turns,
            prompt_already_printed=prompt_already_printed,
        )
        if inspect.iscoroutine(res):
            return await res  # type: ignore
        return res


def extract_speedup_of_last_snapshot(statistics: Dict, query: str):
    # extract row from statistics
    # prepend with zeros until three chars long
    query_3chars = query.zfill(3)

    bespoke_key = f"validation/query_{query_3chars}/bespoke_runtime_ms"
    duckdb_key = f"validation/query_{query_3chars}/duckdb_runtime_ms"

    if bespoke_key not in statistics:
        logger.warning(
            "Key %s not found in statistics (query likely killed/timed out). Returning inf runtime.",
            bespoke_key,
        )
        return float("inf"), None, 0.0
    if duckdb_key not in statistics:
        logger.warning(
            "Key %s not found in statistics. Returning inf runtime.", duckdb_key
        )
        return float("inf"), None, 0.0

    # translate runtimes from ms to seconds
    last_bespoke_rt = float(statistics[bespoke_key]) / 1000
    duckdb_rt = float(statistics[duckdb_key]) / 1000

    # calculate speedup
    speedup = duckdb_rt / last_bespoke_rt if last_bespoke_rt > 0 else float("inf")

    return last_bespoke_rt, duckdb_rt, speedup
