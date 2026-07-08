"""Regression tests for parse_supervision_output() and the dev-hints toggle in
supervision_agent_prompt().

Guards:
- <run_summary>/<dev_hints> blocks are extracted and stripped out of feedback_text.
- Approval is still decided by the last non-empty line of feedback_text (unaffected
  by the new meta blocks).
- dev_hints is None when absent or when the model wrote "None".
- supervision_agent_prompt() only mentions <dev_hints> when generate_dev_hints=True.
"""

from synnodb.conversations.prompts_gen import (
    SUPERVISION_SUCCESS_KW,
    parse_supervision_output,
    supervision_agent_prompt,
)


def test_approved_with_run_summary_only():
    output = (
        "The agent applied the patch and ran the query successfully.\n"
        "<run_summary>Optimized the join order, cutting runtime by 40%.</run_summary>\n"
        f"{SUPERVISION_SUCCESS_KW}"
    )
    result = parse_supervision_output(output)
    assert result.approved is True
    assert result.run_summary == "Optimized the join order, cutting runtime by 40%."
    assert result.dev_hints is None
    assert "<run_summary>" not in result.feedback_text
    assert "</run_summary>" not in result.feedback_text


def test_rejected_with_gap_summary_verdict():
    output = (
        "The agent claimed to update the file but no apply_patch call is listed.\n"
        "<run_summary>Attempted a fix, but no file changes were actually made.</run_summary>\n"
        "The apply_patch tool was never invoked despite the agent claiming so."
    )
    result = parse_supervision_output(output)
    assert result.approved is False
    assert result.feedback_text.endswith(
        "The apply_patch tool was never invoked despite the agent claiming so."
    )
    assert "<run_summary>" not in result.feedback_text


def test_dev_hints_present():
    output = (
        "Analysis here.\n"
        "<run_summary>Ran the benchmark suite.</run_summary>\n"
        "<dev_hints>The agent repeatedly misread the tool schema — possible prompt ambiguity.</dev_hints>\n"
        f"{SUPERVISION_SUCCESS_KW}"
    )
    result = parse_supervision_output(output)
    assert result.approved is True
    assert result.dev_hints == (
        "The agent repeatedly misread the tool schema — possible prompt ambiguity."
    )
    assert "<dev_hints>" not in result.feedback_text


def test_dev_hints_literal_none_is_treated_as_absent():
    output = (
        "Analysis here.\n"
        "<run_summary>Nothing unusual.</run_summary>\n"
        "<dev_hints>None</dev_hints>\n"
        f"{SUPERVISION_SUCCESS_KW}"
    )
    result = parse_supervision_output(output)
    assert result.dev_hints is None


def test_dev_hints_absent_when_not_requested():
    output = f"Analysis here.\n<run_summary>All good.</run_summary>\n{SUPERVISION_SUCCESS_KW}"
    result = parse_supervision_output(output)
    assert result.dev_hints is None
    assert result.run_summary == "All good."


def test_prompt_mentions_dev_hints_only_when_enabled():
    kwargs = dict(
        user_prompt="do the thing",
        activity_summary=["invoked tool X"],
        llm_output="done",
        stage_overview="1: stage A <-- current stage",
    )
    enabled_prompt = supervision_agent_prompt(**kwargs, generate_dev_hints=True)
    disabled_prompt = supervision_agent_prompt(**kwargs, generate_dev_hints=False)

    assert "<dev_hints>" in enabled_prompt
    assert "<dev_hints>" not in disabled_prompt
    assert "<run_summary>" in enabled_prompt
    assert "<run_summary>" in disabled_prompt
    assert "${" not in enabled_prompt
    assert "${" not in disabled_prompt
