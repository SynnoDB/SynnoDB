"""A4 publish gate: never ship an engine that did not pass a live, cache-bypassed validation.

The incident this reproduces: a base-generation run published an engine whose every query was
``incorrect`` (the box had OOM'd, so the binary no longer loaded), because (a) the only publish
gate was "does a query yield a routable template" - which never runs the binary - and (b) a plain
validation call could be answered from the pickled validation cache and bless the now-broken engine
with an earlier cached success.

Two layers are tested:

1. The cache bypass at its exact mechanism (``QueryValidator._check_answer_from_cache``): a
   populated cache entry replays its verdict AND restores its snapshot over the current build
   unless ``force_live`` is set. ``force_live`` is what the publish gate uses, so a stale success
   can no longer bless a since-broken build.
2. The publish API itself: it requires a :class:`ValidationReceipt` and refuses (writing nothing)
   on a missing/non-live/failed/mismatched receipt or an unvalidated serving plane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synnodb.tools.validate.query_validator_class import (
    ExecValidateResult,
    QueryValidator,
)
from synnodb.utils import utils
from synnodb.workloads.validation_receipt import (
    FAIL,
    PASS,
    PLANE_PARQUET,
    PLANE_SHM,
    ReceiptRejected,
    ValidatedQuery,
    ValidationReceipt,
    engine_build_ids,
    verify_receipt_for_publish,
)

# Reuse the cache-test scaffolding (DummyWorkload / FakeSnapshotter / _batch).
from test_query_validator_cache import FakeSnapshotter, _batch

from receipt_helpers import passing_receipt, write_fake_engine_db


# --------------------------------------------------------------------------- #
# Layer 1: the cache bypass (the literal incident mechanism)
# --------------------------------------------------------------------------- #
def _validator(cache_dir: Path, snapshotter: FakeSnapshotter) -> QueryValidator:
    return QueryValidator(
        validate_cache_dir=cache_dir,
        workspace_path=cache_dir,
        query_execution_cache=object(),  # type: ignore[arg-type]
        all_query_ids=["1"],
        git_snapshotter=snapshotter,
    )


def _populate_cache_with_success(validator: QueryValidator, batch) -> Path:
    """Write a cached *success* for *batch* (an "earlier success" snapshot), the way a normal run
    would, and return its cache path."""
    _, cache_path, cache_hash, _ = validator._check_answer_from_cache(
        skip_validate=False,
        other_config={"optimize": True, "memory_budget_mb": None},
        stop_on_first_error=True,
        compile_key_hash="compile-hash",
        query_batch=batch,
    )
    assert cache_path is not None
    cached = ExecValidateResult(
        message="cached success",
        success=True,
        metrics={},
        snapshot_hash="earlier-success-snapshot",
    )
    utils.dump_pickle(cache_path, cached, do_not_cache=False)
    return cache_path


def test_cache_replay_blesses_a_stale_success_without_force_live(tmp_path):
    """Reproduce the incident: a populated cache replays its success AND restores its snapshot,
    so a now-broken build would be validated as correct from an earlier run's cache."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snap = FakeSnapshotter(current_hash="current-broken-build")
    validator = _validator(cache_dir, snap)
    batch = _batch('1 req-x "1"')
    _populate_cache_with_success(validator, batch)

    result, _, _, _ = validator._check_answer_from_cache(
        skip_validate=False,
        other_config={"optimize": True, "memory_budget_mb": None},
        stop_on_first_error=True,
        compile_key_hash="compile-hash",
        query_batch=batch,
        force_live=False,
    )
    assert (
        result is not None and result.success is True
    )  # the stale success is replayed
    assert snap.restored == [
        "earlier-success-snapshot"
    ]  # and it overwrote the current build


def test_force_live_bypasses_the_cache_and_does_not_restore(tmp_path):
    """The fix: force_live never replays the cached verdict and never restores its snapshot, so the
    caller is forced to execute the current build live."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snap = FakeSnapshotter(current_hash="current-broken-build")
    validator = _validator(cache_dir, snap)
    batch = _batch('1 req-x "1"')
    cache_path = _populate_cache_with_success(validator, batch)

    result, returned_path, _, _ = validator._check_answer_from_cache(
        skip_validate=False,
        other_config={"optimize": True, "memory_budget_mb": None},
        stop_on_first_error=True,
        compile_key_hash="compile-hash",
        query_batch=batch,
        force_live=True,
    )
    assert result is None  # no replay: the caller must run live
    assert (
        snap.restored == []
    )  # the current build is left in place (no stale snapshot restored)
    assert (
        returned_path == cache_path
    )  # path still returned so the live result refreshes the cache


# --------------------------------------------------------------------------- #
# Layer 2: the publish API refuses anything the receipt does not prove
# --------------------------------------------------------------------------- #
def _ws(tmp_path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(
        ws / "db"
    )  # a real build-id so the gate's identity check is exercised
    return ws


def _receipt(ws, **over) -> ValidationReceipt:
    base = dict(
        snapshot_id="snap",
        build_ids=engine_build_ids(ws),
        validated_queries=(ValidatedQuery("1", ()),),
        coverage_policy="x",
        data_planes=(PLANE_PARQUET,),
        dataset="tpch",
        validated_scale_factors=(1.0,),
        mode="exhaustive",
        live_run=True,
        verdict=PASS,
    )
    base.update(over)
    return ValidationReceipt(**base)


def test_publish_requires_a_receipt_argument():
    from synnodb.workloads.engine_publish import publish_engine

    import inspect

    params = inspect.signature(publish_engine).parameters
    assert "receipt" in params and params["receipt"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"live_run": False}, "not from a live run"),
        ({"verdict": FAIL}, "verdict"),
        ({"build_ids": {"db": "deadbeef"}}, "build-ids"),
        ({"validated_queries": (ValidatedQuery("99", ()),)}, "does not cover"),
        ({"validated_scale_factors": (99.0,)}, "scale factor"),
        # A non-None publish scale factor must be present in the receipt; an empty list is not a
        # wildcard (Timo: empty validated_scale_factors previously passed vacuously).
        ({"validated_scale_factors": ()}, "scale factor"),
    ],
)
def test_verify_refuses(tmp_path, over, needle):
    ws = _ws(tmp_path)
    with pytest.raises(ReceiptRejected) as ei:
        verify_receipt_for_publish(
            _receipt(ws, **over),
            workspace=ws,
            published_query_ids=["1"],
            scale_factor=1.0,
        )
    assert needle in str(ei.value)


def test_verify_accepts_a_matching_receipt(tmp_path):
    ws = _ws(tmp_path)
    # No exception == accepted.
    verify_receipt_for_publish(
        _receipt(ws), workspace=ws, published_query_ids=["1"], scale_factor=1.0
    )


def test_verify_refuses_an_unidentifiable_build(tmp_path):
    """An engine with no readable build-id cannot be tied to what was validated; the identity
    check must not pass vacuously on empty-equals-empty (Timo: prebuilt/external workspaces)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "db").write_bytes(b"\x7fELF-no-build-id")  # a stub with no NT_GNU_BUILD_ID
    with pytest.raises(ReceiptRejected, match="cannot be identified"):
        verify_receipt_for_publish(
            ValidationReceipt(
                snapshot_id="s",
                build_ids={},
                validated_queries=(ValidatedQuery("1", ()),),
                coverage_policy="x",
                data_planes=(PLANE_PARQUET,),
                dataset="tpch",
                validated_scale_factors=(1.0,),
                mode="m",
                live_run=True,
                verdict=PASS,
            ),
            workspace=ws,
            published_query_ids=["1"],
            scale_factor=1.0,
        )


def test_publish_refuses_a_parquet_engine_on_a_shm_only_receipt(tmp_path):
    """A parquet-serving engine (a bundled/served parquet snapshot) must not publish on a receipt
    that validated only the shm plane - the served plane was never proven (Timo)."""
    from synnodb.router.manifest import QueryTemplate
    from synnodb.workloads.engine_publish import publish_engine

    ws = _ws(tmp_path)
    engines = tmp_path / "engines"
    with pytest.raises(ReceiptRejected, match="did not validate the parquet plane"):
        publish_engine(
            ws,
            query_templates=[QueryTemplate("1", "select 1", ())],
            receipt=_receipt(ws, data_planes=(PLANE_SHM,)),
            parquet_dir="/data/sf1",
            engines_dir=str(engines),
            scale_factor=1.0,
        )
    assert not engines.exists() or not any(engines.iterdir())


def test_publish_refuses_broken_engine_even_with_a_populated_cache(tmp_path):
    """The A4 gate, end to end at the publish layer: an engine whose live validation FAILED is not
    published, regardless of any earlier cached success - and nothing is written to the engines dir.
    The receipt is the structural carrier of "this build failed live"; a failed verdict is refused.
    """
    from synnodb.router.manifest import QueryTemplate
    from synnodb.workloads.engine_publish import publish_engine

    ws = _ws(tmp_path)
    engines = tmp_path / "engines"
    with pytest.raises(ReceiptRejected):
        publish_engine(
            ws,
            query_templates=[QueryTemplate("1", "select 1", ())],
            receipt=_receipt(ws, verdict=FAIL),
            engines_dir=str(engines),
            scale_factor=1.0,
        )
    # Fail-closed: nothing leaked into the engines directory.
    assert not engines.exists() or not any(engines.iterdir())


def test_publish_downgrades_shm_on_a_parquet_only_receipt(tmp_path):
    """A shm-capable engine whose receipt only validated parquet is published parquet-only (the
    shm serving plane is withheld), never shipped with an unverified plane."""
    from synnodb.router.manifest import EngineManifest, QueryTemplate
    from synnodb.workloads.engine_publish import publish_engine

    ws = _ws(tmp_path)
    engines = tmp_path / "engines"
    dest = publish_engine(
        ws,
        query_templates=[QueryTemplate("1", "select 1", ())],
        receipt=_receipt(ws, data_planes=(PLANE_PARQUET,)),
        parquet_dir="/data/sf1",
        engines_dir=str(engines),
        scale_factor=1.0,
        shm_capable=True,
    )
    assert dest is not None
    assert EngineManifest.read(dest / "manifest.json").shm_capable is False


def test_publish_keeps_shm_when_receipt_covers_it(tmp_path):
    from synnodb.router.manifest import EngineManifest, QueryTemplate
    from synnodb.workloads.engine_publish import publish_engine

    ws = _ws(tmp_path)
    engines = tmp_path / "engines"
    dest = publish_engine(
        ws,
        query_templates=[QueryTemplate("1", "select 1", ())],
        receipt=_receipt(ws, data_planes=(PLANE_PARQUET, PLANE_SHM)),
        parquet_dir="/data/sf1",
        engines_dir=str(engines),
        scale_factor=1.0,
        shm_capable=True,
    )
    assert dest is not None
    assert EngineManifest.read(dest / "manifest.json").shm_capable is True


def test_passing_receipt_helper_matches_real_build_ids(tmp_path):
    # The shared helper computes build-ids from the workspace, so it always matches on-disk.
    ws = _ws(tmp_path)
    rc = passing_receipt(ws, ["1"], scale_factors=(1.0,))
    verify_receipt_for_publish(
        rc, workspace=ws, published_query_ids=["1"], scale_factor=1.0
    )


# --------------------------------------------------------------------------- #
# RunTool.validate_for_publish: receipt assembly (without compiling a real engine)
# --------------------------------------------------------------------------- #
import types


class _Entry:
    def __init__(self, query_id, placeholders):
        self.query_id = query_id
        self.placeholders = placeholders


class _Batch:
    def __init__(self, sf, entries):
        self.exec_settings = types.SimpleNamespace(scale_factor=sf)
        self.query_list = entries


class _Provider:
    def produce_workload(self, run_mode, num_threads, core_ids, query_ids):
        # Two scale factors; query 1 with two distinct bindings (one duplicated), query 6 constant.
        return [
            _Batch(
                1.0,
                [
                    _Entry("1", {"DELTA": "90"}),
                    _Entry("1", {"DELTA": "90"}),
                    _Entry("1", {"DELTA": "60"}),
                    _Entry("6", {}),
                ],
            ),
            _Batch(10.0, [_Entry("1", {"DELTA": "90"}), _Entry("6", {})]),
        ]


def _make_run_tool(ws, success):
    from synnodb.tools.run import RunTool

    rt = object.__new__(
        RunTool
    )  # bypass __init__: validate_for_publish needs only a few attrs
    rt.cwd = ws
    rt.dataset_name = "tpch"
    rt.workload_provider = _Provider()
    rt.query_validator = types.SimpleNamespace(
        git_snapshotter=types.SimpleNamespace(current_hash="snap-abc")
    )
    rt.run_worker = lambda **kw: types.SimpleNamespace(success=success)  # type: ignore[assignment]
    return rt


def test_validate_for_publish_builds_a_pass_receipt(tmp_path):
    ws = _ws(tmp_path)
    receipt = _make_run_tool(ws, success=True).validate_for_publish(["1", "6"])

    assert receipt.live_run is True and receipt.verdict == PASS
    assert receipt.snapshot_id == "snap-abc"
    assert receipt.data_planes == (PLANE_PARQUET,)  # generation validates parquet only
    assert set(receipt.validated_scale_factors) == {1.0, 10.0}
    by_qid = {vq.query_id: vq.bindings for vq in receipt.validated_queries}
    assert set(by_qid) == {"1", "6"}
    assert by_qid["1"] == (
        {"DELTA": "90"},
        {"DELTA": "60"},
    )  # deduplicated, order preserved
    assert by_qid["6"] == ({},)  # constant query: one empty binding


def test_validate_for_publish_marks_a_failed_run(tmp_path):
    ws = _ws(tmp_path)
    receipt = _make_run_tool(ws, success=False).validate_for_publish(["1", "6"])
    assert receipt.verdict == FAIL and receipt.live_run is True


def test_validate_for_publish_refuses_when_validation_is_off(tmp_path):
    """A receipt must prove a correctness check. A run with validation disabled reports success
    after merely executing the binary (no answer comparison), so minting a receipt from it must be
    refused outright - otherwise a wrong-answer engine could publish under a green 'pass'."""
    ws = _ws(tmp_path)
    rt = _make_run_tool(ws, success=True)
    rt.parse_out_and_validate_output = False
    with pytest.raises(RuntimeError, match="answer validation is not"):
        rt.validate_for_publish(["1"])

    rt2 = _make_run_tool(ws, success=True)
    rt2.query_validator = None
    with pytest.raises(RuntimeError, match="answer validation is not"):
        rt2.validate_for_publish(["1"])
