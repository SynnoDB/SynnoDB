from pathlib import Path

import pytest

from synnodb.tools.workspace_editor import WorkspaceEditor


class FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []
        self.model = "test-model"
        self.last_turn = 0
        self.read_file_paths: list[str] = []
        self.cache_hits = 0

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, op_type, **kwargs) -> None:
        raise AssertionError("read_file must not log_apply_patch_stats")

    def log_read_file_stats(self, path: str) -> None:
        self.read_file_paths.append(path)

    def record_read_file_cache_hit(self) -> None:
        self.cache_hits += 1


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


def _make_editor(tmp_path: Path, **editor_kwargs):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    stats = FakeRunStatsCollector()
    snap = editor_kwargs.pop("snapshotter", FakeSnapshotter())
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snap,  # type: ignore[arg-type]
        cache_dir=cache_dir,
        **editor_kwargs,
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

    assert (
        result
        == "     3\tline3\n     4\tline4\n... (truncated, showing lines 3-4 of 10 total — pass offset/limit to read more)"
    )


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


def test_outside_workspace_returns_error(tmp_path):
    # An absolute path pointing entirely outside the workspace (the real crash
    # scenario: a path into the source tree) must come back as a graceful error
    # string, not raise and crash the run. read_file skips the _run_cached
    # try/except that protects the mutating ops, so it handles _resolve itself.
    editor, ws, stats = _make_editor(tmp_path)
    outside = tmp_path / "outside.cpp"
    outside.write_text("hi\n")

    result = editor.read_file(str(outside))

    assert result.startswith("Error:")
    # containment is checked before the flat-dir guard, so an out-of-workspace
    # path is reported as such rather than mislabelled "no subdirs".
    assert "outside workspace" in result
    assert "no subdirs" not in result
    # the failed read is still surfaced in the activity log
    assert any("FAILED" in entry for entry in stats.activity_summary)
    # and the attempted path is recorded so the rejected read stays diagnosable
    # in the trace/live UI (not logged with a null path).
    assert stats.read_file_paths == [str(outside)]


def test_subdirectory_read_returns_error(tmp_path):
    # A path that IS inside the workspace but one folder deep still trips the
    # flat-dir guard - and, being contained, is correctly reported "no subdirs"
    # rather than "outside workspace".
    editor, ws, _ = _make_editor(tmp_path)
    sub = ws / "nested"
    sub.mkdir()
    (sub / "a.cpp").write_text("int x = 1;\n")

    result = editor.read_file("nested/a.cpp")

    assert result.startswith("Error:")
    assert "no subdirs" in result
    assert "outside workspace" not in result


def test_read_is_replayed_from_cache(tmp_path):
    # A second read with identical arguments at the same snapshot hash replays
    # the recorded result verbatim - even if the file changed on disk behind the
    # snapshotter's back - along with the exact activity-summary line, and is
    # counted as a cache hit. This is the same replay guarantee the shell tool
    # gives: at a given snapshot hash, the workspace state is fixed by contract.
    editor, ws, stats = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("int x = 1;\n")

    first = editor.read_file("a.cpp")
    assert stats.cache_hits == 0

    (ws / "a.cpp").write_text("int x = 999;\n")
    second = editor.read_file("a.cpp")

    assert second == first == "     1\tint x = 1;"
    assert stats.cache_hits == 1
    assert stats.activity_summary == ["read_file called: a.cpp"] * 2


def test_cache_is_keyed_on_arguments_and_snapshot_hash(tmp_path):
    # Different offset/limit or a different snapshot hash is a different cache
    # key: the read runs live and sees the current file content.
    snap = FakeSnapshotter()
    editor, ws, stats = _make_editor(tmp_path, snapshotter=snap)
    (ws / "a.cpp").write_text("int x = 1;\nint y = 2;\n")

    whole = editor.read_file("a.cpp")
    windowed = editor.read_file("a.cpp", offset=2, limit=1)
    assert windowed != whole
    assert stats.cache_hits == 0

    (ws / "a.cpp").write_text("int x = 999;\n")
    snap.current_hash = "after-edit"
    assert editor.read_file("a.cpp") == "     1\tint x = 999;"
    assert stats.cache_hits == 0


def test_failed_read_is_replayed_from_cache(tmp_path):
    # Error outcomes are recorded and replayed like successful ones: a missing
    # file yields the same error string on replay. It never wrote an
    # activity-summary line when recorded, so the replay writes none either.
    editor, ws, stats = _make_editor(tmp_path)

    first = editor.read_file("ghost.cpp")
    second = editor.read_file("ghost.cpp")

    assert second == first
    assert "does not exist" in second
    assert stats.cache_hits == 1
    assert stats.activity_summary == []


def test_only_from_cache_replays_recorded_read(tmp_path):
    # Strict replay: a read recorded by a normal run is served from cache
    # without touching the file (which has since changed on disk).
    recorder, ws, _ = _make_editor(tmp_path)
    (ws / "a.cpp").write_text("int x = 1;\n")
    recorded = recorder.read_file("a.cpp")

    (ws / "a.cpp").write_text("int x = 999;\n")
    replayer, _, stats = _make_editor(tmp_path, only_from_cache=True)

    assert replayer.read_file("a.cpp") == recorded == "     1\tint x = 1;"
    assert stats.cache_hits == 1
    assert stats.activity_summary == ["read_file called: a.cpp"]


def test_only_from_cache_miss_fails_loudly(tmp_path):
    # Under only_from_cache there is no live fallback: an uncached read means
    # the snapshot chain diverged from the recorded run and must not be papered
    # over with a silent live read.
    editor, ws, _ = _make_editor(tmp_path, only_from_cache=True)
    (ws / "a.cpp").write_text("int x = 1;\n")

    with pytest.raises(ValueError, match="only_from_cache"):
        editor.read_file("a.cpp")


def test_only_from_cache_with_cache_disabled_fails_loudly(tmp_path):
    # only_from_cache combined with a disabled cache (here: do_not_cache) has
    # nothing to replay from. Strict replay must not degrade into a silent live
    # read - it fails loudly instead.
    editor, ws, _ = _make_editor(tmp_path, only_from_cache=True, do_not_cache=True)
    (ws / "a.cpp").write_text("int x = 1;\n")

    with pytest.raises(ValueError, match="only_from_cache"):
        editor.read_file("a.cpp")


def test_do_not_cache_reads_live_without_writing_entries(tmp_path):
    # With caching disabled the read still works, always reflects the current
    # file content, and leaves the cache dir untouched.
    editor, ws, stats = _make_editor(tmp_path, do_not_cache=True)
    (ws / "a.cpp").write_text("int x = 1;\n")

    assert editor.read_file("a.cpp") == "     1\tint x = 1;"
    (ws / "a.cpp").write_text("int x = 999;\n")
    assert editor.read_file("a.cpp") == "     1\tint x = 999;"

    assert stats.cache_hits == 0
    assert list((tmp_path / "cache").glob("*.pkl")) == []
    assert stats.activity_summary == ["read_file called: a.cpp"] * 2


def test_binary_file_returns_error(tmp_path):
    editor, ws, stats = _make_editor(tmp_path)
    (ws / "blob.bin").write_bytes(b"\xff\xfe\x00\x80binary\x00")

    result = editor.read_file("blob.bin")

    assert result.startswith("Error:")
    assert "UTF-8" in result
    assert any("FAILED" in entry for entry in stats.activity_summary)
    assert stats.read_file_paths == ["blob.bin"]
