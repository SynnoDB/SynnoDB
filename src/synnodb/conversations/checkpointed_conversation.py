import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from synnodb.conversations.conversation import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
    AbstractConversation,
)
from synnodb.conversations.stage_config import DynamicStageConfig, StaticStageConfig
from synnodb.conversations.supervision_agent import (
    SUPERVISION_STAGE_VISIBILITY_MARKER,
    SupervisionAgent,
)
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run import RunTool
from synnodb.tools.run_tool_mode import RunToolMode

logger = logging.getLogger(__name__)

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


class CheckpointedConversation(AbstractConversation):
    def __init__(
        self,
        run_tool: RunTool,
        git_snapshotter: GitSnapshotter,
        run_stats_collector: RunStatsCollector,
        gen_incorrect_output_prompt_fn: Callable,
        supervision_agent: SupervisionAgent | None,
        **kwargs,
    ):
        super().__init__(
            allowed_choices=("u", "i"),
            **kwargs,
        )

        self.run_tool = run_tool
        self.git_snapshotter = git_snapshotter
        self.run_stats_collector = run_stats_collector
        self.supervision_agent = supervision_agent
        self.gen_incorrect_output_prompt_fn = gen_incorrect_output_prompt_fn

        assert not self.replay, (
            "Replay mode is not supported for CheckpointedConversation. Use replay_cache if you want to replay from cache without user interaction."
        )

        self.query_rt_log: Dict[str, float] = dict()

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
        stage_config: StaticStageConfig,
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

    async def _run_stages(
        self,
        stage_list: list,
        prompt_pretext: str | None = None,
        stage_nr_offset: int = 0,
    ) -> None:
        """Iterate a stage list, executing each stage in order.

        String entries (markers) are passed directly to _exec.
        StageConfig entries are run via _run_stage_with_revert_monitoring.
        SUPERVISION_STAGE_VISIBILITY_MARKER entries are skipped (used only
        to set the supervisor's view window).
        """
        for i, stage in enumerate(stage_list):
            stage_nr = i + stage_nr_offset
            if stage == SUPERVISION_STAGE_VISIBILITY_MARKER:
                continue
            if isinstance(stage, str):
                await self._exec(stage, None, current_stage_nr=stage_nr)
            elif isinstance(stage, StaticStageConfig):
                await self._run_stage_with_revert_monitoring(
                    stage_config=stage,
                    prompt_pretext=prompt_pretext,
                    rt_before_s=None,
                    tracing_data=None,
                    current_stage_nr=stage_nr,
                )
            elif isinstance(stage, DynamicStageConfig):
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

    # ---------- core stage execution ----------

    async def _run_stage_with_revert_monitoring(
        self,
        stage_config: StaticStageConfig,
        current_stage_nr: int,
        prompt_pretext: str | None,
        rt_before_s: float | None,
        tracing_data: str | None,
        query_id: Optional[str] = None,
    ) -> StageResult | None:
        """Execute one optimization stage and return its measured outcome."""

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
        stage_config: StaticStageConfig,
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
        stage_config: StaticStageConfig,
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

    @abstractmethod
    async def run(self) -> Optional[List[str]]:
        pass

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


def extract_speedup_of_last_snapshot(statistics: Dict, query: str):
    # extract row from statistics
    # prepend with zeros until three chars long
    query_3chars = query.zfill(3)

    impl_key = f"validation/query_{query_3chars}/impl_runtime_ms"
    duckdb_key = f"validation/query_{query_3chars}/duckdb_runtime_ms"

    if impl_key not in statistics:
        logger.warning(
            "Key %s not found in statistics (query likely killed/timed out). Returning inf runtime.",
            impl_key,
        )
        return float("inf"), None, 0.0
    if duckdb_key not in statistics:
        logger.warning(
            "Key %s not found in statistics. Returning inf runtime.", duckdb_key
        )
        return float("inf"), None, 0.0

    # translate runtimes from ms to seconds
    last_impl_rt = float(statistics[impl_key]) / 1000
    duckdb_rt = float(statistics[duckdb_key]) / 1000

    # calculate speedup
    speedup = duckdb_rt / last_impl_rt if last_impl_rt > 0 else float("inf")

    return last_impl_rt, duckdb_rt, speedup
