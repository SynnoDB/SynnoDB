from pathlib import Path

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


# ---------- happy path ----------


def test_creates_new_file(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)

    result = editor.write_file("a.cpp", "int x = 1;\n")

    assert result.status == "completed"
    assert result.output == "Wrote a.cpp"
    assert (ws / "a.cpp").read_text() == "int x = 1;\n"


def test_overwrites_existing_file(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("old content\n")

    result = editor.write_file("a.cpp", "new content\n")

    assert result.status == "completed"
    assert result.output == "Overwrote a.cpp"
    assert f.read_text() == "new content\n"


def test_creates_parent_dirs_not_needed_but_stats_recorded(tmp_path):
    editor, ws, cache_dir, stats, snap = _make_editor(tmp_path)

    editor.write_file("a.cpp", "line one\nline two\n")

    assert stats.stats[-1]["op_type"] == "write"
    assert stats.stats[-1]["added_lines"] == 2
    assert stats.activity_summary == ["write_file called: a.cpp"]


# ---------- failure modes ----------


def test_readonly_rejected(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path, readonly={"locked.cpp"})
    (ws / "locked.cpp").write_text("hi\n")

    result = editor.write_file("locked.cpp", "bye\n")

    assert result.status == "failed"
    assert "read-only" in result.output
    assert (ws / "locked.cpp").read_text() == "hi\n"


def test_no_change_rejected(tmp_path):
    editor, ws, *_ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("same\n")

    result = editor.write_file("a.cpp", "same\n")

    assert result.status == "failed"
    assert "no change" in result.output


# ---------- caching ----------


def _write_file_cache_key(snapshot_hash, path, content, untracked):
    payload = {
        "snapshotter_hash": snapshot_hash,
        "op_type": "write_file",
        "path": path,
        "content": content,
        "untracked_cpp_runner_content": untracked,
    }
    return utils.sha256(utils.stable_json(payload))


def test_cache_hit_replays_and_restores(tmp_path):
    editor, ws, cache_dir, stats, snap = _make_editor(tmp_path)
    f = ws / "a.cpp"
    f.write_text("original\n")

    key = _write_file_cache_key("start", "a.cpp", "patched\n", "")
    utils.dump_pickle(
        cache_dir / f"{key}.pkl",
        ApplyPatchCacheType(
            result_output="Overwrote a.cpp",
            result_status="completed",
            snapshot_hash="cached-snap",
            hash_payload="",
            runtime_seconds=0.0,
            activity_summary_entry="write_file called: a.cpp",
        ),
        do_not_cache=False,
    )

    result = editor.write_file("a.cpp", "patched\n")

    assert result.status == "completed"
    assert result.output == "Overwrote a.cpp"
    assert snap.restored == ["cached-snap"]
    assert f.read_text() == "original\n"  # cache hit -> file NOT modified
