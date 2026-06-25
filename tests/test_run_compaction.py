"""End-to-end tests for run_compaction's reinsertion + compaction logging.

These exercise the real run_compaction control flow with the LLM call simulated
(claude_compaction_helper.compact_with_claude is mocked, but the REAL
build_compacted_items runs, so reinsertion reflects the ambient stage prompt).

Reinsertion rule under test:
  - reanchor=True  (SDK proactive default) + local/claude + a stage prompt set
        -> the active stage prompt is reinserted into the compacted output
  - reanchor=False (caller: <<COMPACTION>> marker / reactive overflow retry)
        -> NO reinsertion (the caller re-issues the task itself)
  - OpenAI server-side path (use_claude=False) -> never reinserts
  - an empty compaction result must raise WITHOUT clearing the live session

The session is built via __new__ (no __init__) with only the attributes/methods
run_compaction touches; everything below the LLM boundary is mocked.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import llm.llm_caching.cached_compaction_session as ccs
from llm.anthropic.claude_compaction_helper import build_compacted_items
from llm.llm_caching.cached_compaction_session import (
    CachedOpenAIResponsesCompactionSession,
)
from observability.logging.debug_logger import DebugLogger


def _make_session(
    tmp_path,
    monkeypatch,
    *,
    use_claude=True,
    current_stage_prompt="implement queries: ...",
):
    # custom_span -> no-op so the test needs no SDK tracing setup.
    monkeypatch.setattr(ccs, "custom_span", lambda *a, **k: contextlib.nullcontext())

    s = CachedOpenAIResponsesCompactionSession.__new__(
        CachedOpenAIResponsesCompactionSession
    )
    s.model = "test-model"
    s.compaction_model_name = "openai/unsloth/MiniMax-M3"
    s._response_id = None
    s._last_unstored_response_id = None
    s.use_claude_compaction = use_claude
    s.do_not_cache = True  # skip cache file I/O; still computes the key
    s.cache_dir = tmp_path / "cache"
    s.cache_dir.mkdir()
    s.runtime_tracker = None
    s._resolve_compaction_mode_for_response = MagicMock(return_value="input")
    s._ensure_compaction_candidates = AsyncMock(
        return_value=([], [{"role": "user", "content": "prior history"}])
    )
    s.underlying_session = AsyncMock()

    debug_logger = DebugLogger(
        "compaction", "ssd", "test-model", base_dir=str(tmp_path / "debug_logs")
    )
    # current_stage_prompt is the ambient task the proactive path re-anchors on.
    s.run_stats_collector = SimpleNamespace(
        debug_logger=debug_logger,
        log_metrics_callback=MagicMock(),
        current_prompt_descriptor="implement queries",
        current_stage_prompt=current_stage_prompt,
    )
    return s, debug_logger


def _claude_helper_simulating_llm():
    # Simulate the compaction LLM: real build_compacted_items, fixed summary.
    def fake_compact(session_items, resume_prompt=None):
        return build_compacted_items("SIMULATED SUMMARY", resume_prompt)

    return SimpleNamespace(compact_with_claude=AsyncMock(side_effect=fake_compact))


@pytest.mark.asyncio
async def test_proactive_default_reanchors_from_stage_prompt(tmp_path, monkeypatch):
    # SDK proactive call shape: force=True, NO reanchor kwarg -> default reanchor=True.
    s, debug_logger = _make_session(tmp_path, monkeypatch, current_stage_prompt="TASK X")
    s.claude_compaction_helper = _claude_helper_simulating_llm()

    await s.run_compaction({"force": True, "compaction_mode": "input"})

    # the ambient stage prompt was threaded into the LLM call
    assert (
        s.claude_compaction_helper.compact_with_claude.call_args.kwargs["resume_prompt"]
        == "TASK X"
    )
    # the compacted session that REPLACES history carries the re-anchor
    added = s.underlying_session.add_items.call_args.args[0]
    assert len(added) == 2
    assert "SIMULATED SUMMARY" in added[0]["content"]
    assert "TASK X" in added[1]["content"]
    assert "UNCHANGED" in added[1]["content"]

    log = debug_logger._path.read_text()
    assert "PROACTIVE COMPACTION" in log
    assert "TASK X" in log


@pytest.mark.asyncio
async def test_caller_path_does_not_reanchor(tmp_path, monkeypatch):
    # caller (marker / reactive): reanchor=False, even though a stage prompt exists.
    s, debug_logger = _make_session(tmp_path, monkeypatch, current_stage_prompt="TASK X")
    s.claude_compaction_helper = _claude_helper_simulating_llm()

    await s.run_compaction(
        {"force": True, "compaction_mode": "input"}, reanchor=False
    )

    assert (
        s.claude_compaction_helper.compact_with_claude.call_args.kwargs["resume_prompt"]
        is None
    )
    added = s.underlying_session.add_items.call_args.args[0]
    assert len(added) == 1  # summary only, no re-anchor item
    assert "SIMULATED SUMMARY" in added[0]["content"]

    log = debug_logger._path.read_text()
    assert "CALLER COMPACTION" in log
    assert "reinsert only on proactive" in log


@pytest.mark.asyncio
async def test_proactive_without_stage_prompt_does_not_reanchor(tmp_path, monkeypatch):
    s, _ = _make_session(tmp_path, monkeypatch, current_stage_prompt=None)
    s.claude_compaction_helper = _claude_helper_simulating_llm()

    await s.run_compaction({"force": True, "compaction_mode": "input"})

    added = s.underlying_session.add_items.call_args.args[0]
    assert len(added) == 1  # nothing to reinsert -> summary only


@pytest.mark.asyncio
async def test_openai_path_never_reanchors(tmp_path, monkeypatch):
    s, debug_logger = _make_session(
        tmp_path, monkeypatch, use_claude=False, current_stage_prompt="TASK X"
    )
    s.claude_compaction_helper = None
    # simulate OpenAI server-side compaction output
    monkeypatch.setattr(
        ccs,
        "_normalize_compaction_output_items",
        lambda out: [{"role": "user", "content": "OAI SUMMARY"}],
    )
    # `client` is a lazy read-only property backed by `_client`
    s._client = MagicMock()
    s._client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=["raw"]))

    # even with reanchor defaulting True, the OpenAI path must ignore it
    await s.run_compaction({"force": True, "compaction_mode": "input"})

    s._client.responses.compact.assert_awaited_once()
    added = s.underlying_session.add_items.call_args.args[0]
    assert added == [{"role": "user", "content": "OAI SUMMARY"}]  # no re-anchor item
    assert "TASK X" not in debug_logger._path.read_text()


@pytest.mark.asyncio
async def test_empty_compaction_raises_without_wiping_session(tmp_path, monkeypatch):
    # defensive: an empty compaction result must not silently wipe the session
    s, _ = _make_session(tmp_path, monkeypatch)
    s.claude_compaction_helper = SimpleNamespace(
        compact_with_claude=AsyncMock(return_value=[])
    )
    with pytest.raises(Exception, match="refusing to clear"):
        await s.run_compaction({"force": True})
    s.underlying_session.clear_session.assert_not_called()  # session intact
