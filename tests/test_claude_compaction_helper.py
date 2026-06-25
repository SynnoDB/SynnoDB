"""Tests for build_compacted_items: the post-compaction items (the summary, plus
an optional re-anchor that re-states the active stage task).

The summary item is always present; the re-anchor item is appended only when a
non-empty stage prompt is supplied. Callers only ever pass a real stage task
(never a control marker), so there is no marker filtering in the builder.
"""

from llm.anthropic.claude_compaction_helper import build_compacted_items

STAGE_PROMPT = "verify storage impl: build at SF=1 and audit the produced files."


def test_summary_always_present():
    items = build_compacted_items("SUMMARY-TEXT")
    assert len(items) == 1
    assert items[0]["role"] == "user"
    assert "SUMMARY-TEXT" in items[0]["content"]


def test_summary_item_wire_format_is_byte_stable():
    # The summary item is embedded in cached compaction output; changing it changes
    # cache behavior, so pin it byte-for-byte.
    assert build_compacted_items("S")[0]["content"] == (
        "Here is a summary of our prior conversation:\n\nS\n\nLet's continue."
    )


def test_reanchor_appended_with_stage_prompt():
    items = build_compacted_items("SUMMARY-TEXT", STAGE_PROMPT)
    assert len(items) == 2
    # summary first, then the re-anchor with the verbatim stage prompt
    assert "SUMMARY-TEXT" in items[0]["content"]
    assert STAGE_PROMPT in items[1]["content"]
    assert "UNCHANGED" in items[1]["content"]
    assert "Resume exactly the task below" in items[1]["content"]
    assert items[1]["role"] == "user"


def test_empty_or_blank_prompt_no_reanchor():
    for blank in (None, "", "   ", "\n\t"):
        assert len(build_compacted_items("S", blank)) == 1


def test_reanchor_prompt_is_stripped():
    items = build_compacted_items("S", "   do the task\n")
    assert len(items) == 2
    # leading/trailing whitespace is stripped before embedding
    assert items[1]["content"].endswith("do the task")
