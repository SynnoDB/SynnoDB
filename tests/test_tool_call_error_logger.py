"""Tests for log_tool_call_error.

Regression coverage for a bug where workspace_editor.py called this function
with an `extra=` kwarg that the signature didn't accept, so every ApplyPatch
failure crashed the error logger itself with a TypeError instead of recording
the failure (masking the real diff-mismatch error the model needed to see).
"""

from pathlib import Path

import pytest

from synnodb.tools import tool_call_error_logger as tcel
from synnodb.tools.tool_call_error_logger import log_tool_call_error


@pytest.fixture(autouse=True)
def _isolated_logger_state(tmp_path, monkeypatch):
    # log_tool_call_error writes to a cwd-relative dir and tracks state in
    # module globals; isolate both so tests don't leak into the repo or
    # into each other.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tcel, "_error_counter", 0)
    monkeypatch.setattr(tcel, "_initialized_models", set())


def test_logs_with_extra_dict(tmp_path):
    log_tool_call_error(
        error_type="ApplyPatchFailed",
        error=ValueError("context lines did not match"),
        model="test-model",
        turn=3,
        extra={
            "file": "/workspace/f.txt",
            "diff (first 60 lines)": "line1\nline2",
        },
    )

    content = Path("tool_call_errors/test-model.log").read_text()
    assert "Type: ApplyPatchFailed" in content
    assert "Turn: 3" in content
    assert "Message: context lines did not match" in content
    assert "--- Extra ---" in content
    assert "file:\n/workspace/f.txt" in content
    assert "diff (first 60 lines):\nline1\nline2" in content


def test_extra_defaults_to_no_section(tmp_path):
    log_tool_call_error(
        error_type="ReplaceInFileFailed",
        error=ValueError("no match"),
        model="test-model",
    )

    content = Path("tool_call_errors/test-model.log").read_text()
    assert "--- Extra ---" not in content


def test_raw_tool_calls_and_extra_can_combine(tmp_path):
    log_tool_call_error(
        error_type="ApplyPatchFailed",
        error=ValueError("boom"),
        model="test-model",
        raw_tool_calls=[{"name": "update_file", "arguments": '{"path": "f.txt"}'}],
        extra={"file": "f.txt"},
    )

    content = Path("tool_call_errors/test-model.log").read_text()
    assert "--- Raw Tool Calls ---" in content
    assert "Tool: update_file" in content
    assert "--- Extra ---" in content
    assert "file:\nf.txt" in content


def test_sanitizes_model_name_for_filename(tmp_path):
    log_tool_call_error(
        error_type="ApplyPatchFailed",
        error=ValueError("boom"),
        model="org/some model",
    )

    assert Path("tool_call_errors/org_some_model.log").exists()


def test_second_error_for_same_model_appends_not_overwrites(tmp_path):
    log_tool_call_error(error_type="First", error=ValueError("a"), model="m")
    log_tool_call_error(error_type="Second", error=ValueError("b"), model="m")

    content = Path("tool_call_errors/m.log").read_text()
    assert "Type: First" in content
    assert "Type: Second" in content
