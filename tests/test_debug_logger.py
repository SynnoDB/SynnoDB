"""Tests for DebugLogger: the human-readable per-run debug log writer.

The logger is best-effort, append-only I/O. These tests pin the (run-scoped) file
layout, the query attribution on stage headers, the per-entry formatting, the
truncation caps (including events), the self-timed stage duration, and the
skip-empty-LLM-turn behavior.
"""

from synnodb.observability.logging.debug_logger import (
    _EVENT_MAX_CHARS,
    _LLM_MAX_CHARS,
    _TOOL_MAX_CHARS,
    DebugLogger,
)


def _read(logger: DebugLogger) -> str:
    return logger._path.read_text()


# ---------- path layout & header (one file per category/storage, run-scoped) ----------


def test_path_is_run_scoped_no_query_dir(tmp_path):
    dl = DebugLogger(
        category="base_impl", storage="ssd", model="some/model", base_dir=tmp_path
    )
    # one chronological file per (category, storage); query is NOT a path segment
    assert dl._path == tmp_path / "base_impl" / "ssd" / "debug.log"
    header = _read(dl)
    assert header.startswith("# debug log — base_impl")
    assert "storage: ssd" in header
    assert "model: some/model" in header


def test_init_overwrites_existing_log(tmp_path):
    DebugLogger("c", "ssd", "m1", base_dir=tmp_path).log_event("first run")
    # A second logger for the same path starts a fresh file (write_text, not append).
    # In production base_dir is run-scoped, so this collision cannot happen.
    dl2 = DebugLogger("c", "ssd", "m2", base_dir=tmp_path)
    content = _read(dl2)
    assert "first run" not in content
    assert "model: m2" in content


# ---------- per-entry formatting ----------


def test_log_prompt_with_descriptor(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_prompt(3, "implement queries", "DO THE THING")
    out = _read(dl)
    assert "[Prompt 3 - implement queries]" in out
    assert "DO THE THING" in out


def test_log_prompt_without_descriptor(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_prompt(0, None, "body")
    assert "[Prompt 0]" in _read(dl)
    assert "[Prompt 0 - " not in _read(dl)


def test_log_event_with_and_without_body(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_event("PROACTIVE COMPACTION", "re-anchored: stage X")
    dl.log_event("bare label")
    out = _read(dl)
    assert "PROACTIVE COMPACTION" in out
    assert "re-anchored: stage X" in out
    assert "bare label" in out


def test_log_llm_turn(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_llm_turn(5, "model said hi")
    out = _read(dl)
    assert "[Turn 5 - LLM]" in out
    assert "model said hi" in out


def test_log_llm_turn_skips_empty(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    before = _read(dl)
    dl.log_llm_turn(1, "")
    dl.log_llm_turn(2, "   \n\t ")
    assert _read(dl) == before  # nothing appended for blank turns


def test_log_tool_result(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_tool_result(7, "apply_patch", "Updated foo.cpp")
    out = _read(dl)
    assert "[Turn 7 - Tool: apply_patch]" in out
    assert "Updated foo.cpp" in out


# ---------- truncation caps (events capped like turns) ----------


def test_llm_turn_truncated_over_cap(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    n = _LLM_MAX_CHARS + 500
    dl.log_llm_turn(1, "x" * n)
    out = _read(dl)
    assert f"...(truncated, {n} chars total)" in out
    # body kept is exactly the cap (plus the truncation suffix)
    assert ("x" * _LLM_MAX_CHARS) in out
    assert ("x" * (_LLM_MAX_CHARS + 1)) not in out


def test_llm_turn_not_truncated_at_cap(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_llm_turn(1, "y" * _LLM_MAX_CHARS)
    assert "truncated" not in _read(dl)


def test_tool_result_truncated_over_cap(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    n = _TOOL_MAX_CHARS + 1
    dl.log_tool_result(1, "shell", "z" * n)
    assert f"...(truncated, {n} chars total)" in _read(dl)


def test_event_truncated_over_cap(tmp_path):
    # the compaction dump goes through log_event; it must be capped like turns,
    # not logged in full.
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    n = _EVENT_MAX_CHARS + 10
    dl.log_event("BIG COMPACTION", "z" * n)
    out = _read(dl)
    assert f"...(truncated, {n} chars total)" in out
    assert ("z" * (_EVENT_MAX_CHARS + 1)) not in out


# ---------- stage boundaries (query in header, logger owns the duration) ----------


def test_stage_header_carries_query_id(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_stage_start(2, "optimize build", rt_before_s=1.5, query_id="12")
    out = _read(dl)
    assert "## Stage 2: optimize build | query 12" in out
    assert "Runtime before: 1500ms" in out


def test_stage_header_without_query_id(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_stage_start(1, "storage plan", rt_before_s=None)
    out = _read(dl)
    assert "## Stage 1: storage plan" in out
    assert "query" not in out  # no trailing "| query ..." segment


def test_stage_end_self_times_and_formats(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_stage_start(2, "optimize build", rt_before_s=1.5)
    dl._stage_start_time -= 65  # simulate 65s elapsed, deterministically
    dl.log_stage_end(rt_after_s=0.5, speedup_after=3.0)
    out = _read(dl)
    assert "Stage END" in out
    assert "1m05s" in out  # 65s formatted
    assert "Speedup: 3.00x" in out


def test_stage_end_duration_only(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_stage_start(1, "s", rt_before_s=None)
    dl._stage_start_time -= 8
    dl.log_stage_end()
    out = _read(dl)
    assert "Duration: 8s" in out
    assert "Speedup" not in out


# ---------- append semantics ----------


def test_entries_accumulate_in_order(tmp_path):
    dl = DebugLogger("c", "ssd", "m", base_dir=tmp_path)
    dl.log_prompt(0, "stage", "P")
    dl.log_llm_turn(1, "L")
    dl.log_tool_result(1, "shell", "R")
    out = _read(dl)
    assert out.index("[Prompt 0") < out.index("[Turn 1 - LLM]") < out.index(
        "[Turn 1 - Tool: shell]"
    )
