"""Tests for the V4A update_file diff-context repair.

apply_diff's context matcher only does exact / rstrip / strip matching, so a
context or deletion line the (weak) model retyped with a unicode lookalike — em
dash for '-', smart quotes, NBSP, '…', '→', digit-group separators — fails to
match and the whole edit is lost. repair_diff_context rewrites such ' '/'-' lines
back to the file's true bytes, but ONLY when the canonical form maps to exactly
one file line (never an ambiguous guess); '+' lines (the model's new content) are
left untouched.

Layers under test:
  - _canonicalize_line / repair_diff_context (pure)
  - the real agents.apply_diff (a diff that raises without repair, applies with it)
  - WorkspaceEditor._update_file_impl wiring (repair + failure recovery block)
"""

from pathlib import Path

import pytest
from agents import apply_diff
from agents.editor import ApplyPatchOperation

from synnodb.tools import workspace_editor as wse
from synnodb.tools.workspace_editor import (
    _canonicalize_line,
    repair_diff_context,
)
from synnodb.tools.workspace_editor import WorkspaceEditor


# ───────────────────────── editor harness (file-local) ─────────────────────────


class _FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []
        self.stats: list[dict] = []
        self.model = "test-model"
        self.last_turn = 0

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, op_type, **kwargs) -> None:
        self.stats.append({"op_type": op_type, **kwargs})


class _FakeSnapshotter:
    def __init__(self, current_hash: str = "start") -> None:
        self.current_hash = current_hash

    def restore(self, snapshot_hash: str) -> None:
        self.current_hash = snapshot_hash

    def snapshot(self, name: str):
        self.current_hash = f"snapshot-{name}"
        return None, self.current_hash


def _make_editor(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=_FakeRunStatsCollector(),  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=_FakeSnapshotter(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    return editor, workspace


# ───────────────────────── _canonicalize_line (pure) ──────────────────────────


def test_canonicalize_folds_unicode_lookalikes():
    assert _canonicalize_line("a — b") == "a - b"  # em dash
    assert _canonicalize_line("“quoted”") == '"quoted"'
    assert _canonicalize_line("a → b") == "a -> b"
    assert _canonicalize_line("x ≥ y") == "x >= y"
    assert _canonicalize_line("done…") == "done..."


def test_canonicalize_normalizes_whitespace_and_digit_separators():
    assert _canonicalize_line("a b") == "a b"  # NBSP -> space
    assert _canonicalize_line("1,000") == "1000"  # digit-group separator dropped
    assert _canonicalize_line("  lead   trail  ") == "lead trail"  # collapse + strip


# ───────────────────────── repair_diff_context (pure) ─────────────────────────


def test_context_line_emdash_repaired_to_file_bytes():
    original = "alpha\nbeta — gamma\ndelta\n"
    # model retyped the context line with an ASCII hyphen instead of the em dash
    bad = " beta - gamma\n-delta\n+epsilon"
    fixed = repair_diff_context(original, bad)
    assert " beta — gamma" in fixed  # context restored to the file's true bytes
    assert "-delta" in fixed  # already-exact deletion untouched
    assert "+epsilon" in fixed  # addition untouched


def test_deletion_line_lookalike_repaired():
    original = "x ≤ y\nkeep\n"
    bad = "-x <= y\n keep"  # deletion line must match the file too
    fixed = repair_diff_context(original, bad)
    assert "-x ≤ y" in fixed


def test_addition_lines_are_never_repaired():
    # '+' is the model's NEW content; its lookalikes must be preserved verbatim.
    original = "a - b\n"
    bad = " a - b\n+new — line"
    fixed = repair_diff_context(original, bad)
    assert "+new — line" in fixed


def test_ambiguous_canonical_match_is_left_alone():
    # em dash and en dash file lines share a canonical form; an ASCII-hyphen body
    # matches BOTH, so the repair must not guess.
    original = "foo — bar\nfoo – bar\n"
    bad = " foo - bar\n+x"
    fixed = repair_diff_context(original, bad)
    assert " foo - bar" in fixed  # left exactly as the model wrote it


def test_no_repair_returns_the_same_object():
    # identity fast-path: when nothing is mistyped we must not reflow the diff.
    original = "a\nb\nc\n"
    diff = " a\n-b\n+B"
    assert repair_diff_context(original, diff) is diff


def test_diff_header_lines_untouched_but_context_repaired():
    original = "x — y\n"
    diff = "--- a/f\n+++ b/f\n x - y"
    fixed = repair_diff_context(original, diff)
    assert "--- a/f" in fixed and "+++ b/f" in fixed  # headers preserved
    assert " x — y" in fixed  # the real context line still gets repaired


def test_blank_context_line_is_not_repaired():
    # an empty context body must never trigger a substitution
    original = "a\n\nb\n"
    diff = " a\n \n-b\n+B"  # second line is a blank context line
    fixed = repair_diff_context(original, diff)
    assert fixed is diff  # nothing to repair


# ───────────────── integration with the real apply_diff matcher ────────────────


def test_apply_diff_fails_without_repair_but_succeeds_with_it():
    original = "alpha\nbeta — gamma\ndelta\n"
    bad = " beta - gamma\n-delta\n+epsilon"  # ASCII hyphen vs the file's em dash

    # apply_diff's matcher (exact/rstrip/strip) cannot bridge the lookalike
    with pytest.raises(ValueError):
        apply_diff(original, bad)

    # ...but repairing the context first makes it apply cleanly
    patched = apply_diff(original, repair_diff_context(original, bad))
    assert "epsilon" in patched
    assert "delta" not in patched
    assert "beta — gamma" in patched  # the file's real bytes survive


# ─────────────────── WorkspaceEditor._update_file_impl wiring ──────────────────


def test_update_file_impl_repairs_and_completes(tmp_path):
    editor, workspace = _make_editor(tmp_path)
    (workspace / "f.txt").write_text("alpha\nbeta — gamma\ndelta\n", encoding="utf-8")

    op = ApplyPatchOperation(
        type="update_file", path="f.txt", diff=" beta - gamma\n-delta\n+epsilon"
    )
    result, _activity = editor._update_file_impl(op)

    assert result.status == "completed"
    content = (workspace / "f.txt").read_text(encoding="utf-8")
    assert "epsilon" in content
    assert "delta" not in content
    assert "beta — gamma" in content  # the em dash was never clobbered


def test_update_file_impl_unmatchable_diff_echoes_current_content(
    tmp_path, monkeypatch
):
    # silence the structured error sink; we only care about the recovery output
    monkeypatch.setattr(wse, "log_tool_call_error", lambda **kw: None)
    editor, workspace = _make_editor(tmp_path)
    (workspace / "f.txt").write_text("real line A\nreal line B\n", encoding="utf-8")

    op = ApplyPatchOperation(
        type="update_file",
        path="f.txt",
        diff=" totally different context\n-real line B\n+X",
    )
    result, _activity = editor._update_file_impl(op)

    assert result.status == "failed"
    # the failure response carries the CURRENT file content so the model can
    # rebuild the diff without an extra read round-trip
    assert "CURRENT CONTENT OF f.txt" in result.output
    assert "real line A" in result.output
