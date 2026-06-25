import logging

from agents import Usage
from openai.types.responses import ResponseUsage

from synnodb.llm.models import context_window_usage, request_cost_usd

logger = logging.getLogger(__name__)


def openai_get_tokens_context_and_dollar_info(
    usage: Usage | ResponseUsage,
    model: str,
    last_entry_only: bool = True,
    log: bool = False,
):
    if isinstance(usage, ResponseUsage):
        assert last_entry_only, "last_entry_only must be True for ResponseUsage"
        num_llm_request = 1
        elem = usage
        last_entry = usage
    elif isinstance(usage, Usage):
        # get last entry or overall usage
        last_entry = usage.request_usage_entries[-1]
        if last_entry_only:
            elem = last_entry
            num_llm_request = 1
        else:
            elem = usage
            num_llm_request = len(usage.request_usage_entries)
    else:
        raise Exception("Unsupported usage type")

    # extract stats
    input_tokens = elem.input_tokens
    output_tokens = elem.output_tokens

    # extract cached stats (OpenAI API cache - not local cache!)
    cached_tokens = elem.input_tokens_details.cached_tokens or 0
    reasoning_tokens = elem.output_tokens_details.reasoning_tokens or 0

    # compute context window usage
    last_request_input = last_entry.input_tokens
    last_request_output = last_entry.output_tokens
    try:
        usage_str, usage_float = context_window_usage(
            model, last_request_input + last_request_output
        )
        cost = request_cost_usd(model, input_tokens, cached_tokens, output_tokens)
    except KeyError as e:
        usage_str = "n/a"
        usage_float = 0.0
        cost = 0.0
        raise e

    if log:
        logger.info(
            f"Context window usage: {usage_str} | Input tokens: {input_tokens} (cached: {cached_tokens}), Output tokens: {output_tokens} (reasoning: {reasoning_tokens}) | Estimated cost: ${cost:0.6f} | LLM requests: {num_llm_request}"
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens - reasoning_tokens,  # exclude reasoning tokens
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "context_window_usage_str": usage_str,
        "context_window_usage": usage_float,
        "cost": cost,
        "num_llm_request": num_llm_request,
    }


# async def print_token_usage(session: AdvancedSQLiteSession):
#     # Get session-level usage (all branches)
#     session_usage = await session.get_session_usage()
#     if session_usage:
#         logger.info(
#             f"Total (billed) tokens: {session_usage['total_tokens']} (requests: {session_usage['requests']}, input: {session_usage['input_tokens']}, output: {session_usage['output_tokens']}, turns: {session_usage['total_turns']})"
#         )
#         # print(session_usage)

#     # # Get usage for specific branch
#     # branch_usage = await session.get_session_usage(branch_id="main")

#     # # Get usage by turn
#     # turn_usage = await session.get_turn_usage()
#     # for turn_data in turn_usage:
#     #     logger.info(
#     #         f"Turn {turn_data['user_turn_number']}: {turn_data['total_tokens']} (in: {turn_data['input_tokens']}, out: {turn_data['output_tokens']}) tokens"
#     #     )
#     #     # print(turn_data)
