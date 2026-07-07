"""Resilience tests for apply_patch / update_file diff normalization.

Weak models routinely wrap a V4A hunk in a markdown code fence or an apply_patch
envelope (``*** Begin Patch`` / ``*** Update File:`` ...), which makes an otherwise
valid diff fail to apply. normalize_diff_payload strips those wrappers before the
hunk is applied. The stripping is conservative and fail-closed: it never touches a
real hunk line (those are ' '/'-'/'+' prefixed), so a diff that legitimately edits
backtick lines is left exactly as-is.

This complements test_update_file_diff_repair.py (unicode-lookalike context repair);
here we cover fence/envelope stripping and the full stack composed end-to-end.
"""

from pathlib import Path

from agents.editor import ApplyPatchOperation

from synnodb.tools.workspace_editor import (
    WorkspaceEditor,
    _strip_code_fences,
    _strip_patch_envelope,
    normalize_diff_payload,
)


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


# ───────────────────────────── _strip_code_fences ──────────────────────────────


def test_strip_fence_diff_flavor():
    assert _strip_code_fences("```diff\n a\n-b\n+c\n```") == " a\n-b\n+c"


def test_strip_fence_plain_and_language_tagged():
    assert _strip_code_fences("```\n a\n```") == " a"
    assert _strip_code_fences("```patch\n a\n+x\n```") == " a\n+x"


def test_strip_fence_tolerates_blank_lines_around_it():
    assert _strip_code_fences("\n```diff\n a\n-b\n+c\n```\n") == " a\n-b\n+c"


def test_no_fence_is_identity():
    d = " a\n-b\n+c"
    assert _strip_code_fences(d) is d  # untouched, same object (no reflow)


def test_unclosed_fence_left_untouched():
    d = "```diff\n a\n-b"  # opener but no closing fence
    assert _strip_code_fences(d) is d


def test_prefixed_backtick_content_not_stripped():
    # editing a markdown file: '+' lines ADD a ``` fence as real content
    d = " text\n+```\n+code\n+```"
    assert _strip_code_fences(d) is d  # first line " text" is not a fence opener


def test_space_prefixed_context_fence_is_not_a_wrapper():
    # a context line that happens to be a fence (space-prefixed) must NOT be
    # mistaken for a wrapping fence — this is the key safety case.
    d = " ```\n-old\n+new"
    assert _strip_code_fences(d) is d


# ──────────────────────────── _strip_patch_envelope ────────────────────────────


def test_strip_envelope_markers():
    d = "*** Begin Patch\n*** Update File: f.py\n a\n-b\n+c\n*** End Patch"
    assert _strip_patch_envelope(d) == " a\n-b\n+c"


def test_strip_envelope_handles_add_and_delete_file_headers():
    d = "*** Add File: new.py\n+content"
    assert _strip_patch_envelope(d) == "+content"


def test_no_envelope_is_identity():
    d = " a\n-b\n+c"
    assert _strip_patch_envelope(d) is d


def test_envelope_does_not_touch_content_lines_containing_stars():
    # only lines that START with the marker are dropped
    d = " has *** in the middle\n+added *** stars"
    assert _strip_patch_envelope(d) is d


# ──────────────────────────── normalize_diff_payload ───────────────────────────


def test_normalize_strips_fence_then_envelope():
    d = "```\n*** Begin Patch\n a\n-b\n+c\n*** End Patch\n```"
    assert normalize_diff_payload(d) == " a\n-b\n+c"


def test_normalize_is_identity_on_a_clean_hunk():
    d = " a\n-b\n+c"
    assert normalize_diff_payload(d) is d


# ───────────────────── end-to-end through _update_file_impl ────────────────────


def test_create_file_impl_overwrites_existing_non_readonly(tmp_path):
    # Agents reach for create_file to (re)write a scaffolded file; it must overwrite,
    # not fail with "refusing to overwrite" (which left weak models looping).
    editor, workspace = _make_editor(tmp_path)
    (workspace / "db_loader.cpp").write_text("// stub\nold content\n", encoding="utf-8")

    op = ApplyPatchOperation(
        type="create_file",
        path="db_loader.cpp",
        diff="+// real impl\n+int build(){return 0;}\n",
    )
    result, _ = editor._create_file_impl(op)

    assert result.status == "completed"
    assert "Overwrote existing" in result.output
    assert (workspace / "db_loader.cpp").read_text(
        encoding="utf-8"
    ) == "// real impl\nint build(){return 0;}"


def test_create_file_impl_creates_new_file(tmp_path):
    editor, workspace = _make_editor(tmp_path)
    op = ApplyPatchOperation(type="create_file", path="new.cpp", diff="+hello\n")
    result, _ = editor._create_file_impl(op)
    assert result.status == "completed" and "Created" in result.output
    assert "Overwrote" not in result.output
    assert (workspace / "new.cpp").read_text(encoding="utf-8") == "hello"


def test_create_file_impl_rejects_empty_diff_without_truncating_existing_file(tmp_path):
    # An empty diff parses to "" via apply_diff with no exception raised - nothing
    # else signals the problem, so writing it out would silently truncate an
    # existing file to 0 bytes while still reporting "completed". The model must
    # get an explicit failure instead, and the original content must survive.
    editor, workspace = _make_editor(tmp_path)
    (workspace / "query1.cpp").write_text("// real implementation\n", encoding="utf-8")

    op = ApplyPatchOperation(type="create_file", path="query1.cpp", diff="")
    result, _ = editor._create_file_impl(op)

    assert result.status == "failed"
    assert "empty" in result.output.lower()
    assert (workspace / "query1.cpp").read_text(
        encoding="utf-8"
    ) == "// real implementation\n"


def test_create_file_impl_rejects_missing_diff_without_truncating_existing_file(
    tmp_path,
):
    # operation.diff is str | None in the SDK's own ApplyPatchOperation - a model
    # can omit it entirely, not just send "". `diff = operation.diff or ""` must
    # route None through the exact same rejection as an explicit empty string.
    editor, workspace = _make_editor(tmp_path)
    (workspace / "query1.cpp").write_text("// real implementation\n", encoding="utf-8")

    op = ApplyPatchOperation(type="create_file", path="query1.cpp", diff=None)
    result, _ = editor._create_file_impl(op)

    assert result.status == "failed"
    assert (workspace / "query1.cpp").read_text(
        encoding="utf-8"
    ) == "// real implementation\n"


def test_create_file_impl_allows_empty_diff_for_a_brand_new_file(tmp_path):
    # Nothing existing is lost by creating an empty new file, unlike overwriting
    # one that already had content - only the overwrite case is destructive, so
    # only that case is rejected.
    editor, workspace = _make_editor(tmp_path)

    op = ApplyPatchOperation(type="create_file", path="new.cpp", diff="")
    result, _ = editor._create_file_impl(op)

    assert result.status == "completed"
    assert (workspace / "new.cpp").read_text(encoding="utf-8") == ""


def test_create_file_rejects_empty_overwrite_before_any_cache_lookup(tmp_path):
    # The empty-diff rejection lives in create_file() itself, ahead of the cache
    # lookup in _cached_op/_run_cached - not only inside _create_file_impl - so a
    # cache entry recorded by a pre-fix version of this tool for this exact empty
    # diff can never be replayed (the cache key has no notion of this rejection
    # and would otherwise happily restore the old truncated-file snapshot and
    # report the old "completed" result).
    editor, workspace = _make_editor(tmp_path)
    (workspace / "query1.cpp").write_text("// real implementation\n", encoding="utf-8")

    op = ApplyPatchOperation(type="create_file", path="query1.cpp", diff="")

    # Simulate a pre-fix cache entry: a prior (buggy) run recorded "completed" for
    # this exact empty-diff overwrite, snapshotting the post-truncation (now-empty)
    # file state.
    payload = {
        "snapshotter_hash": editor._snapshotter.current_hash,
        "op_type": "create_file",
        "path": "query1.cpp",
        "diff": "",
        "untracked_cpp_runner_content": editor._untracked_cpp_runner_content,
    }
    from synnodb.tools.workspace_editor import ApplyPatchCacheType
    from synnodb.utils import utils

    hash_payload = utils.stable_json(payload)
    cache_path = editor._cache_path_for(utils.sha256(hash_payload))
    utils.dump_pickle(
        cache_path,
        ApplyPatchCacheType(
            result_output="Overwrote existing file query1.cpp",
            result_status="completed",
            snapshot_hash="poisoned-empty-snapshot",
            hash_payload=hash_payload,
            runtime_seconds=0.01,
        ),
        do_not_cache=False,
    )

    result = editor.create_file(op)

    assert result.status == "failed"
    assert (workspace / "query1.cpp").read_text(
        encoding="utf-8"
    ) == "// real implementation\n"
    # The stale cache entry must not have been replayed (which would have called
    # restore() and pointed the snapshotter at the poisoned snapshot).
    assert editor._snapshotter.current_hash != "poisoned-empty-snapshot"


def test_create_file_impl_still_blocks_readonly(tmp_path):
    # read-only framework files must never be overwritten via create_file.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=_FakeRunStatsCollector(),  # type: ignore[arg-type]
        readonly_files={"args_parser.hpp"},
        untracked_cpp_runner_content="",
        snapshotter=_FakeSnapshotter(),  # type: ignore[arg-type]
    )
    (workspace / "args_parser.hpp").write_text("// framework", encoding="utf-8")
    op = ApplyPatchOperation(
        type="create_file", path="args_parser.hpp", diff="+hacked\n"
    )
    result, _ = editor._create_file_impl(op)
    assert result.status == "failed" and "read-only" in result.output
    assert (workspace / "args_parser.hpp").read_text(encoding="utf-8") == "// framework"


def test_update_file_impl_strips_fence_and_applies(tmp_path):
    editor, workspace = _make_editor(tmp_path)
    (workspace / "g.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    op = ApplyPatchOperation(
        type="update_file", path="g.txt", diff="```diff\n one\n-two\n+TWO\n three\n```"
    )
    result, _ = editor._update_file_impl(op)

    assert result.status == "completed"
    content = (workspace / "g.txt").read_text(encoding="utf-8")
    assert "TWO" in content
    assert "two" not in content


def test_update_file_impl_handles_fence_envelope_and_unicode_together(tmp_path):
    # the kitchen sink: a fenced, enveloped diff whose context line was retyped
    # with an ASCII hyphen instead of the file's em dash — every layer must compose
    editor, workspace = _make_editor(tmp_path)
    (workspace / "f.txt").write_text("alpha\nbeta — gamma\ndelta\n", encoding="utf-8")

    diff = (
        "```diff\n"
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        " beta - gamma\n"  # em dash mistyped as hyphen
        "-delta\n"
        "+epsilon\n"
        "*** End Patch\n"
        "```"
    )
    op = ApplyPatchOperation(type="update_file", path="f.txt", diff=diff)
    result, _ = editor._update_file_impl(op)

    assert result.status == "completed"
    content = (workspace / "f.txt").read_text(encoding="utf-8")
    assert "epsilon" in content
    assert "delta" not in content
    assert "beta — gamma" in content  # the file's real em-dash bytes are preserved
