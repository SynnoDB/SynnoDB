"""PrepareFeatures: JSON round-trip, resolve() semantics, the feature-to-builder
mapping of the interpreter, feature-delta semantics, the workspace metadata
file, and the checkSfCorrectness replay path.

Each feature maps to exactly one PrepareWorkspace call: ``scaffold`` (with the
storage plan and the query-impl flags baked into the assembly) -> ``prepare``,
and the chain step that newly enables ``tracing`` -> ``prepare_cleanup``. The
concatenated artifacts string is a cache-key input via
``framework_code_content``, so the call order is part of the contract.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from types import SimpleNamespace

import pytest

import synnodb.cpp_runner.prepare_repo.prepare_workspace as prepare_workspace_module
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    PREPARE_METADATA_FILENAME,
    Parallelism,
    PrepareFeatures,
    apply_prepare_features,
    read_prepare_metadata,
    write_prepare_metadata,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import (
    DELETE_KW,
    PrepareWorkspace,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.utils import DBStorage


# ------------------------------ spy provider ---------------------------------
class SpyPrepareWorkspace:
    """Records every builder call and returns distinct artifact strings, so
    both the call sequence and the concatenated artifacts string are compared."""

    def __init__(self, db_storage=DBStorage.IN_MEMORY):
        self.db_storage = db_storage
        self.prepare_cache_dir = None
        self.calls: list[tuple[str, dict]] = []

    def assemble(self, features, write_non_tracked_only=False):
        self.calls.append(
            (
                "prepare",
                {
                    "features": features,
                    "write_non_tracked_only": write_non_tracked_only,
                },
            )
        )
        return SimpleNamespace(
            artifacts_str=f"<scaffold wnto={write_non_tracked_only}>",
            tracked_files={},
            readonly_files_not_git_tracked={},
            tracked_artifacts_str="",
            readonly_artifacts_str="",
        )

    def assemble_cleanup(self):
        self.calls.append(("prepare_cleanup", {}))
        return SimpleNamespace(
            artifacts_str="<cleanup>",
            tracked_files={},
            readonly_files_not_git_tracked={},
            tracked_artifacts_str="",
            readonly_artifacts_str="",
        )

    def write_prepared_files(self, _part, write_tracked=True):
        pass


def _apply(features: PrepareFeatures, ws, source: PrepareFeatures | None) -> str:
    resolved = features.resolve(ws.db_storage)
    source_resolved = source.resolve(ws.db_storage) if source is not None else None
    return apply_prepare_features(resolved, ws, source_resolved)


def _call_names(ws: SpyPrepareWorkspace) -> list[str]:
    return [name for name, _ in ws.calls]


# ------------------------------- JSON round-trip ------------------------------
def test_prepare_features_json_round_trip():
    for features in [
        PrepareFeatures.storage_plan(),
        PrepareFeatures.base(),
        PrepareFeatures.optim(),
        PrepareFeatures.mt(),
        PrepareFeatures(parallel_ready_impl=True, sample_trace=True),
        PrepareFeatures.mt().resolve(DBStorage.SSD),
    ]:
        assert PrepareFeatures.from_json(features.to_json()) == dataclasses.replace(
            features, storage_plan_text=None
        )
    # storage_plan_text is per-run input, deliberately not serialized
    assert (
        PrepareFeatures.base(storage_plan_text="PLAN").to_json()
        == PrepareFeatures.base().to_json()
    )
    with pytest.raises(ValueError, match="Unknown prepare features"):
        PrepareFeatures.from_json('{"tracing": true, "bogus": 1}')


# ---------------------------------- resolve -----------------------------------
def test_resolve_fills_storage_and_parallel_ready():
    in_memory = PrepareFeatures.base().resolve(DBStorage.IN_MEMORY)
    assert in_memory.storage == "in_memory"
    assert in_memory.parallel_ready_impl is True

    ssd = PrepareFeatures.base().resolve(DBStorage.SSD)
    assert ssd.storage == "ssd"
    assert ssd.parallel_ready_impl is False

    # LABSTORE shares the SSD scaffold
    assert PrepareFeatures.base().resolve(DBStorage.LABSTORE).storage == "ssd"

    # an explicit parallel_ready_impl is left untouched
    assert PrepareFeatures.mt().resolve(DBStorage.SSD).parallel_ready_impl is True


def test_resolve_rejects_storage_backend_mismatch():
    ssd_features = PrepareFeatures.base().resolve(DBStorage.SSD)
    with pytest.raises(ValueError, match="storage backend"):
        ssd_features.resolve(DBStorage.IN_MEMORY)
    # resolving against the same backend again is a no-op
    assert ssd_features.resolve(DBStorage.SSD) == ssd_features


# ------------------------- feature-to-builder mapping --------------------------
@pytest.mark.parametrize("from_snapshot", [False, True])
def test_storage_plan_maps_to_scaffold_only(from_snapshot):
    source = PrepareFeatures.storage_plan() if from_snapshot else None
    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.storage_plan(), ws, source)
    assert _call_names(ws) == ["prepare"]
    call = dict(ws.calls)["prepare"]
    assert call["features"].scaffold == "queries_md_only"
    assert call["write_non_tracked_only"] is from_snapshot


@pytest.mark.parametrize("db_storage", [DBStorage.IN_MEMORY, DBStorage.SSD])
def test_base_maps_to_scaffold_with_plan_text(db_storage):
    ws = SpyPrepareWorkspace(db_storage)
    _apply(PrepareFeatures.base(storage_plan_text="STORAGE PLAN TEXT"), ws, None)
    assert _call_names(ws) == ["prepare"]
    features = dict(ws.calls)["prepare"]["features"]
    assert features.storage_plan_text == "STORAGE PLAN TEXT"
    # in-memory scaffolds parallel-ready, SSD does not
    assert features.parallel_ready_impl is (db_storage == DBStorage.IN_MEMORY)


def test_optim_from_base_upgrades_scaffold_and_cleans_up():
    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.optim(), ws, PrepareFeatures.base())
    assert _call_names(ws) == ["prepare", "prepare_cleanup"]
    call = dict(ws.calls)["prepare"]
    # scaffold already present: only untracked support files are refreshed
    # (query_impl.cpp among them, now assembled with tracing)
    assert call["write_non_tracked_only"] is True
    assert call["features"].tracing is True


def test_mt_from_optim_does_not_clean_up_again():
    """Cleanup accompanies the chain step that newly enables tracing - a later
    step whose source already has tracing must not repeat it."""
    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.mt(), ws, PrepareFeatures.optim())
    assert _call_names(ws) == ["prepare"]


def test_fresh_mt_workspace_applies_everything():
    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.mt(), ws, None)
    assert _call_names(ws) == ["prepare", "prepare_cleanup"]
    call = dict(ws.calls)["prepare"]
    assert call["write_non_tracked_only"] is False
    assert call["features"].parallel_ready_impl is True
    assert call["features"].tracing is True


def test_artifacts_string_concatenates_in_canonical_order():
    ws = SpyPrepareWorkspace()
    artifacts = _apply(PrepareFeatures.optim(), ws, PrepareFeatures.base())
    assert artifacts == "<scaffold wnto=True><cleanup>"


def test_disabling_a_recorded_feature_raises():
    # on SSD, optim resolves to parallel_ready_impl=False - chaining it from an
    # mt snapshot (parallel_ready_impl=True) would downgrade the query impl
    ws = SpyPrepareWorkspace(DBStorage.SSD)
    with pytest.raises(ValueError, match="additive"):
        _apply(PrepareFeatures.optim(), ws, PrepareFeatures.mt())
    with pytest.raises(ValueError, match="additive"):
        _apply(PrepareFeatures.storage_plan(), ws, PrepareFeatures.base())
    assert ws.calls == []  # rejected before touching the workspace


def test_changing_storage_along_a_chain_raises():
    requested = PrepareFeatures.optim().resolve(DBStorage.IN_MEMORY)
    source = PrepareFeatures.base().resolve(DBStorage.SSD)
    ws = SpyPrepareWorkspace()
    with pytest.raises(ValueError, match="storage variant"):
        apply_prepare_features(requested, ws, source)
    assert ws.calls == []


# --------------------------- per-feature builders ------------------------------
def _stub_workspace(tmp_path) -> PrepareWorkspace:
    class StubWorkspace(PrepareWorkspace):
        plan_filename = "the_plan.txt"

        def build_scaffold_files(self, features):
            return {}

    return StubWorkspace(
        workload_provider=SimpleNamespace(benchmark_name="stub"),
        workspace_dir=tmp_path,
        git_snapshotter=None,
        db_storage=DBStorage.IN_MEMORY,
        prepare_cache_dir=None,
    )


def test_storage_plan_builder_injects_plan_file(tmp_path):
    ws = _stub_workspace(tmp_path)
    features = PrepareFeatures.base(storage_plan_text="THE PLAN").resolve(
        DBStorage.IN_MEMORY
    )
    assert ws.build_storage_plan_files(features) == {"the_plan.txt": "THE PLAN"}
    assert ws.build_storage_plan_files(PrepareFeatures.base()) == {}


def test_cleanup_builder_deletes_only_existing_base_impl_inputs(tmp_path):
    ws = _stub_workspace(tmp_path)
    assert ws.build_cleanup_deletes() == {}  # nothing to delete on a fresh tree

    (tmp_path / "base_impl_todo.txt").write_text("todo")
    (tmp_path / "trace.hpp").write_text("// stale workspace copy")
    assert ws.build_cleanup_deletes() == {
        "base_impl_todo.txt": DELETE_KW,
        "trace.hpp": DELETE_KW,
    }


# ------------------------------ metadata file ---------------------------------
def test_metadata_serialization_is_deterministic(tmp_path):
    features = PrepareFeatures.optim().resolve(DBStorage.IN_MEMORY)
    write_prepare_metadata(tmp_path, features, parallelism=Parallelism.SINGLE_THREADED)
    first = (tmp_path / PREPARE_METADATA_FILENAME).read_bytes()
    write_prepare_metadata(tmp_path, features, parallelism=Parallelism.SINGLE_THREADED)
    assert (tmp_path / PREPARE_METADATA_FILENAME).read_bytes() == first
    assert first.endswith(b"\n")
    # the enums are recorded as their plain string values
    assert b'"parallelism": "single_threaded"' in first
    assert b'"storage": "in_memory"' in first

    features2, parallelism = read_prepare_metadata(tmp_path)
    assert features2 == dataclasses.replace(features, storage_plan_text=None)
    assert parallelism is Parallelism.SINGLE_THREADED


def test_metadata_requires_resolved_features():
    with pytest.raises(AssertionError, match="resolve the prepare features"):
        write_prepare_metadata(
            None, PrepareFeatures.base(), parallelism=Parallelism.SINGLE_THREADED
        )


def test_missing_metadata_raises_clear_error(tmp_path):
    with pytest.raises(ValueError, match="no prepare record"):
        read_prepare_metadata(tmp_path)


def test_old_metadata_format_raises_clear_error(tmp_path):
    (tmp_path / PREPARE_METADATA_FILENAME).write_text(
        '{"features": {}, "format_version": 2, "parallelism": "single_threaded"}\n'
    )
    with pytest.raises(ValueError, match="format_version"):
        read_prepare_metadata(tmp_path)


def test_metadata_is_readonly_but_git_tracked():
    not_tracked, tracked = PrepareWorkspace._get_readonly_files()
    # read-only for the agent's editor/shell tools ...
    assert PREPARE_METADATA_FILENAME in tracked
    # ... but NOT excluded from git (it must travel with every snapshot)
    assert PREPARE_METADATA_FILENAME not in not_tracked
    # and no gitignore pattern main() installs may match it
    extra_gitignore = [
        "*.o",
        "*.d",
        "/db",
        "/build/",
        "/tmp/",
        "/output/",
        "*.log",
        "*.tmp",
        "*.out",
        "*.bin",
        "*.csv",
    ]
    assert not any(
        fnmatch.fnmatch(PREPARE_METADATA_FILENAME, pat) for pat in extra_gitignore
    )


def test_metadata_survives_snapshot_restore_round_trip(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)
    snapshotter.create_empty_snapshot("metadata-round-trip")

    features = PrepareFeatures.mt().resolve(DBStorage.IN_MEMORY)
    write_prepare_metadata(workspace, features, parallelism=Parallelism.MULTI_THREADED)
    (workspace / "somefile.txt").write_text("content")
    _, commit = snapshotter.snapshot("with-metadata")
    assert commit is not None

    # wipe and restore: the record comes back with the snapshot
    (workspace / PREPARE_METADATA_FILENAME).unlink()
    snapshotter.clear_untracked()
    snapshotter.reset_changes()
    snapshotter.restore(commit)

    restored, parallelism = read_prepare_metadata(workspace)
    assert restored == dataclasses.replace(features, storage_plan_text=None)
    assert parallelism is Parallelism.MULTI_THREADED


# ------------------- prepared snapshot is a single commit ----------------------
class _ScaffoldWorkspace(PrepareWorkspace):
    """A workspace whose scaffold writes git-tracked files, so a prepare
    produces a real content commit."""

    plan_filename = "the_plan.txt"

    def build_scaffold_files(self, features):
        return {"queries.md": "-- q --\n", "engine.cpp": "int main(){}\n"}


def _make_scaffold_workspace(workspace, snapshotter):
    return _ScaffoldWorkspace(
        workload_provider=SimpleNamespace(benchmark_name="stub"),
        workspace_dir=workspace,
        git_snapshotter=snapshotter,
        db_storage=DBStorage.IN_MEMORY,
        prepare_cache_dir=workspace.parent / "prepare_cache",
    )


def _prepare_snapshot_refs(snapshotter):
    refs = snapshotter._git_capture(
        ["for-each-ref", "--format=%(refname)", "refs/snapshots"]
    )
    return [r for r in refs.stdout.splitlines() if "/snapshot-" in r]


def test_prepared_snapshot_is_single_commit_with_record(tmp_path):
    """The prepared workspace is exactly one commit onto the base carrying both
    the scaffold files and the prepare record - there is no separate per-step
    cache commit."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)
    base = snapshotter.create_empty_snapshot("prepared-single")

    prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=None,
        features=PrepareFeatures.base(storage_plan_text="PLAN"),
        conv_name="prepared-single",
        prepare_workspace_provider=_make_scaffold_workspace(workspace, snapshotter),
        parallelism=Parallelism.SINGLE_THREADED,
        do_not_cache=False,
    )

    prepared = snapshotter.current_hash
    assert prepared is not None

    # the prepared snapshot sits directly on the base - a single commit
    parents = snapshotter._git_capture(["rev-list", "--parents", "-n", "1", prepared])
    assert parents.stdout.split()[1:] == [base]

    # that single commit tracks the scaffold files and the prepare record
    tree = snapshotter._git_capture(["ls-tree", "-r", "--name-only", prepared])
    tracked = set(tree.stdout.split())
    assert {"queries.md", "engine.cpp", PREPARE_METADATA_FILENAME} <= tracked

    # exactly one prepared snapshot ref (no extra cache commit)
    assert len(_prepare_snapshot_refs(snapshotter)) == 1


def test_prepared_snapshot_cache_hit_restores_before_tracked_writes(
    tmp_path, monkeypatch
):
    """On a prepare-cache hit, tracked scaffold files must come from the
    prepared snapshot, not from writes performed before the lookup."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)

    prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=None,
        features=PrepareFeatures.base(storage_plan_text="PLAN"),
        conv_name="prepared-cache-hit",
        prepare_workspace_provider=_make_scaffold_workspace(workspace, snapshotter),
        parallelism=Parallelism.SINGLE_THREADED,
        do_not_cache=False,
    )
    prepared = snapshotter.current_hash
    assert prepared is not None

    real_write_files = prepare_workspace_module._write_files

    def fail_on_tracked_scaffold_write(
        files, workspace_dir, delete_kw, require_delete_targets
    ):
        if require_delete_targets and {"queries.md", "engine.cpp"} & set(files):
            pytest.fail("tracked scaffold files were written before cache restore")
        return real_write_files(files, workspace_dir, delete_kw, require_delete_targets)

    monkeypatch.setattr(
        prepare_workspace_module, "_write_files", fail_on_tracked_scaffold_write
    )

    prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=None,
        features=PrepareFeatures.base(storage_plan_text="PLAN"),
        conv_name="prepared-cache-hit",
        prepare_workspace_provider=_make_scaffold_workspace(workspace, snapshotter),
        parallelism=Parallelism.SINGLE_THREADED,
        do_not_cache=False,
    )

    assert snapshotter.current_hash == prepared
    assert len(_prepare_snapshot_refs(snapshotter)) == 1


def test_prepare_do_not_cache_skips_pkl_and_snapshot_write(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)

    prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=None,
        features=PrepareFeatures.base(storage_plan_text="PLAN"),
        conv_name="prepare-do-not-cache",
        prepare_workspace_provider=_make_scaffold_workspace(workspace, snapshotter),
        parallelism=Parallelism.SINGLE_THREADED,
        do_not_cache=True,
    )

    assert _prepare_snapshot_refs(snapshotter) == []
    cache_dir = workspace.parent / "prepare_cache"
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


def test_metadata_differences_produce_distinct_prepared_snapshots(tmp_path):
    """The prepare record is part of the cache key: two runs with identical
    scaffold files but different metadata (parallelism) get their own commit,
    never sharing one that carries the wrong record."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)

    def _prepare(parallelism):
        snapshotter.create_empty_snapshot(f"base-{parallelism.value}")
        prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=None,
            features=PrepareFeatures.base(storage_plan_text="PLAN"),
            conv_name=f"conv-{parallelism.value}",
            prepare_workspace_provider=_make_scaffold_workspace(workspace, snapshotter),
            parallelism=parallelism,
            do_not_cache=False,
        )
        recorded, _ = read_prepare_metadata(workspace)
        return snapshotter.current_hash, recorded

    st_commit, st_features = _prepare(Parallelism.SINGLE_THREADED)
    mt_commit, mt_features = _prepare(Parallelism.MULTI_THREADED)

    # identical resolved features and scaffold files ...
    assert st_features == mt_features
    # ... but distinct commits, each recording its own parallelism
    assert st_commit != mt_commit
    assert len(_prepare_snapshot_refs(snapshotter)) == 2
    for commit, expected in (
        (st_commit, Parallelism.SINGLE_THREADED),
        (mt_commit, Parallelism.MULTI_THREADED),
    ):
        snapshotter.clear_untracked()
        snapshotter.restore(commit)
        _, parallelism = read_prepare_metadata(workspace)
        assert parallelism is expected


# ------------------------- checkSf replay resolution ---------------------------
def test_replay_resolves_features_and_parallelism_from_snapshot(tmp_path):
    """features=None replays the restored snapshot's own prepare record - no
    source_stage / stage-name resolution anywhere."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)
    snapshotter.create_empty_snapshot("replay-source")

    source_features = PrepareFeatures.mt().resolve(DBStorage.IN_MEMORY)
    write_prepare_metadata(
        workspace, source_features, parallelism=Parallelism.MULTI_THREADED
    )
    _, commit = snapshotter.snapshot("source-run")
    assert commit is not None

    ws_spy = SpyPrepareWorkspace()
    result = prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=commit,
        features=None,  # replay
        prepare_workspace_provider=ws_spy,
        parallelism=Parallelism.SINGLE_THREADED,  # ignored on the replay path
    )

    assert result.features == dataclasses.replace(
        source_features, storage_plan_text=None
    )
    assert result.parallelism is Parallelism.MULTI_THREADED
    # every feature is already present: the scaffold call refreshes untracked
    # support files only, and the cleanup step is not repeated
    assert _call_names(ws_spy) == ["prepare"]
    assert dict(ws_spy.calls)["prepare"]["write_non_tracked_only"] is True
    # the replay re-records the source's parallelism in the fresh metadata
    _, recorded_parallelism = read_prepare_metadata(workspace)
    assert recorded_parallelism is Parallelism.MULTI_THREADED
