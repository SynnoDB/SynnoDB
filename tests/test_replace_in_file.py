from pathlib import Path

from agents.editor import ApplyPatchOperation

from synnodb.tools import workspace_editor as wse
from synnodb.tools.workspace_editor import ApplyPatchCacheType, WorkspaceEditor
from synnodb.utils import utils


class FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []
        self.stats: list[dict] = []
        self.model = "test-model"
        self.last_turn = 0

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, op_type, **kwargs) -> None:
        self.stats.append({"op_type": op_type, **kwargs})

    def record_apply_patch_cache_hit(self) -> None:
        self.cache_hits = getattr(self, "cache_hits", 0) + 1


class FakeSnapshotter:
    def __init__(self, current_hash: str = "start") -> None:
        self.current_hash = current_hash
        self.restored: list[str] = []

    def restore(self, snapshot_hash: str) -> None:
        self.current_hash = snapshot_hash
        self.restored.append(snapshot_hash)

    def snapshot(self, name: str):
        self.current_hash = f"snapshot-{name}"
        return None, self.current_hash


def _make_editor(tmp_path: Path, readonly: set[str] = frozenset()):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    stats = FakeRunStatsCollector()
    snap = FakeSnapshotter()
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(readonly),
        untracked_cpp_runner_content="",
        snapshotter=snap,  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    return editor, workspace, cache_dir, stats, snap


def _no_log_errors(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(wse, "log_tool_call_error", lambda **kw: captured.append(kw))
    return captured


# ---------- happy path ----------


def test_exact_unique_replace(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("int x = 1;\nint y = 2;\n")

    result = editor.replace_in_file("a.cpp", "int x = 1;", "int x = 42;")

    assert result.status == "completed"
    assert f.read_text() == "int x = 42;\nint y = 2;\n"


def test_replace_all(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("x\nx\nx\n")

    result = editor.replace_in_file("a.cpp", "x", "y", replace_all=True)

    assert result.status == "completed"
    assert f.read_text() == "y\ny\ny\n"


def test_quote_normalized_fallback(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    # File has curly double quotes; model supplies straight quotes.
    f.write_text("auto s = “hi”;\n")

    result = editor.replace_in_file("a.cpp", 'auto s = "hi";', 'auto s = "bye";')

    assert result.status == "completed"
    assert f.read_text() == 'auto s = "bye";\n'


def test_trailing_whitespace_stripped(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("a\n")

    result = editor.replace_in_file("a.cpp", "a", "b   \nc\t")

    assert result.status == "completed"
    assert f.read_text() == "b\nc\n"


def test_markdown_keeps_trailing_whitespace(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "notes.md"
    f.write_text("a\n")

    # Two trailing spaces are a markdown hard line break — must be preserved.
    result = editor.replace_in_file("notes.md", "a", "b  ")

    assert result.status == "completed"
    assert f.read_text() == "b  \n"


# ---------- failure modes ----------


def test_not_found_echoes_content(tmp_path, monkeypatch):
    captured = _no_log_errors(monkeypatch)
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("int x = 1;\n")

    result = editor.replace_in_file("a.cpp", "does not exist", "y")

    assert result.status == "failed"
    assert "=== CURRENT CONTENT OF a.cpp ===" in result.output
    assert "int x = 1;" in result.output
    assert f.read_text() == "int x = 1;\n"  # unchanged
    assert len(captured) == 1


def test_ambiguous_without_replace_all(tmp_path, monkeypatch):
    captured = _no_log_errors(monkeypatch)
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("x\nx\n")

    result = editor.replace_in_file("a.cpp", "x", "y")

    assert result.status == "failed"
    assert "Found 2 occurrences" in result.output
    assert f.read_text() == "x\nx\n"  # unchanged
    assert len(captured) == 1


def test_empty_old_string(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("hi\n")

    result = editor.replace_in_file("a.cpp", "", "y")

    assert result.status == "failed"
    assert "must not be empty" in result.output


def test_identical_no_op(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("hi\n")

    result = editor.replace_in_file("a.cpp", "hi", "hi")

    assert result.status == "failed"
    assert "identical" in result.output


def test_missing_file(tmp_path):
    editor, *_ = _make_editor(tmp_path)
    result = editor.replace_in_file("ghost.cpp", "x", "y")
    assert result.status == "failed"
    assert "does not exist" in result.output


def test_readonly_rejected(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path, readonly={"locked.cpp"})
    (ws / "locked.cpp").write_text("hi\n")

    result = editor.replace_in_file("locked.cpp", "hi", "bye")

    assert result.status == "failed"
    assert "read-only" in result.output
    assert (ws / "locked.cpp").read_text() == "hi\n"


# ---------- caching ----------


def _replace_cache_key(snapshot_hash, path, old, new, replace_all, untracked):
    payload = {
        "snapshotter_hash": snapshot_hash,
        "op_type": "replace_in_file",
        "path": path,
        "old_string": old,
        "new_string": new,
        "replace_all": replace_all,
        "untracked_cpp_runner_content": untracked,
    }
    return utils.sha256(utils.stable_json(payload))


def test_cache_hit_replays_and_restores(tmp_path):
    editor, ws, cache_dir, stats, snap = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("original\n")

    key = _replace_cache_key("start", "a.cpp", "original", "patched", False, "")
    utils.dump_pickle(
        cache_dir / f"{key}.pkl",
        ApplyPatchCacheType(
            result_output="Replaced 1 occurrence in a.cpp",
            result_status="completed",
            snapshot_hash="cached-snap",
            hash_payload="",
            runtime_seconds=0.0,
            activity_summary_entry="replace_in_file called: a.cpp",
        ),
        do_not_cache=False,
    )

    result = editor.replace_in_file("a.cpp", "original", "patched")

    assert result.status == "completed"
    assert result.output == "Replaced 1 occurrence in a.cpp"
    assert snap.restored == ["cached-snap"]
    assert f.read_text() == "original\n"  # cache hit -> file NOT modified


def test_apply_patch_cache_key_unchanged(tmp_path):
    """Regression: the _run_cached refactor must keep apply_patch cache keys
    byte-identical, so previously cached apply_patch results still hit."""
    editor, ws, cache_dir, stats, snap = _make_editor(tmp_path)
    f = ws / "q.cpp"
    f.write_text("old\n")

    diff = "@@\n-old\n+new\n"
    operation = ApplyPatchOperation(type="update_file", path="q.cpp", diff=diff)

    # Key computed with the pre-refactor payload shape.
    expected_key = utils.sha256(
        utils.stable_json(
            {
                "snapshotter_hash": "start",
                "op_type": "update_file",
                "path": "q.cpp",
                "diff": diff,
                "untracked_cpp_runner_content": "",
            }
        )
    )

    editor.update_file(operation)

    assert (cache_dir / f"{expected_key}.pkl").exists()
    assert f.read_text() == "new\n"
