"""PrepareFeatures: JSON round-trip, interpreter equivalence with the legacy
per-stage prepare functions, feature-delta semantics, the workspace metadata
file, and the checkSfCorrectness replay path.

The interpreter must call the PrepareWorkspace primitives with exactly the
arguments (and in exactly the order) the four legacy functions
(prepare_storage_plan / prepare_base / prepare_optim / prepare_mt in the
deleted prepare_olap.py) used, because the concatenated artifacts string is a
cache-key input via framework_code_content.
"""

from __future__ import annotations

import dataclasses
import fnmatch

import pytest

from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    PREPARE_METADATA_FILENAME,
    PrepareFeatures,
    apply_prepare_features,
    read_prepare_metadata,
    write_prepare_metadata,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.utils import DBStorage


# ------------------------------ spy provider ---------------------------------
class SpyPrepareWorkspace:
    """Records every primitive call and returns distinct artifact strings, so
    both the call sequence and the concatenated artifacts string are compared."""

    def __init__(self, db_storage=DBStorage.IN_MEMORY):
        self.db_storage = db_storage
        self.calls: list[tuple[str, dict]] = []

    def prepare(self, **kwargs):
        self.calls.append(("prepare", kwargs))
        return f"<scaffold only_query_md={kwargs['only_query_md']} wnto={kwargs['write_non_tracked_only']} usecase_args={sorted(kwargs['usecase_args'].items())}>"

    def prepare_optim(self, **kwargs):
        self.calls.append(("prepare_optim", kwargs))
        return f"<tracing wnto={kwargs['write_non_tracked_only']}>"

    def prepare_mt(self, **kwargs):
        self.calls.append(("prepare_mt", kwargs))
        return "<mt_helpers>"


def _legacy_prepare_storage_plan(ws, write_non_tracked_only: bool) -> str:
    """Verbatim legacy call sequence (prepare_olap.prepare_storage_plan)."""
    usecase_args = {
        "add_thread_pool_to_query_impl": False,
        "add_sample_trace": False,
    }
    return ws.prepare(
        only_query_md=True,
        write_non_tracked_only=write_non_tracked_only,
        only_from_cache=False,
        do_not_cache=True,
        usecase_args=usecase_args,
    )


def _legacy_prepare_base(ws, write_non_tracked_only: bool, storage_plan=None) -> str:
    """Verbatim legacy call sequence (prepare_olap.prepare_base)."""
    parallel_ready_in_memory = ws.db_storage == DBStorage.IN_MEMORY
    usecase_args = {
        **({"storage_plan": storage_plan} if storage_plan is not None else {}),
        "add_thread_pool_to_query_impl": parallel_ready_in_memory,
        "add_sample_trace": False,
    }
    return ws.prepare(
        only_query_md=False,
        write_non_tracked_only=write_non_tracked_only,
        only_from_cache=False,
        do_not_cache=True,
        usecase_args=usecase_args,
    )


def _legacy_prepare_optim(ws, write_non_tracked_only: bool) -> str:
    """Verbatim legacy call sequence (prepare_olap.prepare_optim)."""
    artifacts = _legacy_prepare_base(ws, write_non_tracked_only)
    # legacy always upgraded the tracked query_impl.cpp on this path
    artifacts += ws.prepare_optim(
        write_non_tracked_only=False, only_from_cache=False, do_not_cache=True
    )
    return artifacts


def _legacy_prepare_mt(ws, write_non_tracked_only: bool) -> str:
    """Verbatim legacy call sequence (prepare_olap.prepare_mt)."""
    usecase_args = {
        "add_thread_pool_to_query_impl": True,
        "add_sample_trace": False,
    }
    artifacts = ws.prepare(
        only_query_md=False,
        write_non_tracked_only=write_non_tracked_only,
        only_from_cache=False,
        do_not_cache=True,
        usecase_args=usecase_args,
    )
    # legacy relied on the snapshot already carrying tracing on this path
    artifacts += ws.prepare_optim(
        write_non_tracked_only=True, only_from_cache=False, do_not_cache=True
    )
    artifacts += ws.prepare_mt(only_from_cache=False, do_not_cache=True)
    return artifacts


def _apply(features: PrepareFeatures, ws, source: PrepareFeatures | None) -> str:
    resolved = features.resolve(in_memory_storage=ws.db_storage == DBStorage.IN_MEMORY)
    return apply_prepare_features(resolved, ws, source)


def _canonical_calls(ws: SpyPrepareWorkspace):
    return [(name, {k: v for k, v in kwargs.items()}) for name, kwargs in ws.calls]


# ------------------------------- JSON round-trip ------------------------------
def test_prepare_features_json_round_trip():
    for features in [
        PrepareFeatures.storage_plan(),
        PrepareFeatures.base(),
        PrepareFeatures.optim(),
        PrepareFeatures.mt(),
        PrepareFeatures(parallel_ready_impl=True, sample_trace=True),
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


# --------------------- interpreter == legacy, byte for byte -------------------
@pytest.mark.parametrize("from_snapshot", [False, True])
def test_interpreter_matches_legacy_storage_plan(from_snapshot):
    source = PrepareFeatures.storage_plan().resolve(False) if from_snapshot else None
    ws_new, ws_old = SpyPrepareWorkspace(), SpyPrepareWorkspace()
    new = _apply(PrepareFeatures.storage_plan(), ws_new, source)
    old = _legacy_prepare_storage_plan(ws_old, write_non_tracked_only=from_snapshot)
    assert new == old
    assert _canonical_calls(ws_new) == _canonical_calls(ws_old)


@pytest.mark.parametrize("db_storage", [DBStorage.IN_MEMORY, DBStorage.SSD])
@pytest.mark.parametrize("from_snapshot", [False, True])
def test_interpreter_matches_legacy_base(from_snapshot, db_storage):
    source = (
        PrepareFeatures.base().resolve(db_storage == DBStorage.IN_MEMORY)
        if from_snapshot
        else None
    )
    ws_new, ws_old = SpyPrepareWorkspace(db_storage), SpyPrepareWorkspace(db_storage)
    plan_text = None if from_snapshot else "STORAGE PLAN TEXT"
    new = _apply(PrepareFeatures.base(storage_plan_text=plan_text), ws_new, source)
    old = _legacy_prepare_base(
        ws_old, write_non_tracked_only=from_snapshot, storage_plan=plan_text
    )
    assert new == old
    assert _canonical_calls(ws_new) == _canonical_calls(ws_old)


@pytest.mark.parametrize("from_snapshot", [False, True])
def test_interpreter_matches_legacy_optim(from_snapshot):
    # runOptimLoop starts from a base snapshot: scaffold present, tracing new
    source = PrepareFeatures.base().resolve(True) if from_snapshot else None
    ws_new, ws_old = SpyPrepareWorkspace(), SpyPrepareWorkspace()
    new = _apply(PrepareFeatures.optim(), ws_new, source)
    old = _legacy_prepare_optim(ws_old, write_non_tracked_only=from_snapshot)
    assert new == old
    assert _canonical_calls(ws_new) == _canonical_calls(ws_old)


@pytest.mark.parametrize("from_snapshot", [False, True])
def test_interpreter_matches_legacy_mt(from_snapshot):
    if from_snapshot:
        # addMultiThreading starts from an optim snapshot: tracing already there
        source = PrepareFeatures.optim().resolve(True)
        ws_new, ws_old = SpyPrepareWorkspace(), SpyPrepareWorkspace()
        new = _apply(PrepareFeatures.mt(), ws_new, source)
        old = _legacy_prepare_mt(ws_old, write_non_tracked_only=True)
        assert new == old
        assert _canonical_calls(ws_new) == _canonical_calls(ws_old)
    else:
        # fresh MT workspace: every feature applies fully. The legacy function
        # hardcoded wnto=True for its tracing step (it assumed a snapshot); the
        # generalized delta writes tracked files on a fresh workspace, which is
        # the correct fresh behavior - assert the flags directly instead.
        ws_new = SpyPrepareWorkspace()
        _apply(PrepareFeatures.mt(), ws_new, None)
        assert [name for name, _ in ws_new.calls] == [
            "prepare",
            "prepare_optim",
            "prepare_mt",
        ]
        assert ws_new.calls[0][1]["write_non_tracked_only"] is False
        assert ws_new.calls[1][1]["write_non_tracked_only"] is False


def test_delta_newly_enabled_vs_already_present_tracing():
    """The single rule that replaces the prepare_optim/prepare_mt special cases:
    tracing writes tracked files iff the source snapshot does not have it."""
    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.optim(), ws, PrepareFeatures.base().resolve(True))
    tracing_call = dict(ws.calls)["prepare_optim"]
    assert tracing_call["write_non_tracked_only"] is False  # newly enabled

    ws = SpyPrepareWorkspace()
    _apply(PrepareFeatures.mt(), ws, PrepareFeatures.optim().resolve(True))
    tracing_call = dict(ws.calls)["prepare_optim"]
    assert tracing_call["write_non_tracked_only"] is True  # already present


def test_disabling_a_recorded_feature_raises():
    ws = SpyPrepareWorkspace()
    with pytest.raises(ValueError, match="additive"):
        _apply(PrepareFeatures.optim(), ws, PrepareFeatures.mt().resolve(True))
    with pytest.raises(ValueError, match="additive"):
        _apply(PrepareFeatures.storage_plan(), ws, PrepareFeatures.base().resolve(True))
    assert ws.calls == []  # rejected before touching the workspace


# ------------------------------ metadata file ---------------------------------
def test_metadata_serialization_is_deterministic(tmp_path):
    features = PrepareFeatures.optim().resolve(True)
    write_prepare_metadata(tmp_path, features, parallelism=False)
    first = (tmp_path / PREPARE_METADATA_FILENAME).read_bytes()
    write_prepare_metadata(tmp_path, features, parallelism=False)
    assert (tmp_path / PREPARE_METADATA_FILENAME).read_bytes() == first
    assert first.endswith(b"\n")

    features2, parallelism = read_prepare_metadata(tmp_path)
    assert features2 == dataclasses.replace(features, storage_plan_text=None)
    assert parallelism is False


def test_metadata_requires_resolved_parallel_ready():
    with pytest.raises(AssertionError, match="resolve parallel_ready_impl"):
        write_prepare_metadata(None, PrepareFeatures.base(), parallelism=False)


def test_missing_metadata_raises_clear_error(tmp_path):
    with pytest.raises(ValueError, match="no prepare record"):
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

    features = PrepareFeatures.mt().resolve(True)
    write_prepare_metadata(workspace, features, parallelism=True)
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
    assert parallelism is True


# ------------------------- checkSf replay resolution ---------------------------
def test_replay_resolves_features_and_parallelism_from_snapshot(tmp_path):
    """features=None replays the restored snapshot's own prepare record - no
    source_stage / stage-name resolution anywhere."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshotter = GitSnapshotter(working_dir=workspace)
    snapshotter.create_empty_snapshot("replay-source")

    source_features = PrepareFeatures.mt().resolve(True)
    write_prepare_metadata(workspace, source_features, parallelism=True)
    _, commit = snapshotter.snapshot("source-run")
    assert commit is not None

    ws_spy = SpyPrepareWorkspace()
    result = prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=commit,
        features=None,  # replay
        prepare_workspace_provider=ws_spy,
        parallelism=False,  # ignored on the replay path
    )

    assert result.features == dataclasses.replace(
        source_features, storage_plan_text=None
    )
    assert result.parallelism is True
    # every recorded feature refreshes untracked support files only
    calls = dict(ws_spy.calls)
    assert calls["prepare"]["write_non_tracked_only"] is True
    assert calls["prepare_optim"]["write_non_tracked_only"] is True
    assert (
        "prepare_mt",
        {"only_from_cache": False, "do_not_cache": True},
    ) in ws_spy.calls
    # the replay re-records the source's parallelism in the fresh metadata
    _, recorded_parallelism = read_prepare_metadata(workspace)
    assert recorded_parallelism is True
