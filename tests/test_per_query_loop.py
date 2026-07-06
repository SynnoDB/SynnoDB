"""Unit tests for the engine's composite stage items.

Covers the PerQueryLoop executor (branch-switch call sequence, branch anchor,
pre-stage measurement order, revert path) and the per-item benchmark_sf
override (applied before execution, restored afterwards - including on
exception). The SDK wrapper and run tool are mocked; the branch-turn semantics
the loop relies on are pinned separately in tests/test_sdk_branch_semantics.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.conversation_engine import (
    BRANCH_ANCHOR_PROMPT,
    Conversation,
)
from synnodb.conversations.filenames import Filenames
from synnodb.conversations.stage_items import (
    COMPACTION_MARKER,
    AssertCorrect,
    Benchmark,
    MeasureBaselines,
    PerQueryLoop,
    PromptStage,
)
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

QUERY_IDS = ["1", "6"]


# ------------------------------ fakes / harness -------------------------------
def _metrics(qid: str, impl_rt_ms: float, correct: bool = True) -> dict:
    q = qid.zfill(3)
    return {
        "validation/correct": correct,
        f"validation/query_{q}/bespoke_runtime_ms": impl_rt_ms,
        f"validation/query_{q}/duckdb_runtime_ms": 1000.0,
    }


def _fake_provider(benchmark_sf: float = 20.0, large_check_sf: float | None = 100.0):
    """A real-typed OLAPWorkloadProvider (bypassing __init__) so the engine's
    isinstance check and the real set_benchmark_sf field assignment apply."""
    provider = object.__new__(OLAPWorkloadProvider)
    provider.benchmark_sf = benchmark_sf
    provider.spec = SimpleNamespace(large_check_sf=large_check_sf)
    provider.sf_log = []
    original = OLAPWorkloadProvider.set_benchmark_sf

    def _logged_set(sf: float) -> None:
        provider.sf_log.append(sf)
        original(provider, sf)

    provider.set_benchmark_sf = _logged_set
    return provider


class FakeRunTool:
    """Returns scripted per-query runtimes; records every run() call."""

    def __init__(self, schedules: dict[str, list[float]], provider=None):
        self.schedules = {q: list(rts) for q, rts in schedules.items()}
        self.calls: list[tuple] = []
        self.cwd = Path("/nonexistent-fake-workspace")
        self.workload_provider = provider if provider is not None else _fake_provider()

    def run(
        self,
        mode=None,
        optimize=None,
        query_ids=None,
        trace_mode=False,
        external_call=False,
    ):
        self.calls.append(
            ("run", tuple(query_ids) if query_ids else None, bool(trace_mode))
        )
        if trace_mode:
            qid = query_ids[0] if query_ids else "all"
            return ("msg", _metrics(qid, 1.0), f"TRACE({qid})")
        if query_ids is not None and len(query_ids) == 1:
            qid = query_ids[0]
            rt_ms = self.schedules[qid].pop(0)
            return ("msg", _metrics(qid, rt_ms), None)
        # full benchmark across all queries
        metrics = {"validation/correct": True}
        for qid in QUERY_IDS:
            metrics.update(_metrics(qid, 1.0))
        return ("full-benchmark", metrics, None)


class _LoopConversation(Conversation):
    """Executes real engine logic; only _exec (the LLM boundary) is recorded."""

    def __init__(self, items, **kwargs):
        super().__init__(plan_stages=lambda _ctx: items, **kwargs)
        self.exec_calls: list[dict] = []

    async def _exec(
        self, task_prompt, prompt_descriptor, current_stage_nr, max_turns=None
    ):
        self.exec_calls.append(
            dict(
                prompt=task_prompt,
                descriptor=prompt_descriptor,
                stage_nr=current_stage_nr,
                max_turns=max_turns,
            )
        )
        return "llm output"


def _make_conv(tmp_path, items, run_tool, query_ids=QUERY_IDS):
    sdk = MagicMock(name="agent_sdk_wrapper")
    sdk.get_conversation_turns = AsyncMock(return_value=3)
    sdk.switch_to_conversation_branch = AsyncMock()
    sdk.create_conversation_branch_from_turn = AsyncMock(
        side_effect=lambda turn_nr, branch_name: branch_name
    )
    ctx = ConvContext(
        query_ids=query_ids,
        filenames=Filenames.for_usecase(),
        workspace_path=tmp_path,
        db_storage=DBStorage.IN_MEMORY,
        threads=1,
        model="test-model",
        run_tool=run_tool,
        workload_provider=run_tool.workload_provider,
        sql_dict={},
        workload=None,
        conversation_json_path=tmp_path / "conv.json",
    )
    conv = _LoopConversation(
        items,
        conv_context=ctx,
        run_tool=run_tool,
        git_snapshotter=MagicMock(current_hash="snapshot-0"),
        run_stats_collector=MagicMock(debug_logger=None),
        gen_incorrect_output_prompt_fn=lambda *a, **k: "GEN_INCORRECT",
        supervision_agent=None,
        agent_sdk_wrapper=sdk,
        callback=lambda **kwargs: None,
    )
    return conv, sdk


def _loop_stage(qid: str, prompts: list, **overrides) -> PromptStage:
    defaults = dict(
        descriptor=f"tune {qid}",
        get_prompt_with_tracing=lambda _s, rt, trace, qid=qid: (
            prompts.append((qid, rt, trace)) or f"PROMPT({qid}, rt={rt}, {trace})"
        ),
    )
    defaults.update(overrides)
    return PromptStage(**defaults)


# ------------------------------------ tests ----------------------------------
def test_loop_branch_sequence_and_anchor(tmp_path):
    prompts: list = []
    loop = PerQueryLoop(
        build=lambda qid, _ctx: [_loop_stage(qid, prompts)],
        branch_anchor=True,
    )
    run_tool = FakeRunTool({"1": [2000.0, 1000.0], "6": [2000.0, 1000.0]})
    conv, sdk = _make_conv(tmp_path, [loop], run_tool)

    asyncio.run(conv.run())

    # anchor is the first _exec, byte-identical text, 5-turn budget
    anchor = conv.exec_calls[0]
    assert anchor["prompt"] == BRANCH_ANCHOR_PROMPT
    assert anchor["descriptor"] == "Branch Anchor"
    assert anchor["max_turns"] == 5

    # branches are created from the current last turn, one per query, always
    # branching off main
    assert sdk.create_conversation_branch_from_turn.call_args_list == [
        ((), {"turn_nr": 3, "branch_name": "query_1_3"}),
        ((), {"turn_nr": 3, "branch_name": "query_6_3"}),
    ]
    switch_calls = [c.args[0] for c in sdk.switch_to_conversation_branch.call_args_list]
    # setup: back to main before each branch creation; ring: one switch per stage
    assert switch_calls == ["main", "main", "query_1_3", "query_6_3"]

    # stage-major execution: prompt for q1 then q6, each followed by compaction
    stage_prompts = [c["prompt"] for c in conv.exec_calls[1:]]
    assert stage_prompts == [
        "PROMPT(1, rt=2000.0, TRACE(1))",
        COMPACTION_MARKER,
        "PROMPT(6, rt=2000.0, TRACE(6))",
        COMPACTION_MARKER,
    ]
    # compaction shares its stage number with the stage it follows
    assert conv.exec_calls[1]["stage_nr"] == conv.exec_calls[2]["stage_nr"]

    # measurement order per stage: benchmark (plain) then tracing run, then the
    # post-stage measurement; ring ends with a full benchmark (query_ids=None)
    q1_calls = [c for c in run_tool.calls if c[1] == ("1",)]
    assert q1_calls == [
        ("run", ("1",), False),
        ("run", ("1",), True),
        ("run", ("1",), False),
    ]
    assert run_tool.calls[-1] == ("run", None, False)


def test_loop_without_anchor_emits_no_anchor_turn(tmp_path):
    prompts: list = []
    loop = PerQueryLoop(build=lambda qid, _ctx: [_loop_stage(qid, prompts)])
    run_tool = FakeRunTool({"1": [2000.0, 1000.0], "6": [2000.0, 1000.0]})
    conv, sdk = _make_conv(tmp_path, [loop], run_tool)

    asyncio.run(conv.run())

    assert all(c["prompt"] != BRANCH_ANCHOR_PROMPT for c in conv.exec_calls)
    # branches are still created (branching on, anchor off = round-1 behavior)
    assert sdk.create_conversation_branch_from_turn.call_count == 2


def test_loop_reverts_stage_on_regression(tmp_path):
    prompts: list = []
    loop = PerQueryLoop(
        build=lambda qid, _ctx: [_loop_stage(qid, prompts)],
    )
    # q1 regresses (1000 -> 3000) and is re-measured after the revert; q6 improves
    run_tool = FakeRunTool({"1": [1000.0, 3000.0, 1000.0], "6": [2000.0, 1000.0]})
    conv, _ = _make_conv(tmp_path, [loop], run_tool)
    snapshotter = conv.git_snapshotter

    asyncio.run(conv.run())

    snapshotter.reset_changes.assert_called_once()
    snapshotter.clear_untracked.assert_called_once()
    snapshotter.restore.assert_called_once_with("snapshot-0")
    # the reverted runtime is what survives in the log
    assert conv.query_rt_log["1"] == pytest.approx(1.0)
    assert conv.query_rt_log["6"] == pytest.approx(1.0)


def test_benchmark_sf_override_applied_and_restored(tmp_path):
    provider = _fake_provider(benchmark_sf=20.0, large_check_sf=100.0)
    run_tool = FakeRunTool({}, provider=provider)
    seen_sf: list[float] = []

    stage = PromptStage(
        descriptor="check large sf",
        get_prompt=lambda _s, _rt: (
            seen_sf.append(provider.benchmark_sf) or "CHECK PROMPT"
        ),
        measure_performance_after_stage=False,
        auto_revert_on_regression=False,
        benchmark_sf="large_check",
    )
    conv, _ = _make_conv(tmp_path, [stage, Benchmark(benchmark_sf=42.0)], run_tool)
    asyncio.run(conv.run())

    # large_check resolved via the workload spec; restored after each item
    assert provider.sf_log == [100.0, 20.0, 42.0, 20.0]
    # The scheduled-stage preview renders the prompt once at plan registration
    # (before any per-stage benchmark_sf override applies, hence the default 20),
    # then execution renders it again under the large_check override (100).
    assert seen_sf == [20.0, 100.0]
    assert provider.benchmark_sf == 20.0


def test_benchmark_sf_restored_on_exception(tmp_path):
    provider = _fake_provider(benchmark_sf=20.0)
    run_tool = FakeRunTool({}, provider=provider)

    def _boom(_s, _rt):
        raise RuntimeError("stage exploded")

    stage = PromptStage(
        descriptor="fails",
        get_prompt=_boom,
        measure_performance_after_stage=False,
        auto_revert_on_regression=False,
        benchmark_sf=77.0,
    )
    conv, _ = _make_conv(tmp_path, [stage], run_tool)
    with pytest.raises(RuntimeError, match="stage exploded"):
        asyncio.run(conv.run())

    assert provider.sf_log == [77.0, 20.0]
    assert provider.benchmark_sf == 20.0


def test_assert_correct_and_measure_baselines_items(tmp_path):
    run_tool = FakeRunTool({})
    items = [AssertCorrect(), MeasureBaselines(into="single_threaded_rt_ms")]
    conv, _ = _make_conv(tmp_path, items, run_tool)
    asyncio.run(conv.run())

    # AssertCorrect ran a validation benchmark; MeasureBaselines stored the
    # runtimes on the ConvContext (where stage prompts close over them)
    assert conv.conv_context.single_threaded_rt_ms == {"1": 1.0, "6": 1.0}
    assert conv.exec_calls == []  # neither item talks to the LLM
