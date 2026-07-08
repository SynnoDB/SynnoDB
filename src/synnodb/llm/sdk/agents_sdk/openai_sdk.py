import inspect
import logging
from typing import Any, Callable

from agents import (
    Agent,
    ApplyPatchTool,
    ModelBehaviorError,
    ModelSettings,
    Runner,
    ShellTool,
    Tool,
    ToolSearchTool,
    trace,
)
from agents.extensions.memory import AdvancedSQLiteSession

from synnodb.llm.llm_caching.cached_compaction_session import (
    CachedOpenAIResponsesCompactionSession,
)
from synnodb.llm.llm_caching.cached_litellm import CachedLitellmModel
from synnodb.llm.llm_caching.cached_openai import CachedOpenAIResponsesModel
from synnodb.llm.sdk.agents_sdk.compaction_trigger import (
    COMPACTION_TRIGGER_FRACTION,
    context_usage_at_or_above,
)
from synnodb.llm.sdk.agents_sdk.openai_make_compile_tool import make_openai_compile_tool
from synnodb.llm.sdk.agents_sdk.openai_make_run_tool import make_openai_run_tool
from synnodb.llm.sdk.agents_sdk.openai_sdk_tools import (
    make_custom_openai_apply_patch_tool,
    make_custom_openai_read_file_tool,
    make_custom_openai_replace_in_file_tool,
    make_custom_openai_shell_tool,
    make_custom_openai_write_file_tool,
)
from synnodb.llm.sdk.agents_sdk.openai_token_usage import (
    openai_get_tokens_context_and_dollar_info,
)
from synnodb.llm.sdk.sdk_wrapper import SDKWrapper
from synnodb.observability.logging.run_stats_collector import (
    SUPERVISOR_AGENT_NAME,
    RunStatsCollector,
)
from synnodb.utils.model_setup import resolve_model_extra_body, setup_model_config

logger = logging.getLogger(__name__)


class OpenAIAgentsSDKWrapper(SDKWrapper):
    def __init__(
        self,
        **args,
    ):
        super().__init__(sdk="OpenAIAgentsSDK", **args)

        use_litellm, model_name, api_key, openai_client, api_base = setup_model_config(
            self.args.model,
            api_base_override=getattr(self.args, "api_base", None),
        )

        openai_compile_tool = make_openai_compile_tool(
            compile_tool=self.compile_tool,
            defer_loading=self.args.tool_search_tool,  # if tool search tool is included, we want to load the compile tool in deferred loading mode, so that it is not loaded at the beginning of the conversation and does not take up context space and resources before it is actually needed. The tool search tool will load it when needed.
        )

        openai_run_tool = make_openai_run_tool(
            run_tool=self.run_tool,
            run_tool_offer_trace_option=self.args.run_tool_offer_trace_option,
            defer_loading=self.args.tool_search_tool,  # if tool search tool is included, we want to load the run tool in deferred loading mode, so that it is not loaded at the beginning of the conversation and does not take up context space and resources before it is actually needed. The tool search tool will load it when needed.
        )

        # assemble tools
        if not use_litellm:
            apply_patch = ApplyPatchTool(editor=self.editor)
            shell_tool = ShellTool(executor=self.shell)
        else:
            apply_patch = make_custom_openai_apply_patch_tool(editor=self.editor)
            shell_tool = make_custom_openai_shell_tool(
                shell_executor=self.shell,
            )

        self.tools: list[Tool] = [
            apply_patch,
            shell_tool,
            openai_compile_tool,
            openai_run_tool,
        ]

        # Always expose the search/replace edit tool alongside apply_patch. It
        # needs only a locally-unique old_string (no verbatim context hunks), so
        # weak local models avoid apply_patch's context-match failures.
        self.tools.append(make_custom_openai_replace_in_file_tool(editor=self.editor))

        # Also expose simpler full-file write/read primitives alongside
        # apply_patch: write_file takes raw content (no V4A diff syntax) and
        # read_file returns cat -n-style numbered lines, both modeled after
        # Claude Code's own Write/Read tools.
        self.tools.append(make_custom_openai_write_file_tool(editor=self.editor))
        self.tools.append(make_custom_openai_read_file_tool(editor=self.editor))

        if self.args.tool_search_tool:
            logger.info("Utilizing tool search tool.")
            self.tools.append(ToolSearchTool())

        #########################
        # Prepare Model and Agent
        #########################

        self.underlying_session = AdvancedSQLiteSession(
            session_id=self.args.conv_name, create_tables=True
        )

        run_stats_collector_for_trigger = self.run_stats_collector

        resolved_model_extra_body = resolve_model_extra_body(
            getattr(self.args, "model_extra_body", None)
        )

        def should_trigger_compaction_near_limit(context: dict[str, Any]) -> bool:
            # Proactively compact once we are near the model's context window so we
            # summarize BEFORE a hard overflow. The SDK consults this between turns
            # and, when True, runs compaction before the next model call (which then
            # reinserts the active stage prompt; see the session's run_compaction).
            return context_usage_at_or_above(
                run_stats_collector_for_trigger, COMPACTION_TRIGGER_FRACTION
            )

        # assemble session
        self.session = CachedOpenAIResponsesCompactionSession(
            session_id=self.args.conv_name,
            client=openai_client,
            underlying_session=self.underlying_session,
            should_trigger_compaction=should_trigger_compaction_near_limit,
            cache_dir=self.cache_path / "compaction",
            do_not_cache=self.args.do_not_cache,
            model="gpt-5.2" if not self.args.tool_search_tool else "gpt-5.4",
            run_stats_collector=self.run_stats_collector,
            runtime_tracker=self.runtime_tracker,
            use_claude_compaction=use_litellm,  # apply compaction with claude in case we use litellm wrapper
            claude_compaction_model=model_name if use_litellm else None,
            compaction_api_base=api_base,
            model_extra_body=resolved_model_extra_body,
        )

        if use_litellm:
            self.model = CachedLitellmModel(
                model=model_name,
                api_key=api_key,
                **({"base_url": api_base} if api_base else {}),
                llm_cache_dir=self.cache_path / "llm_cache",
                do_not_cache=self.args.do_not_cache,
                snapshotter=self.snapshotter,
                stop_on_cache_miss=self.args.replay
                or self.args.only_from_llm_cache
                or self.args.only_from_cache,
                config_kwargs=self.config_kwargs,
                tools_loaded_deferred=self.args.tool_search_tool,  # if tool search tool is included, we want to load the litellm wrapper in deferred loading mode, so that it is not loaded at the beginning of the conversation and does not take up context space and resources before it is actually needed. The tool search tool will load it when needed.
                run_stats_collector=self.run_stats_collector,
                runtime_tracker=self.runtime_tracker,
                working_dir=self.workspace_path_absolute,
                glm_thinking_enabled=getattr(self.args, "glm_thinking", False),
                model_extra_body=resolved_model_extra_body,
            )
            instructions = [
                f"You can edit files inside {self.workspace_path} using the apply_patch tool. ",
                "When modifying an existing file, include the file contents between ",
                "<BEGIN_FILES> and <END_FILES> in your prompt. ",
                "You can run shell commands using the shell tool. Do not emit argv form. ",
                "You can compile the code using the compile tool. ",
                "You can run a list of queries using the run tool. The run tool automatically compiles the code. You can specify the queries to run and the run mode. If no queries are specified, all queries will be run.",
            ]
        else:
            self.model = CachedOpenAIResponsesModel(
                model=model_name,
                openai_client=openai_client,
                llm_cache_dir=self.cache_path / "llm_cache",
                do_not_cache=self.args.do_not_cache,
                snapshotter=self.snapshotter,
                stop_on_cache_miss=self.args.replay
                or self.args.only_from_llm_cache
                or self.args.only_from_cache,
                config_kwargs=self.config_kwargs,  # will be included in hash
                runtime_tracker=self.runtime_tracker,
                tools_loaded_deferred=self.args.tool_search_tool,  # add this info to llm cache
                run_stats_collector=self.run_stats_collector,
                working_dir=self.workspace_path_absolute,
            )

            instructions = [
                "You are an autonomous agent. Run independently. Don't ask the user questions - try to figure unclear things out by your own. If you encounter errors or negative feedback from tools, fix them immediately without user confirmation. ",
                f"You can edit files inside {self.workspace_path} using the apply_patch tool. ",  # follows openai cookbook: https://github.com/openai/openai-agents-python/blob/main/examples/tools/apply_patch.py
                "When modifying an existing file, include the file contents between ",
                "<BEGIN_FILES> and <END_FILES> in your prompt. ",
                "You can run shell commands using the shell tool. Do not emit argv form. ",
                "You can compile the code using the compile tool. ",
                "You can run a list of queries using the run tool. The run tool automatically compiles the code. You can specify the queries to run and the run mode. If no queries are specified, all queries will be run.",
            ]

        model_settings = ModelSettings(tool_choice="auto", parallel_tool_calls=False)
        if use_litellm:
            model_settings = ModelSettings(
                tool_choice="auto", include_usage=True, parallel_tool_calls=False
            )

        self.agent = Agent(
            name=self.default_agent_name,
            model=self.model,
            instructions="".join(instructions),
            tools=self.tools,
            model_settings=model_settings,
        )

        # ================
        # SUPERVISOR AGENT
        # ================
        # session
        self.supervisor_agent = Agent(
            name=SUPERVISOR_AGENT_NAME,
            model=self.model,
            instructions=self.supervisor_agent_instruction,
        )

        # assemble session
        session_id = f"{self.conv_name}-supervision"
        self.supervisor_session = AdvancedSQLiteSession(
            session_id=session_id,
            create_tables=True,
        )

    async def run_traced(
        self, title: str, data: dict, callback: Callable, add_tools: bool = True
    ):
        customized_data = data.copy()
        if add_tools:
            customized_data["tools"] = str([type(t).__name__ for t in self.tools])

        with trace(
            workflow_name=title,
            metadata=data,
        ):
            # check if callback is async function
            if inspect.iscoroutinefunction(callback):
                return await callback()
            else:
                return callback()

    def get_total_saved_by_llm_cache(self) -> float:
        return self.model.total_saved

    async def clear_supervisor_session(self):
        await self.supervisor_session.clear_session()

    async def run_supervisor_agent(self, prompt: str, max_turns: int) -> str:
        result = await Runner.run(
            self.supervisor_agent,
            input=prompt,
            session=self.supervisor_session,
            max_turns=max_turns,
            hooks=self.run_stats_collector,
        )

        output = result.final_output
        return output

    async def run_one_off_completion(
        self, prompt: str, max_tokens: int | None = None
    ) -> str:
        # Tool-less, session-less agent reusing self.model - the same
        # CachedLitellmModel/CachedOpenAIResponsesModel instance already resolved
        # once (backend, api_base/api_key) for this run, so a one-off check gets
        # the correct endpoint, response caching, and cost/logging for free instead
        # of re-deriving them (and only ever supporting litellm) by hand.
        judge_agent = Agent(
            name="One-off Judge",
            model=self.model,
            instructions="",
            model_settings=ModelSettings(max_tokens=max_tokens),
        )
        result = await Runner.run(
            judge_agent,
            input=prompt,
            max_turns=1,
            hooks=self.run_stats_collector,
        )
        return result.final_output

    async def run_agent(
        self,
        prompt: str,
        max_turns: int,
        run_stats_collector: RunStatsCollector,
        short_desc: str | None = None,
    ) -> str:
        ## 2026/03/14: Experimentation with prompt_cache_key for openai models - no increased caching ratio observed.
        # # extract model name
        # if isinstance(model, str):
        #     model_name = model
        # else:
        #     model_name = str(model)

        # # assemble prompt-cache-key for openai
        # agent_model_settings = agent.model_settings
        # if model_name.startswith("openai/") or "gpt-" in model_name.lower():
        #     assert agent_model_settings is not None, (
        #         "Model settings must be provided for OpenAI models to use prompt caching"
        #     )

        #     # add prompt cache key
        #     extra_args = (
        #         agent_model_settings.extra_args
        #         if agent_model_settings.extra_args is not None
        #         else {}
        #     )
        #     extra_args = extra_args.copy()  # make a copy to avoid mutating original

        #     extra_args["prompt_cache_key"] = f"{model_name}:{idx}"

        #     model_settings = copy(agent_model_settings)
        #     model_settings.extra_args = extra_args
        # else:
        #     model_settings = agent_model_settings

        # Rename the agent for each stage based on the short description - this makes it easier to analyze the tracing logs and see which stage is producing which output, without having to rely on the prompt content which might be very long. The name will be reset to default_agent_name if short_desc is None, which is the case for normal prompts that are not associated with a specific stage.
        # We rewrite it to hack a different header for each stage into the tracing log.
        # THIS IS RISKY: if openai somehow refers to agent.name this is a problem, since it will be not an identifier anymore.
        if short_desc is None:
            workflow_name = self.default_agent_name
        else:
            workflow_name = f"{self.default_agent_name} ({short_desc})"
        self.agent.name = workflow_name

        try:
            result = await Runner.run(
                self.agent,
                input=prompt,
                session=self.session,
                max_turns=max_turns,
                hooks=run_stats_collector,
                # run_config=RunConfig(model_settings=model_settings),
            )
        except ModelBehaviorError as e:
            logger.error(f"Error runing agent: {prompt}\n{str(e)}")
            raise e

        # Log cost summary
        openai_get_tokens_context_and_dollar_info(
            result.context_wrapper.usage,
            str(self.model),
            last_entry_only=False,
            log=True,
        )

        return result.final_output

    async def run_compaction(self):
        # Caller-initiated compaction (marker or reactive overflow). reanchor=False:
        # the caller re-issues the task prompt itself, so do not reinsert it here.
        await self.session.run_compaction(
            {"force": True, "compaction_mode": "input"}, reanchor=False
        )

    async def get_conversation_turns(self) -> int:
        turns = await self.underlying_session.get_conversation_turns()
        if len(turns) == 0:
            return 0

        return turns[-1]["turn"]

    async def switch_to_conversation_branch(self, branch_name: str):
        # switch branch in underlying session

        branches = await self.underlying_session.list_branches()
        if len(branches) == 0:
            return  # no branches to switch to, likely the case at the beginning of the conversation - do nothing

        branch_names = [b["branch_id"] for b in branches]
        if branch_name not in branch_names:
            logger.error(
                f"Branch {branch_name} not found in underlying session. Available branches: {branches}"
            )
            raise Exception(f"Branch {branch_name} not found in underlying session.")

        await self.underlying_session.switch_to_branch(branch_name)

    async def list_conversation_branches(self) -> list[str]:
        branches = await self.underlying_session.list_branches()
        branch_names = [b["branch_id"] for b in branches]
        return branch_names

    async def create_conversation_branch_from_turn(
        self, branch_name: str, turn_nr: int
    ) -> str:
        # create branch from turn in underlying session
        return await self.underlying_session.create_branch_from_turn(
            turn_nr, branch_name=branch_name
        )

    def last_llm_call_was_cached(self) -> bool:
        return self.model.llm_was_cached
