import logging
from pathlib import Path
from typing import Optional

from conversations.checkpointed_conversation import (
    CheckpointedConversation,
    extract_speedup_of_last_snapshot,
)
from conversations.conversation import (
    VALIDATE_OFF,
)
from conversations.ff.prompts_gen import base_ff_planner_prompt
from conversations.filenames import get_filenames
from conversations.prompts_gen import (
    base_fix_slow_queries_prompt,
)
from conversations.stage_config import (
    StageConfig,
    StaticStageConfig,
)
from tools.run_tool_mode import RunToolMode
from workloads.workload_provider import Workload

logger = logging.getLogger(__name__)


class BaseFFImplConversation(CheckpointedConversation):
    def __init__(
        self,
        benchmark: Workload,
        workspace_path: Path,
        sql_dict: dict[str, str],
        parquet_dir: Path,
        read_storage_plan: bool = False,
        sample_query_args_dict: Optional[dict[str, str]] = None,
        use_master_prompt: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.benchmark = benchmark
        self.read_storage_plan = read_storage_plan
        self.sample_query_args_dict = sample_query_args_dict
        self.workspace_path = workspace_path
        self.use_master_prompt = use_master_prompt
        self.sql_dict = sql_dict
        self.parquet_dir = parquet_dir

    def _validate_no_crazy_slow_queries(
        self, query_ids: list[str], query_impl_path: str, builder_path: str
    ) -> str | None:
        """Check all queries for critical slowness (speedup < 0.2x vs DuckDB).

        Returns a fix prompt if any slow queries are found, None otherwise.
        """
        slow_queries = []
        for qid in query_ids:
            _, metrics, _ = self.run_tool.run(
                mode=RunToolMode.BENCHMARK,
                optimize=True,
                query_ids=[qid],
                trace_mode=False,
                external_call=True,
            )
            if metrics is None:
                continue
            try:
                impl_rt_s, duckdb_rt_s, speedup = extract_speedup_of_last_snapshot(
                    metrics, qid
                )
                if speedup < 0.2:
                    assert duckdb_rt_s is not None, "DuckDB runtime is None"
                    slow_queries.append(
                        (qid, impl_rt_s * 1000, duckdb_rt_s * 1000, speedup)
                    )
            except (AssertionError, KeyError) as e:
                logger.warning(f"Could not extract speedup for query {qid}: {e}")

        if not slow_queries:
            return None

        logger.warning(
            f"Found {len(slow_queries)} critically slow queries: "
            + ", ".join(
                f"{qid} ({speedup:.3f}x)" for qid, _, _, speedup in slow_queries
            )
        )
        return base_fix_slow_queries_prompt(
            slow_queries,
            query_impl_path=query_impl_path,
            builder_path=builder_path,
        )

    async def run(self):
        # reset used prompts to empty and start from the beginning of the conversation
        self.used = []

        # fetch stages
        stage_list = self.assemble_stages()

        # register planned steps
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info(stage_list)

        if self.use_master_prompt:
            prompt_prefix = "# MASTER PROMPT:\n You are an autonomous agent - solve your task without asking questions! Do everything that is necessary to solve your task (including invoking Shell, Apply-Patch, Compile and Run Tools). You will not get external feedback. Solve your task.\n\nYour task:\n"
        else:
            prompt_prefix = None

        await self._run_stages(stage_list, prompt_pretext=prompt_prefix)

    def assemble_stages(self) -> list[StageConfig | str]:

        # ==========
        # Paths & Co
        # ==========

        # pick a representative query from the middle of the available set
        _mid = len(self.all_query_ids) // 2
        example_query = list(self.sql_dict.keys())[_mid]
        example_query_params = self.all_query_ids[_mid]

        # paths
        filenames_dict = get_filenames("bff")
        queries_path = filenames_dict["queries_path"]
        builder_path = filenames_dict["builder_path"]
        builder_cpp_path = filenames_dict["builder_cpp_path"]
        builder_hpp_path = filenames_dict["builder_hpp_path"]
        query_impl_path = filenames_dict["query_impl_path"]
        args_path = filenames_dict["args_path"]
        base_impl_todo_filename = filenames_dict["base_impl_todo_filename"]
        plan_filename = filenames_dict["plan_filename"]

        def _validate_plan_exists() -> str | None:
            plan_path = self.workspace_path / base_impl_todo_filename
            if plan_path.exists():
                return None  # plan exists, proceed to next stage
            else:
                # return prompt to llm to fix this.
                logger.error(
                    f"Todo file {plan_path} does not exist. Reprompting the LLM now"
                )
                return f"Your task was to create an implementation plan. However, no implementation plan called `{base_impl_todo_filename}` exists in your workspace. Please create a plan and write it to `{base_impl_todo_filename}` before proceeding to the implementation stage."

        # =========
        # Stage Defs
        # =========

        stage_list = [
            VALIDATE_OFF,
            StaticStageConfig(
                descriptor="base impl planner",
                get_prompt=lambda _exec_settings, _rt: base_ff_planner_prompt(
                    queries_path=queries_path,
                    num_queries=len(self.all_query_ids),
                    builder_path=builder_path,
                    read_storage_plan=self.read_storage_plan,
                    storage_plan_path=plan_filename,
                    query_impl_path=query_impl_path,
                    example_query=example_query,
                    example_query_params=example_query_params,
                    args_path=args_path,
                    parquet_path=self.parquet_dir.as_posix(),
                    base_impl_todo_file=base_impl_todo_filename,
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                # if a plan exists, validate that it is correct before proceeding to impl stage. If it is not correct, go back to planner stage
                post_stage_validate=_validate_plan_exists,
            ),
        ]

        return stage_list
