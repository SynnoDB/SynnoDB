"""Scheduled-stage previews surfaced to the live dashboard's prompts pane.

The conversation knows its whole stage list the moment ``build_items`` returns,
so it publishes a preview of every not-yet-executed prompt-bearing stage. These
tests pin what gets previewed (only stages that surface as pane sections),
how runtime-only values are rendered (as explicit placeholders), and that the
live drain stores the previews on its snapshot meta keyed to the stage base step.
"""

from __future__ import annotations

import json
import threading

from synnodb.conversations.stage_items import (
    AssertCorrect,
    Benchmark,
    Compact,
    DynamicStageConfig,
    MeasureBaselines,
    PerQueryLoop,
    PromptStage,
    SupervisionHorizon,
)
from synnodb.conversations.stage_preview import (
    TRACING_PLACEHOLDER,
    build_stage_previews,
)
from synnodb.observability.live_ui.live_dashboard import LiveDashboardDrain


class _Iterative(DynamicStageConfig):
    def __init__(self, descriptor: str):
        self.descriptor = descriptor
        self.max_turns = None
        self.benchmark_sf = None

    def next_prompt(self) -> str:  # pragma: no cover - not exercised here
        return None


def test_only_prompt_bearing_stages_are_previewed():
    items = [
        Benchmark(),
        PromptStage(descriptor="static stage", get_prompt=lambda es, rt: "hello"),
        Compact(),
        AssertCorrect(),
        MeasureBaselines(),
        SupervisionHorizon(),
        _Iterative("iterative stage"),
        PerQueryLoop(build=lambda q, ctx: []),
    ]
    previews = build_stage_previews(items)
    descriptors = [p["descriptor"] for p in previews]
    # Markers and the silent composites (AssertCorrect/MeasureBaselines) never
    # run an LLM turn, so they are not part of the pane's section stream.
    assert descriptors == [
        "static stage",
        "iterative stage",
        "Per-query optimization loop",
    ]


def test_static_prompt_is_rendered_without_placeholder_flag():
    items = [
        PromptStage(descriptor="s", get_prompt=lambda es, rt: f"do it ({int(rt)})")
    ]
    (preview,) = build_stage_previews(items)
    assert preview["prompt_preview"] == "do it (0)"
    assert preview["has_runtime_placeholder"] is False
    assert preview["dynamic"] is False


def test_tracing_prompt_leaves_a_runtime_placeholder():
    items = [
        PromptStage(
            descriptor="s",
            get_prompt_with_tracing=lambda es, rt, td: f"optimize using:\n{td}",
        )
    ]
    (preview,) = build_stage_previews(items)
    assert TRACING_PLACEHOLDER in preview["prompt_preview"]
    assert preview["has_runtime_placeholder"] is True


def test_dynamic_and_loop_stages_have_no_preview():
    items = [_Iterative("iter"), PerQueryLoop(build=lambda q, ctx: [])]
    iter_preview, loop_preview = build_stage_previews(items)
    for p in (iter_preview, loop_preview):
        assert p["prompt_preview"] is None
        assert p["dynamic"] is True
        assert p["has_runtime_placeholder"] is True


def test_failing_prompt_builder_degrades_to_no_preview():
    def _boom(_es, _rt):
        raise RuntimeError("boom")

    items = [PromptStage(descriptor="broken", get_prompt=_boom)]
    (preview,) = build_stage_previews(items)
    assert preview["prompt_preview"] is None
    assert preview["dynamic"] is False


def _bare_drain() -> LiveDashboardDrain:
    """A LiveDashboardDrain with its data structures set up but no HTTP server."""
    d = LiveDashboardDrain.__new__(LiveDashboardDrain)
    d._data = {}
    d._lock = threading.Lock()
    d._stage_base = 0
    d._carry = {}
    d._last_global = {}
    d._stages = []
    d._meta = {"run_name": None, "stages": d._stages, "planned_stages": None}
    d._workspace_dir = None
    return d


def test_live_drain_publishes_previews_on_snapshot_meta():
    d = _bare_drain()
    d.begin_stage(run_name="createBaseImpl")
    d.emit({"current_prompt_descriptor": "Q1"}, step=0)
    d.begin_stage(run_name="runOptimLoop")  # second stage offsets past the first
    previews = [{"descriptor": "Optim Q1", "prompt_preview": "go", "dynamic": False}]
    d.register_planned_stages(previews, stage_name="runOptimLoop")

    meta = json.loads(d._snapshot())["meta"]
    planned = meta["planned_stages"]
    assert planned["stage_name"] == "runOptimLoop"
    assert planned["base_step"] == 1  # sits after the first stage's single step
    assert planned["stages"] == previews


def test_begin_stage_clears_previous_stage_previews():
    d = _bare_drain()
    d.begin_stage(run_name="a")
    d.register_planned_stages([{"descriptor": "x"}], stage_name="a")
    assert d._meta["planned_stages"] is not None
    d.begin_stage(run_name="b")
    # The next stage starts with no previews until it registers its own.
    assert d._meta["planned_stages"] is None
