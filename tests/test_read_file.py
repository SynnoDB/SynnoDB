from pathlib import Path

from synnodb.tools.workspace_editor import WorkspaceEditor


class FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []
        self.model = "test-model"
        self.last_turn = 0

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, op_type, **kwargs) -> None:
        raise AssertionError("read_file must not log_apply_patch_stats")


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


def _make_editor(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    stats = FakeRunStatsCollector()
    snap = FakeSnapshotter()
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snap,  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    return editor, workspace, stats


def test_reads_whole_file_numbered(tmp_path):
    editor, ws, stats = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("int x = 1;\nint y = 2;\n")

    result = editor.read_file("a.cpp")

    assert result == "     1\tint x = 1;\n     2\tint y = 2;"
    assert stats.activity_summary == ["read_file called: a.cpp"]


def test_missing_file_returns_error(tmp_path):
    editor, ws, _ = _make_editor(tmp_path)

    result = editor.read_file("ghost.cpp")

    assert result.startswith("Error:")
    assert "does not exist" in result


def test_offset_and_limit(tmp_path):
    editor, ws, _ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")

    result = editor.read_file("a.cpp", offset=3, limit=2)

    assert result == "     3\tline3\n     4\tline4\n... (truncated, showing lines 3-4 of 10 total — pass offset/limit to read more)"


def test_truncation_note_on_long_file(tmp_path):
    editor, ws, _ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("\n".join(f"line{i}" for i in range(1, 2005)) + "\n")

    result = editor.read_file("a.cpp")

    assert "truncated, showing lines 1-2000 of 2004 total" in result


def test_offset_past_end(tmp_path):
    editor, ws, _ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("only one line\n")

    result = editor.read_file("a.cpp", offset=5)

    assert "past the end of the file" in result


def test_root_restriction_rejected(tmp_path):
    editor, ws, _ = _make_editor(tmp_path)
    outside = tmp_path / "outside.cpp"
    outside.write_text("hi\n")

    try:
        editor.read_file(str(outside))
        raised = False
    except RuntimeError:
        raised = True

    assert raised
