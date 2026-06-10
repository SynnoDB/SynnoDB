import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents import ModelResponse, RunContextWrapper, RunHooks, TContext, Tool
from openai.types.responses.response_apply_patch_tool_call import (
    ResponseApplyPatchToolCall,
)
from openai.types.responses.response_function_shell_tool_call import (
    ResponseFunctionShellToolCall,
)
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage

from conversations.prompts_gen import SUPERVISION_SUCCESS_KW
from llm.sdk.agents_sdk.openai_token_usage import (
    openai_get_tokens_context_and_dollar_info,
)
from observability.logging.cloc_utils import calculate_loc
from observability.logging.run_stats_drain import DataDrain
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from utils.utils import create_dir_and_set_permissions

logger = logging.getLogger(__name__)

SUPERVISOR_AGENT_NAME = "Supervision Agent"


class RunStatsCollector(RunHooks):
    """Base hooks class for tracking agent execution metrics"""

    logged_turn = -1
    apply_patch_added_ctr = 0
    apply_patch_deleted_ctr = 0
    apply_patch_str = ""
    apply_patch_files = set()
    apply_patch_failed = []

    def __init__(
        self,
        model,
        git_snapshotter: GitSnapshotter,
        prompt_idx: int = 0,
        cloc_cache_dir: Path | None = None,
        runtime_tracker: Optional[RuntimeTracker] = None,
        do_not_cache: bool = False,
        drains: list[DataDrain] | None = None,
    ):
        self.model = model
        self.git_snapshotter = git_snapshotter
        self.drains: list[DataDrain] = drains if drains is not None else []
        self.prompt_idx = prompt_idx  # will be externally set by conversation loop
        self.runtime_tracker = runtime_tracker
        self.current_prompt: Optional[str] = (
            None  # will be externally set by conversation loop
        )
        self.current_prompt_descriptor: Optional[str] = (
            None  # set externally by conversation loop
        )

        if len(self.drains) == 0:
            logger.warning(
                "No data drains provided to RunStatsCollector - metrics will not be emitted anywhere!"
            )

        self.current_turn_tools = {}
        self.last_turn = 0  # Track last known turn for validation callback

        self.total_stats = defaultdict(float)

        self.total_type_counts = defaultdict(
            int
        )  # llm-call, handoff, (+ specific tool calls)

        # per tool stats
        self.apply_patch_stats = defaultdict(int)

        self.cloc_cache_dir = cloc_cache_dir
        self.do_not_cache = do_not_cache
        if self.cloc_cache_dir is not None:
            create_dir_and_set_permissions(self.cloc_cache_dir)

        # summary of activities -utilized by supervision agent to keep track of what has been done in the turn and decide what to do next based on that
        self.activity_summary = []
        self.last_llm_hash: str | None = None
        self._llm_answered_from_cache_by_response_id: dict[str, bool] = {}

        # store metrics locally + also emit to external system in _emit_metrics (override in subclass)
        self.metrics_list = []

    def record_llm_cache_status(
        self,
        answered_from_cache: bool,
        response_id: str | None = None,
        request_hash: str | None = None,
    ) -> None:
        if request_hash is not None:
            self.last_llm_hash = request_hash
        if response_id is not None:
            self._llm_answered_from_cache_by_response_id[response_id] = (
                answered_from_cache
            )

    def _consume_llm_cache_status(self, output: ModelResponse) -> bool:
        response_id = get_response_id(output)

        answered_from_cache = self._llm_answered_from_cache_by_response_id.get(
            response_id, None
        )
        if answered_from_cache is not None:
            return answered_from_cache
        return False

    def _emit_metrics(self, metrics: dict, step: int) -> None:
        """Fan out metrics to all registered data drains."""
        for drain in self.drains:
            drain.emit(metrics, step)

    def add_to_activity_summary(self, entry: str) -> None:
        """Add an entry to the activity summary"""
        self.activity_summary.append(entry)

    # Callback for validation tool to report metrics
    def log_metrics_callback(
        self, metrics: dict, log_and_increment: bool = False
    ) -> None:
        """Callback for validation tool to report query-specific metrics"""
        # Use the last known turn from hooks
        turn = self.last_turn

        assert self.logged_turn + 1 == turn, (
            f"Logged turn {self.logged_turn} is not one behind current turn {turn}"
        )
        assert log_and_increment, "log_and_increment must be True to increment turn"

        # Bump both counters up-front so the invariant `logged_turn + 1 == last_turn`
        # holds even if the rest of this method raises (e.g. drain/cloc failure).
        # Without this, a transient error would poison turn accounting for the
        # remainder of the run and trigger this same assertion on the next call.
        self.logged_turn = turn
        self.last_turn += 1

        metrics["turn"] = turn

        # Track total counts per type
        self.total_type_counts[metrics["type"]] += 1

        # assemble full action list
        action_names = [
            "llm",
            "apply_patch",
            "handoff",
            "shell",
            "compile",
            "validate",
            "compaction",
        ]
        for a in self.total_type_counts.keys():
            if a not in action_names:
                action_names.append(a)

        # log total counts
        for action in action_names:
            action_str = action.replace("_", "")  # strip _ from type for action_str
            metrics[f"tool/{action_str}_count"] = self.total_type_counts[action]

        metrics["code/snapshot_hash"] = self.git_snapshotter.current_hash
        assert self.git_snapshotter.current_hash is not None, (
            "Current hash should not be None"
        )
        metrics["code/loc"] = calculate_loc(
            self.cloc_cache_dir,
            self.git_snapshotter.current_hash,
            self.git_snapshotter.working_dir,
            self.do_not_cache,
        )
        metrics["total/runtime"] = (
            self.runtime_tracker.retrieve_total_time() if self.runtime_tracker else None
        )
        metrics["wallclock_time"] = datetime.now().isoformat()

        self.metrics_list.append(metrics)
        self._emit_metrics(metrics, step=turn)

    def log_apply_patch_stats(
        self,
        operation_type: str,
        added_lines: int,
        deleted_lines: int,
        string_diff: str,
        file_touched: str,
        failed: str | None = None,
    ) -> None:
        """Log apply patch operation stats"""
        self.apply_patch_stats[operation_type] += 1
        self.apply_patch_added_ctr += added_lines
        self.apply_patch_deleted_ctr += deleted_lines
        self.apply_patch_str += string_diff + "\n"
        self.apply_patch_files.add(file_touched)
        if failed is not None:
            self.apply_patch_failed.append(failed)

    async def on_agent_start(self, ctx, agent):
        """Called when an agent starts processing"""
        logger.debug(f"Agent {agent.name} started (turn {self.last_turn})")

    async def on_llm_end(self, ctx, agent, output: ModelResponse):
        """Called after each LLM call completes - log metrics here for accurate per-turn tracking"""

        # Get usage from context
        assert hasattr(ctx, "usage"), "Context missing usage attribute"
        usage = ctx.usage

        # retrieve num tokens
        token_stats = openai_get_tokens_context_and_dollar_info(
            usage, self.model, last_entry_only=True, log=False
        )

        assert token_stats["num_llm_request"] == 1, (
            "Expected single LLM request for last entry"
        )
        calculatorial_cost_usd = token_stats["cost"]
        answered_from_cache = self._consume_llm_cache_status(output)
        real_cost_usd = 0.0 if answered_from_cache else calculatorial_cost_usd
        logger.info(
            f"LLM ended: Turn {self.last_turn} - Input tokens: {token_stats['input_tokens']}, Output tokens: {token_stats['output_tokens']}, Calculatorial cost: ${calculatorial_cost_usd:0.6f}, Real cost: ${real_cost_usd:0.6f}, Context window usage: {token_stats['context_window_usage'] * 100:.1f}%. Hash: {self.last_llm_hash}"
        )

        # Build metrics
        metrics = {
            "type": "llm",
            "prompt_idx": self.prompt_idx,
            "agent_name": agent.name,
            "cost_usd": calculatorial_cost_usd,
            "real_cost_usd": real_cost_usd,
            "answered_from_cache": answered_from_cache,
            "input_tokens": token_stats["input_tokens"],
            "cached_tokens": token_stats["cached_tokens"],
            "output_tokens": token_stats["output_tokens"],
            "reasoning_tokens": token_stats["reasoning_tokens"],
            "context_window_usage": token_stats["context_window_usage"],
            "current_prompt": self.current_prompt,
            "current_prompt_descriptor": self.current_prompt_descriptor,
            "llm_hash": self.last_llm_hash,
        }

        # extract msg from output object
        assert len(output.output) > 0, (
            f"Expected at least one output object from LLM response. Got {len(output.output)}"
        )
        output_text = []
        for output_obj in output.output:
            if isinstance(output_obj, ResponseOutputMessage):
                assert isinstance(output_obj, ResponseOutputMessage), (
                    f"Expected output object of type ResponseOutputMessage. Got {type(output_obj)}"
                )
                assert len(output_obj.content) == 1, (
                    f"Expected single content item in output message. Got {len(output_obj.content)}"
                )
                output_text.append(output_obj.content[0].text)  # type: ignore
            elif isinstance(
                output_obj,
                (
                    ResponseFunctionShellToolCall,
                    ResponseApplyPatchToolCall,
                    ResponseFunctionToolCall,
                ),
            ):
                continue
            else:
                output_text.append(str(output_obj))
        # join outputs together
        output_text = "\n".join(output_text)

        if output_text.strip() != "":
            logger.info(f"LLM output: {output_text}")

        if agent.name == SUPERVISOR_AGENT_NAME:
            # add additional metrics for supervisor
            metrics["supervisor"] = True

            metrics["supervisor/approved"] = output_text == SUPERVISION_SUCCESS_KW

        # reset current prompt
        self.current_prompt = None
        self.current_prompt_descriptor = None

        self.total_stats["input_tokens"] += token_stats["input_tokens"]
        self.total_stats["cached_tokens"] += token_stats["cached_tokens"]
        self.total_stats["output_tokens"] += token_stats["output_tokens"]
        self.total_stats["reasoning_tokens"] += token_stats["reasoning_tokens"]
        self.total_stats["cost_usd"] += token_stats["cost"]
        self.total_stats["real_cost_usd"] += real_cost_usd

        # total info
        metrics.update(
            {
                "total/input_tokens": self.total_stats["input_tokens"],
                "total/cached_tokens": self.total_stats["cached_tokens"],
                "total/output_tokens": self.total_stats["output_tokens"],
                "total/reasoning_tokens": self.total_stats["reasoning_tokens"],
                "total/cost_usd": self.total_stats["cost_usd"],
                "total/real_cost_usd": self.total_stats["real_cost_usd"],
                "llm/output_text": output_text[
                    :1000
                ],  # log only first 1000 chars of output
            }
        )

        self.log_metrics_callback(metrics, log_and_increment=True)

    async def on_agent_end(self, ctx, agent, output):
        """Called when an agent finishes processing"""
        logger.debug(f"Agent {agent.name} ended (turn {self.last_turn})")

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent,
        tool: Tool,
    ):
        """Called when a tool starts executing"""
        tool_name = tool.name if hasattr(tool, "name") else str(tool)
        logger.debug(f"starting tool: {tool_name} (turn {self.last_turn})")

        if tool_name == "apply_patch":
            self.apply_patch_added_ctr = 0
            self.apply_patch_deleted_ctr = 0
            self.apply_patch_str = ""
            self.apply_patch_files = set()
            self.apply_patch_failed = []

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent,
        tool: Tool,
        result: str,
    ):
        """Called when a tool finishes - track tool usage"""

        # stats logging happens inside tools with callback
        tool_name = tool.name if hasattr(tool, "name") else str(tool)
        self.current_turn_tools[tool_name] = (
            self.current_turn_tools.get(tool_name, 0) + 1
        )

        if tool_name == "apply_patch":
            operation_type_dict = dict()
            for operation_type, count in self.apply_patch_stats.items():
                operation_type_dict[f"apply_patch/{operation_type}_count"] = count

            self.log_metrics_callback(
                {
                    "type": "apply_patch",
                    "apply_patch/added_loc_count": self.apply_patch_added_ctr,
                    "apply_patch/deleted_loc_count": self.apply_patch_deleted_ctr,
                    "apply_patch/string": self.apply_patch_str[:1000],
                    "apply_patch/files": sorted(list(self.apply_patch_files)),
                    "apply_patch/failed": self.apply_patch_failed
                    if len(self.apply_patch_failed) > 0
                    else None,
                    **operation_type_dict,
                },
                log_and_increment=True,
            )

    async def on_handoff(self, ctx, from_agent, to_agent):
        """Called when control is handed off between agents"""
        raise Exception(
            "handoff loging not implemented yet! Log to wandb and co please"
        )

        logger.info(
            f"Handoff from {from_agent.name} to {to_agent.name} (turn {self.last_turn})"
        )
        self._emit_metrics(
            {
                "handoff/from": from_agent.name,
                "handoff/to": to_agent.name,
                "type": "handoff",
            },
            step=self.last_turn,
        )


def get_response_id(output: ModelResponse):
    msgs = [
        o
        for o in output.output
        if isinstance(o, ResponseOutputMessage)
        or isinstance(o, ResponseFunctionShellToolCall)
        or isinstance(o, ResponseApplyPatchToolCall)
        or isinstance(o, ResponseFunctionToolCall)
    ]
    if len(msgs) == 0:
        logger.warning(
            "No ResponseOutputMessage in output (reasoning-only response). Skipping cache status."
        )
        return None

    if len(msgs) > 1:
        types_str = ", ".join(type(m).__name__ for m in msgs)
        logger.warning(
            f"Expected single ResponseOutputMessage in output for cache status. Got {len(msgs)} [{types_str}]. Using last one for cache status."
        )
    last_response = msgs[-1]
    assert hasattr(last_response, "provider_data"), (
        f"Expected last response object to have provider_data attribute to consume cache status. Got {type(last_response)} with attributes {dir(last_response)}"
    )
    response_id = last_response.provider_data["response_id"]  # type: ignore

    assert response_id is not None, (
        f"Expected response_id in ModelResponse to consume cache status: {output}"
    )
    return response_id
