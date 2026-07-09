from pathlib import Path

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
        pass


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


def test_cached_apply_patch_replays_activity_summary_for_legacy_cache(
    tmp_path: Path,
) -> None:
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
    cache_key = _cache_key_for(snapshotter.current_hash, operation, "")
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

    editor = WorkspaceEditor(
        root=workspace,
        run_stats_collector=stats,  # type: ignore[arg-type]
        readonly_files=set(),
        untracked_cpp_runner_content="",
        snapshotter=snapshotter,  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )

    result = editor.update_file(operation)

    assert result.status == "completed"
    assert result.output == "Updated query_impl.cpp"
    assert snapshotter.restored == ["post-patch"]
    assert stats.activity_summary == ["Apply_patch called: Update file query_impl.cpp"]


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
