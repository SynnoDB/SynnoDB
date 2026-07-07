"""_judge_storage_plan(): backend-agnostic via the agent SDK wrapper.

The judge is a one-off LLM completion outside the main conversation. It used to call
litellm directly (only supporting litellm-routed models) and compute its own cost via
litellm.completion_cost. These tests pin the current behaviour:
- it goes through ctx.agent_sdk_wrapper.run_one_off_completion, so it automatically
  supports whichever backend the run actually uses (litellm or native OpenAI) instead
  of hand-resolving litellm-specific config;
- a call failure (bad endpoint, transient error) still degrades to "skip the check"
  rather than crashing the stage - that's a runtime condition and must not block;
- a missing agent_sdk_wrapper is a wiring bug, not a runtime condition (main.py
  always constructs one before building ConvContext), so it asserts loudly instead
  of silently skipping the check.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.examples.storage_plan import _judge_storage_plan
from synnodb.conversations.filenames import Filenames
from synnodb.utils.utils import DBStorage

SCHEMA = "CREATE TABLE lineitem (l_orderkey BIGINT);"
PLAN_TEXT = "l_orderkey stored as int64, sorted ascending, zone-mapped per 64k block."


def _make_ctx(agent_sdk_wrapper=None) -> ConvContext:
    return ConvContext(
        query_ids=["1"],
        filenames=Filenames.for_usecase(),
        workspace_path=MagicMock(name="workspace_path"),
        db_storage=DBStorage.IN_MEMORY,
        threads=1,
        model="openai/unsloth/MiniMax-M3",
        run_tool=MagicMock(name="run_tool"),
        workload_provider=MagicMock(name="workload_provider"),
        sql_dict={},
        workload=None,
        agent_sdk_wrapper=agent_sdk_wrapper,
    )


@pytest.mark.asyncio
async def test_routes_through_agent_sdk_wrapper_not_litellm_directly():
    wrapper = MagicMock(name="agent_sdk_wrapper")
    wrapper.run_one_off_completion = AsyncMock(return_value="VALID")
    ctx = _make_ctx(agent_sdk_wrapper=wrapper)

    result = await _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    assert result is None  # VALID
    wrapper.run_one_off_completion.assert_awaited_once()
    call = wrapper.run_one_off_completion.await_args
    assert SCHEMA in call.args[0]
    assert PLAN_TEXT in call.args[0]
    assert call.kwargs["max_tokens"] == 200


@pytest.mark.asyncio
async def test_returns_the_invalid_reason():
    wrapper = MagicMock(name="agent_sdk_wrapper")
    wrapper.run_one_off_completion = AsyncMock(return_value="INVALID: just says 'TODO'")
    ctx = _make_ctx(agent_sdk_wrapper=wrapper)

    result = await _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    assert result == "INVALID: just says 'TODO'"


@pytest.mark.asyncio
async def test_call_failure_skips_the_check_without_crashing():
    wrapper = MagicMock(name="agent_sdk_wrapper")
    wrapper.run_one_off_completion = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )
    ctx = _make_ctx(agent_sdk_wrapper=wrapper)

    result = await _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    assert result is None  # treated as "skip the check", not a crash


@pytest.mark.asyncio
async def test_missing_agent_sdk_wrapper_asserts_instead_of_skipping():
    ctx = _make_ctx(agent_sdk_wrapper=None)

    with pytest.raises(AssertionError):
        await _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)
