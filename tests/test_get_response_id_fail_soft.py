"""Regression for the gpt-5.4 crash: get_response_id must fail soft.

When the last output item is a tool/shell call (which carries no provider_data),
cache-status consumption used to `assert hasattr(..., "provider_data")` and crash
the whole agent loop. It must instead return None (best-effort), like the
reasoning-only branch already does.
"""

from agents import ModelResponse
from agents.usage import Usage
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from synnodb.observability.logging.run_stats_collector import get_response_id


def test_returns_none_when_last_item_is_a_tool_call_without_provider_data():
    tool_call = ResponseFunctionToolCall.model_construct()  # no provider_data attr
    assert not hasattr(tool_call, "provider_data")

    resp = ModelResponse(output=[tool_call], usage=Usage(), response_id="resp-x")
    # Must not raise, and must return None (skip cache status).
    assert get_response_id(resp) is None


def test_returns_none_on_empty_output():
    resp = ModelResponse(output=[], usage=Usage(), response_id=None)
    assert get_response_id(resp) is None


def test_returns_response_id_from_reasoning_only_output():
    reasoning = ResponseReasoningItem.model_construct(
        provider_data={"response_id": "reasoning-response"}
    )
    resp = ModelResponse(output=[reasoning], usage=Usage(), response_id=None)

    assert get_response_id(resp) == "reasoning-response"
