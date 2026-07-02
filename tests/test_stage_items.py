"""Typed stage items: marker lowering round-trip.

Typed marker items are the authoring format; the legacy marker strings remain
the wire/persistence format. These tests pin that lowering: each item lowers to
its exact legacy string, the engine hands the lowered string to ``_exec`` (so
``handle_prompt`` and the conversation JSON see the same bytes as before), and
``SupervisionHorizon`` is skipped by the runner.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.conversation_engine import Conversation
from synnodb.conversations.filenames import Filenames
from synnodb.conversations.stage_items import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
    VALIDATE_OUTPUT_STDOUT_ON,
    Benchmark,
    Compact,
    PromptStage,
    SupervisionHorizon,
    ValidateOff,
    ValidateOn,
    ValidateStdoutOff,
    ValidateStdoutOn,
)
from synnodb.conversations.supervision_agent import (
    SUPERVISION_STAGE_VISIBILITY_MARKER,
    SupervisionAgent,
)
from synnodb.utils.utils import DBStorage


def test_marker_items_lower_to_legacy_strings():
    assert Compact().marker == COMPACTION_MARKER
    assert Benchmark().marker == BENCHMARK_MARKER
    assert ValidateOn().marker == VALIDATE_ON
    assert ValidateOff().marker == VALIDATE_OFF
    assert ValidateStdoutOn().marker == VALIDATE_OUTPUT_STDOUT_ON
    assert ValidateStdoutOff().marker == VALIDATE_OUTPUT_STDOUT_OFF
    assert SupervisionHorizon().marker == SUPERVISION_STAGE_VISIBILITY_MARKER


class _RecordingConversation(Conversation):
    """Records what _exec receives instead of talking to an LLM."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.exec_calls: list[tuple[str, str | None, int]] = []

    async def _exec(
        self, task_prompt, prompt_descriptor, current_stage_nr, max_turns=None
    ):
        self.exec_calls.append((task_prompt, prompt_descriptor, current_stage_nr))
        # mirror the persistence path: accepted prompts land in `used`
        self.used.append(task_prompt)
        return "ok"


def _make_conv(tmp_path: Path) -> _RecordingConversation:
    ctx = ConvContext(
        query_ids=["1"],
        filenames=Filenames.for_usecase(),
        workspace_path=tmp_path,
        db_storage=DBStorage.IN_MEMORY,
        threads=1,
        model="test-model",
        run_tool=MagicMock(),
        workload_provider=MagicMock(),
        sql_dict={},
        workload=None,
        conversation_json_path=tmp_path / "conv.json",
    )
    return _RecordingConversation(
        plan_stages=lambda _ctx: [],
        conv_context=ctx,
        run_tool=MagicMock(),
        git_snapshotter=MagicMock(),
        run_stats_collector=MagicMock(debug_logger=None),
        gen_incorrect_output_prompt_fn=lambda *a, **k: "x",
        supervision_agent=None,
        agent_sdk_wrapper=MagicMock(),
        callback=lambda **kwargs: None,
    )


def test_run_stages_lowers_markers_and_skips_supervision_horizon(tmp_path):
    conv = _make_conv(tmp_path)
    items = [
        ValidateOn(),
        Benchmark(),
        PromptStage(
            descriptor="stage A",
            get_prompt=lambda _s, _rt: "PROMPT A",
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
        ),
        SupervisionHorizon(),
        Compact(),
    ]
    asyncio.run(conv._run_stages(items))

    prompts = [c[0] for c in conv.exec_calls]
    # SupervisionHorizon is skipped; every marker arrives as its legacy string.
    assert prompts == [VALIDATE_ON, BENCHMARK_MARKER, "PROMPT A", COMPACTION_MARKER]
    # Stage numbers still follow list positions (the horizon occupies an index).
    assert [c[2] for c in conv.exec_calls] == [0, 1, 2, 4]

    # The conversation JSON (persistence format) is byte-identical to what the
    # legacy string-authored lists produced.
    expected_json = json.dumps(
        [VALIDATE_ON, BENCHMARK_MARKER, "PROMPT A", COMPACTION_MARKER],
        ensure_ascii=False,
        indent=2,
    )
    assert json.dumps(conv.used, ensure_ascii=False, indent=2) == expected_json


def test_supervision_agent_registers_typed_items():
    agent = SupervisionAgent(
        run_stats_collector=MagicMock(),
        agent_sdk_wrapper=MagicMock(),
    )
    stage = PromptStage(
        descriptor="stage A",
        get_prompt=lambda _s, _rt: "PROMPT A",
        measure_performance_after_stage=False,
        auto_revert_on_regression=False,
    )
    agent.register_workload_info([ValidateOn(), stage, SupervisionHorizon()])
    assert agent.stage_descriptions == [
        VALIDATE_ON,
        "stage A",
        SUPERVISION_STAGE_VISIBILITY_MARKER,
    ]
    # The lowered strings keep the scoping comparisons working.
    assert agent.stages[0] == VALIDATE_ON
    assert agent.stages[2] == SUPERVISION_STAGE_VISIBILITY_MARKER
