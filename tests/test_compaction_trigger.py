"""Tests for the proactive compaction trigger and the persistent stage prompt.

Two pieces work together to compact BEFORE a hard overflow and keep the agent on
task afterward:
  - _context_usage_at_or_above: the predicate behind should_trigger_compaction,
    reading the context-window fraction stashed on RunStatsCollector each turn.
  - RunStatsCollector._reset_per_turn_prompt_state: clears per-turn prompt state
    but MUST preserve current_stage_prompt, so a compaction firing mid-loop still
    has the active task to re-anchor on.
"""

from types import SimpleNamespace

from llm.sdk.agents_sdk.compaction_trigger import (
    COMPACTION_TRIGGER_FRACTION,
    context_usage_at_or_above,
)
from observability.logging.run_stats_collector import RunStatsCollector


# ---------- the proactive trigger predicate ----------


def test_trigger_fires_at_or_above_threshold():
    assert context_usage_at_or_above(
        SimpleNamespace(last_context_window_usage=0.90), 0.90
    )
    assert context_usage_at_or_above(
        SimpleNamespace(last_context_window_usage=0.97), 0.90
    )


def test_trigger_does_not_fire_below_threshold():
    assert not context_usage_at_or_above(
        SimpleNamespace(last_context_window_usage=0.89), 0.90
    )


def test_trigger_handles_missing_collector_and_attr():
    assert not context_usage_at_or_above(None, 0.90)
    # no turn completed yet -> attribute absent -> treated as 0.0
    assert not context_usage_at_or_above(SimpleNamespace(), 0.90)


def test_default_threshold_is_below_one():
    # we must compact BEFORE a hard overflow, not at 100%
    assert 0.0 < COMPACTION_TRIGGER_FRACTION < 1.0


# ---------- the persistent stage prompt (the proactive-reinsert enabler) ----------


def _collector():
    return RunStatsCollector.__new__(RunStatsCollector)


def test_per_turn_reset_preserves_stage_prompt():
    c = _collector()
    c.current_prompt = "the full stage prompt text"
    c.current_prompt_descriptor = "implement queries"
    c.current_agent_config = {"model": "x"}
    c.current_stage_prompt = "the full stage prompt text"

    c._reset_per_turn_prompt_state()

    # per-turn fields are cleared ...
    assert c.current_prompt is None
    assert c.current_prompt_descriptor is None
    assert c.current_agent_config is None
    # ... but the stage prompt survives so a mid-loop compaction can re-anchor on it
    assert c.current_stage_prompt == "the full stage prompt text"
