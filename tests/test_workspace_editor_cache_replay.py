from pathlib import Path

import pytest
from agents.editor import ApplyPatchOperation

from synnodb.tools.workspace_editor import (
    ApplyPatchCacheType,
    RejectedApplyPatchCacheType,
    WorkspaceEditor,
)
from synnodb.utils import utils


class FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []
        self.rejected: list[tuple[str | None, str]] = []

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, *args, **kwargs) -> None:
        pass

    def record_apply_patch_cache_hit(self) -> None:
        self.cache_hits = getattr(self, "cache_hits", 0) + 1

    def record_apply_patch_rejected(self, path: str | None, reason: str) -> None:
        self.rejected.append((path, reason))


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


def _cache_key_for(
    snapshot_hash: str, operation: ApplyPatchOperation, untracked: str
) -> str:
    payload = {
        "snapshotter_hash": snapshot_hash,
        "op_type": operation.type,
        "path": operation.path,
        "diff": operation.diff,
        "untracked_cpp_runner_content": untracked,
    }
    return utils.sha256(utils.stable_json(payload))


def _write_legacy_cache_entry(
    cache_dir: Path, operation: ApplyPatchOperation, snapshot_hash: str
) -> Path:
    """Persist a cache entry in the pre-``activity_summary_entry`` format (the
    attribute is deleted, exactly as an old pickle would deserialize)."""
    cache_key = _cache_key_for(snapshot_hash, operation, "")
    cache_path = cache_dir / f"{cache_key}.pkl"
    cached = ApplyPatchCacheType(
        result_output="Updated query_impl.cpp",
        result_status="completed",
        snapshot_hash="post-patch",
        hash_payload="{}",
        runtime_seconds=1.0,
    )
    delattr(cached, "activity_summary_entry")
    utils.dump_pickle(cache_path, cached, do_not_cache=False)
    return cache_path


def test_legacy_cache_entry_is_recomputed_not_reconstructed(
    tmp_path: Path,
) -> None:
    # A cache entry that predates the stored activity_summary_entry field is NOT
    # replayed by reconstructing its supervisor line from the result text (that
    # reconstruction could silently drift from the live string and miss the
    # supervisor LLM cache). Instead it is treated as a miss and recomputed: the
    # op runs for real, records today's exact activity line, and the entry is
    # rewritten in the current format.
    stats = FakeRunStatsCollector()
    snapshotter = FakeSnapshotter()
    cache_dir = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    cache_dir.mkdir()
    workspace.mkdir()

    operation = ApplyPatchOperation(
        type="create_file",
        path="query_impl.cpp",
        diff="+hello\n",
    )
    cache_path = _write_legacy_cache_entry(
        cache_dir, operation, snapshotter.current_hash
    )

    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snapshotter,  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )

    result = editor.create_file(operation)

    assert result.status == "completed"
    # Recomputed for real (the stale snapshot was never restored) ...
    assert snapshotter.restored == []
    assert (workspace / "query_impl.cpp").read_text() == "hello"
    # ... producing the live activity line, not a reconstructed one.
    assert stats.activity_summary == ["Apply_patch called: Create file query_impl.cpp"]
    # The cache is refreshed into the current format so later replays hit cleanly.
    refreshed = utils.load_pickle(cache_path, ApplyPatchCacheType)
    assert refreshed is not None
    assert (
        refreshed.activity_summary_entry
        == "Apply_patch called: Create file query_impl.cpp"
    )


def test_legacy_cache_entry_rejected_under_only_from_cache(
    tmp_path: Path,
) -> None:
    # Under only_from_cache there is no recompute fallback, so a legacy entry that
    # cannot be replayed deterministically must fail loudly rather than silently
    # diverge.
    stats = FakeRunStatsCollector()
    snapshotter = FakeSnapshotter()
    cache_dir = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    cache_dir.mkdir()
    workspace.mkdir()

    operation = ApplyPatchOperation(
        type="update_file",
        path="query_impl.cpp",
        diff="@@\n-old\n+new\n",
    )
    _write_legacy_cache_entry(cache_dir, operation, snapshotter.current_hash)

    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snapshotter,  # type: ignore[arg-type]
        cache_dir=cache_dir,
        only_from_cache=True,
    )

    with pytest.raises(ValueError, match="predates the stored activity-summary"):
        editor.update_file(operation)


def test_uncached_apply_patch_stores_activity_summary_in_cache(
    tmp_path: Path,
) -> None:
    stats = FakeRunStatsCollector()
    snapshotter = FakeSnapshotter()
    cache_dir = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    cache_dir.mkdir()
    workspace.mkdir()

    operation = ApplyPatchOperation(
        type="create_file",
        path="query_impl.cpp",
        diff="+hello\n",
    )
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snapshotter,  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )

    result = editor.create_file(operation)

    assert result.status == "completed"
    assert stats.activity_summary == ["Apply_patch called: Create file query_impl.cpp"]
    cache_files = list(cache_dir.glob("*.pkl"))
    assert len(cache_files) == 1
    cached = utils.load_pickle(cache_files[0], ApplyPatchCacheType)
    assert cached is not None
    assert (
        cached.activity_summary_entry
        == "Apply_patch called: Create file query_impl.cpp"
    )


def _rejected_editor(tmp_path: Path, **kwargs) -> tuple[WorkspaceEditor, object, Path]:
    stats = FakeRunStatsCollector()
    snapshotter = FakeSnapshotter()
    cache_dir = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    cache_dir.mkdir()
    workspace.mkdir()
    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snapshotter,  # type: ignore[arg-type]
        cache_dir=cache_dir,
        **kwargs,
    )
    return editor, stats, cache_dir


def test_rejected_apply_patch_records_then_replays_from_cache(tmp_path: Path) -> None:
    # A schema-rejected apply_patch (never reaches the file ops) is cached on its raw
    # arguments. The first encounter is a live miss: replay_rejected_patch returns
    # None, and record_rejected_patch writes the entry. An identical later call is
    # replayed verbatim from cache.
    args_json = '{"path": "db_loader.cpp", "diff": "+x\\n"}'  # missing `type`
    message = "Error: apply_patch arguments failed validation. Retry ...\ntype missing"
    editor, stats, cache_dir = _rejected_editor(tmp_path)

    # miss: nothing cached yet
    assert editor.replay_rejected_patch(args_json) is None
    assert getattr(stats, "cache_hits", 0) == 0

    editor.record_rejected_patch(args_json, "db_loader.cpp", "missing type", message)
    assert stats.rejected == [("db_loader.cpp", "missing type")]
    entry = utils.load_pickle(list(cache_dir.glob("*.pkl"))[0], RejectedApplyPatchCacheType)
    assert entry is not None and entry.message == message

    # replay: the recorded verdict + exact message come back from cache
    assert editor.replay_rejected_patch(args_json) == message
    assert stats.cache_hits == 1
    assert stats.rejected[-1] == ("db_loader.cpp", "missing type")


def test_rejected_apply_patch_replays_even_if_current_rules_would_accept_it(
    tmp_path: Path,
) -> None:
    # The whole point of looking up BEFORE validation: once a rejection is recorded,
    # replaying its arguments reproduces the rejection regardless of what the current
    # validation rules would now decide. The lookup is purely argument-keyed and
    # never re-runs validation, so a rules change cannot alter an old run's outcome.
    args_json = '{"path": "db_loader.cpp"}'
    message = "Error: apply_patch arguments failed validation. Retry ...\nold verdict"
    editor, stats, _cache_dir = _rejected_editor(tmp_path)
    editor.record_rejected_patch(args_json, "db_loader.cpp", "old rule", message)

    assert editor.replay_rejected_patch(args_json) == message
    assert stats.cache_hits == 1


def test_rejected_apply_patch_record_is_readonly_under_only_from_cache(
    tmp_path: Path,
) -> None:
    # Strict replay never writes: an uncached rejection under only_from_cache is
    # still surfaced as a rejected step, but no cache entry is created.
    editor, stats, cache_dir = _rejected_editor(tmp_path, only_from_cache=True)

    editor.record_rejected_patch('{"path": "x"}', "x", "missing type", "msg")

    assert stats.rejected == [("x", "missing type")]
    assert list(cache_dir.glob("*.pkl")) == []
