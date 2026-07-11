from pathlib import Path

import pytest
from agents.editor import ApplyPatchOperation

from synnodb.tools.workspace_editor import ApplyPatchCacheType, WorkspaceEditor
from synnodb.utils import utils


class FakeRunStatsCollector:
    def __init__(self) -> None:
        self.activity_summary: list[str] = []

    def add_to_activity_summary(self, entry: str) -> None:
        self.activity_summary.append(entry)

    def log_apply_patch_stats(self, *args, **kwargs) -> None:
        pass

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
