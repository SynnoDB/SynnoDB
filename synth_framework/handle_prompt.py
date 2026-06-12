import logging
import re

from agents import MaxTurnsExceeded, ModelBehaviorError
from agents.exceptions import UserError
from litellm.exceptions import BadRequestError, InternalServerError

from conversations.conversation import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
)
from llm.sdk.sdk_wrapper import SDKWrapper
from observability.logging.run_stats_collector import RunStatsCollector
from observability.logging.truncate_model_log import truncate_model_final_output
from tools.run import RunTool
from tools.run_tool_mode import RunToolMode
from tools.tool_call_error_logger import log_tool_call_error
from tools.validate.query_validator_class import QueryValidator

logger = logging.getLogger(__name__)


async def handle_prompt(
    text: str,
    short_desc: str | None,
    idx: int,
    run_tool: RunTool,
    run_stats_collector: RunStatsCollector,
    agent_sdk_wrapper: SDKWrapper,
    query_validator: QueryValidator | None,
    max_turns: int | None = None,
    prompt_already_printed: bool = False,
) -> str | None:
    # set default max_turns value
    if max_turns is None:
        max_turns = 75 * 3

    # check for compaction marker in the prompt string - in this case run compaction and return
    if text == COMPACTION_MARKER:
        logger.info(f"Triggering compaction at prompt index {idx}")
        await agent_sdk_wrapper.run_compaction()
        return None
    # perform benchmarking
    if text == BENCHMARK_MARKER:
        logger.info(f"Triggering benchmarking at prompt index {idx}")
        run_tool.run(
            mode=RunToolMode.EXHAUSTIVE,
            optimize=True,
            query_ids=None,
            trace_mode=False,
            external_call=True,
        )
        return None

    # check for markers to enable / disable validation
    if text == VALIDATE_ON:
        run_tool.parse_out_and_validate_output = True
        logger.info(f"Enabled output parsing and validation at prompt index {idx}")
        return None
    if text == VALIDATE_OFF:
        run_tool.parse_out_and_validate_output = False
        logger.info(f"Disabled output parsing and validation at prompt index {idx}")
        return None
    if text == VALIDATE_OUTPUT_STDOUT_ON:
        assert query_validator is not None
        query_validator.output_stdout_stderr = True
        logger.info(
            f"Enabled output stdout in validation results at prompt index {idx}"
        )
        return None
    if text == VALIDATE_OUTPUT_STDOUT_OFF:
        assert query_validator is not None
        query_validator.output_stdout_stderr = False
        logger.info(
            f"Disabled output stdout in validation results at prompt index {idx}"
        )
        return None

    if not prompt_already_printed:
        logger.info("=" * 80)
        logger.info(text)
        logger.info("=" * 80)

    # Update prompt index in hooks

    run_stats_collector.prompt_idx = idx
    run_stats_collector.current_prompt = text
    run_stats_collector.current_prompt_descriptor = short_desc

    # Run with hooks for automatic metric tracking
    try:
        final_output = await agent_sdk_wrapper.run_agent(
            text, max_turns, run_stats_collector, short_desc=short_desc
        )
    except MaxTurnsExceeded as e:
        logger.error(
            f"Max turns exceeded while running agent: {e}. Reprompting to finish NOW!"
        )
        desc = (
            f"{short_desc} - reprompted after max turns exceeded"
            if short_desc
            else "reprompted after max turns exceeded"
        )

        final_output = await agent_sdk_wrapper.run_agent(
            "You exceeded the maximum number of turns. FINISH NOW!",
            10,
            run_stats_collector,
            short_desc=desc,
        )
    except ModelBehaviorError as e:
        tool_not_found_regex = r"Tool (.*?) not found in agent"
        match = re.search(tool_not_found_regex, str(e))
        if match:
            missing_tool = match.group(1)
            logger.error(
                f"Model tried to use tool '{missing_tool}' which is not in the agent's tool list. Continue run"
            )

            final_output = await agent_sdk_wrapper.run_agent(
                f"The requested tool {missing_tool} is not available. Please continue without using this tool and finish the task.",
                max_turns,
                run_stats_collector,
                short_desc=short_desc,
            )

        else:
            raise e

    except UserError as e:
        if "invalid json" in str(e).lower() or "eof while parsing" in str(e).lower():
            logger.warning(f"Model generated malformed tool call JSON: {e}. Retrying.")
            last_args = getattr(agent_sdk_wrapper.model, "last_tool_call_args", None)
            log_tool_call_error(
                error_type="UserError",
                error=e,
                model=run_stats_collector.model,
                turn=run_stats_collector.last_turn,
                raw_tool_calls=last_args,
            )
            final_output = await agent_sdk_wrapper.run_agent(
                "Your previous tool call had malformed/truncated JSON arguments. Please retry the operation with valid JSON.",
                max_turns,
                run_stats_collector,
                short_desc=short_desc,
            )
        else:
            raise e

    except BadRequestError as e:
        if "exceeds" in str(e).lower() or "context size" in str(e).lower():
            logger.warning(
                f"Context size exceeded: {e}. Running compaction and retrying."
            )
            await agent_sdk_wrapper.run_compaction()
            final_output = await agent_sdk_wrapper.run_agent(
                text,
                max_turns,
                run_stats_collector,
                short_desc=short_desc,
            )
        else:
            raise e

    except InternalServerError as e:
        if "failed to parse" in str(e).lower():
            logger.warning(
                f"Model generated malformed tool call (InternalServerError): {e}. Retrying with fresh instructions."
            )
            last_args = getattr(agent_sdk_wrapper.model, "last_tool_call_args", None)
            log_tool_call_error(
                error_type="InternalServerError",
                error=e,
                model=run_stats_collector.model,
                turn=run_stats_collector.last_turn,
                raw_tool_calls=last_args,
            )
            final_output = await agent_sdk_wrapper.run_agent(
                "Your previous tool call used an invalid format (XML-style instead of JSON). Please retry the operation using proper JSON tool call format.",
                max_turns,
                run_stats_collector,
                short_desc=short_desc,
            )
        else:
            raise e

    except AssertionError as e:
        if "ResponseOutputMessage" in str(e):
            xml_calls = getattr(agent_sdk_wrapper.model, "last_xml_tool_calls", None)
            if xml_calls:
                from llm.glm.glm_xml_tool_call_parser import format_for_reprompt

                call_summary = format_for_reprompt(xml_calls)
                reprompt = (
                    "Your reasoning block contained XML tool calls that were NOT executed "
                    "(writing tool syntax inside <think> does not run the tool). "
                    "The following tool calls were detected in your reasoning:\n\n"
                    f"{call_summary}\n\n"
                    "Please call these tools NOW using proper JSON function call format. "
                    "Use the exact arguments shown above — do not re-derive them."
                )
            else:
                reprompt = (
                    "Your previous response contained only reasoning/thinking but no actual tool call or text message. "
                    "You may have written tool call syntax (e.g. apply_patch, shell, etc.) inside your thinking block — that does NOT execute the tool. "
                    "You MUST invoke the tool explicitly by calling it as a proper function call (JSON format). Please redo the action using the actual tool."
                )
            logger.warning(
                "Model returned reasoning-only response: %s. XML calls found: %s. Reprompting.",
                e,
                bool(xml_calls),
            )
            final_output = await agent_sdk_wrapper.run_agent(
                reprompt,
                max_turns,
                run_stats_collector,
                short_desc=short_desc,
            )
        else:
            logger.error(f"AssertionError while running agent: {e}\n{text}")
            raise e

    except Exception as e:
        logger.error(f"Error occurred while running agent: {e}\n{text}")
        raise e

    # Detect reasoning-only response: agents SDK returns empty final_output when the model
    # produced only a reasoning block (no tool call, no text message). GLM-5.1 sometimes
    # writes tool calls as XML inside its thinking block — extract them and reprompt so
    # the model only needs to emit the JSON call, not re-derive the arguments.
    if not final_output or not final_output.strip():
        xml_calls = getattr(agent_sdk_wrapper.model, "last_xml_tool_calls", None)
        if xml_calls:
            from llm.glm.glm_xml_tool_call_parser import format_for_reprompt

            call_summary = format_for_reprompt(xml_calls)
            reasoning_reprompt = (
                "Your reasoning block contained XML tool calls that were NOT executed "
                "(writing tool syntax inside <think> does not run the tool). "
                "The following tool calls were detected in your reasoning:\n\n"
                f"{call_summary}\n\n"
                "Please call these tools NOW using proper JSON function call format. "
                "Use the exact arguments shown above — do not re-derive them."
            )
        else:
            reasoning_reprompt = (
                "Your previous response contained only reasoning/thinking but no actual tool call or text message. "
                "You may have written tool call syntax inside your thinking block — that does NOT execute the tool. "
                "You MUST invoke the tool explicitly by calling it as a proper function call (JSON format). "
                "Please redo the action using the actual tool."
            )
        logger.warning(
            "Model returned empty output (reasoning-only response). XML calls found: %s. Reprompting.",
            bool(xml_calls),
        )
        final_output = await agent_sdk_wrapper.run_agent(
            reasoning_reprompt,
            max_turns,
            run_stats_collector,
            short_desc=short_desc,
        )

    # log final output (truncated)
    logger.info("=" * 20 + " LLM Final Output " + "=" * 20)
    logger.info(truncate_model_final_output(final_output))
    logger.info("=" * 60)

    return final_output
