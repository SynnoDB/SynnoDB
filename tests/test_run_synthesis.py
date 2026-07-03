"""run_synthesis: the single entry point for executing a ConversationPlan.

Covers plan-name validation, the chain-token resolution matrix, the RunConfig
assembly + artifact stamping plumbing (with the execution backend mocked), and
that every built-in method resolves to a run_synthesis call with its predefined
plan - there is no second dispatch path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from synnodb.api import SynnoDB, _resolve_chain
from synnodb.builtin_plans import (
    base_impl_plan,
    check_sf_plan,
    mt_plan,
    optim_plan,
    storage_plan_plan,
)
from synnodb.conversations.stage_items import PromptStage
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    Parallelism,
    PrepareFeatures,
    write_prepare_metadata,
)
from synnodb.plan import ConversationPlan, SupervisionPolicy
from synnodb.results import RunResult, StageArtifact, StoragePlan
from synnodb.utils.utils import DBStorage


def _trivial_plan(**overrides) -> ConversationPlan:
    defaults = dict(
        name="trivialPlan",
        prepare=PrepareFeatures.base(),
        stages=lambda ctx: [
            PromptStage(
                descriptor="one stage",
                get_prompt=lambda _s, _rt: "PROMPT",
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
            )
        ],
    )
    defaults.update(overrides)
    return ConversationPlan(**defaults)


def _db(tmp_path, monkeypatch) -> SynnoDB:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    return SynnoDB(workspace="ws")


# ------------------------------ plan validation -------------------------------
def test_plan_name_must_be_identifier_ish():
    for bad in ["", "1abc", "has space", "a/b", "a.b"]:
        with pytest.raises(ValueError, match="Invalid plan name"):
            _trivial_plan(name=bad)
    for good in ["myTuningPass", "check_sf-v2", "A1"]:
        assert _trivial_plan(name=good).name == good


# --------------------------- chain-token resolution ----------------------------
def test_resolve_chain_matrix():
    artifact = StageArtifact("rid", None, None, snapshot_hash="deadbeef")
    # artifact -> its snapshot hash (W&B-free)
    assert _resolve_chain("s", artifact, None) == ("deadbeef", None)
    # raw hash string
    assert _resolve_chain("s", "cafe", None) == ("cafe", None)
    # W&B id path
    assert _resolve_chain("s", None, "run123") == (None, "run123")
    # artifact as wandb source -> its run id
    assert _resolve_chain("s", None, artifact) == (None, "rid")
    # neither / both -> error
    with pytest.raises(ValueError, match="neither"):
        _resolve_chain("s", None, None)
    with pytest.raises(ValueError, match="both"):
        _resolve_chain("s", "cafe", "run123")


def test_run_synthesis_rejects_artifact_without_snapshot(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    artifact = StageArtifact("rid", None, None, snapshot_hash=None)
    with pytest.raises(ValueError, match="snapshot_hash"):
        db.run_synthesis(_trivial_plan(), start=artifact)


# ------------------------- execution plumbing (mocked) -------------------------
def _fake_backend(tmp_path):
    """Patch run_conv_wrapper; record the (run_config, plan) it was handed and
    write a prepare record like a real run would."""
    calls = {}

    def _fake_run_conv_wrapper(run_config, plan):
        calls["run_config"] = run_config
        calls["plan"] = plan
        write_prepare_metadata(
            tmp_path / "ws",
            PrepareFeatures.optim().resolve(DBStorage.IN_MEMORY),
            parallelism=plan.parallelism,
        )
        return RunResult(run_id=None, snapshot_hash="newsnap")

    return calls, _fake_run_conv_wrapper


def test_run_synthesis_plumbs_plan_and_start(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    plan = _trivial_plan(offer_trace_option=True)
    calls, fake = _fake_backend(tmp_path)

    with patch("synnodb.main.run_conv_wrapper", side_effect=fake):
        artifact = db.run_synthesis(plan, start="cafebabe")

    assert calls["plan"] is plan
    rc = calls["run_config"]
    assert rc.start_snapshot == "cafebabe"
    assert rc.run_tool_offer_trace_option is True
    assert rc.query_list == "1"  # default queries="1"
    # the artifact mirrors the workspace prepare record
    assert artifact.snapshot_hash == "newsnap"
    assert artifact.prepare_features == PrepareFeatures.optim().resolve(
        DBStorage.IN_MEMORY
    )
    assert artifact.parallelism is Parallelism.SINGLE_THREADED


def test_run_synthesis_uses_artifact_snapshot_as_start(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    calls, fake = _fake_backend(tmp_path)
    source = StageArtifact("rid", None, None, snapshot_hash="feedface")
    with patch("synnodb.main.run_conv_wrapper", side_effect=fake):
        db.run_synthesis(_trivial_plan(), start=source)
    assert calls["run_config"].start_snapshot == "feedface"


# ------------------- built-ins are thin wrappers, no 2nd path ------------------
def test_builtin_methods_resolve_to_run_synthesis(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    seen: list[dict] = []

    def _spy(plan, *, start=None, storage_plan_snapshot=None, verbose=None):
        seen.append(
            dict(plan=plan, start=start, storage_plan_snapshot=storage_plan_snapshot)
        )
        return MagicMock(name="artifact")

    monkeypatch.setattr(db, "run_synthesis", _spy)

    db.createStoragePlan()
    db.createBaseImpl(storage_plan="PLAN TEXT")
    db.runOptimLoop("basehash")
    db.addMultiThreading("optimhash")
    db.checkSfCorrectness("mthash", target_sf=100.0)

    assert [c["plan"].name for c in seen] == [
        "createStoragePlan",
        "createBaseImpl",
        "runOptimLoop",
        "addMultiThreading",
        "checkSfCorrectness",
    ]
    assert [c["start"] for c in seen] == [
        None,
        None,
        "basehash",
        "optimhash",
        "mthash",
    ]
    # createBaseImpl's text path bakes the plan text into the prepare features
    base_call = seen[1]
    assert base_call["plan"].prepare.storage_plan_text == "PLAN TEXT"
    assert base_call["storage_plan_snapshot"] is None
    # checkSf bakes target_sf into the plan (stages + result builder), replay prepare
    check_call = seen[4]
    assert check_call["plan"].prepare is None


def test_run_optim_loop_bakes_plan_source_into_the_plan(tmp_path, monkeypatch):
    """The reference-plan source (umbra/duckdb) stays selectable per call and is
    baked into the constructed plan, not passed through the runner."""
    db = _db(tmp_path, monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        db, "run_synthesis", lambda plan, **kw: seen.append(plan) or MagicMock()
    )

    db.runOptimLoop("basehash")  # default source
    db.runOptimLoop("basehash", plan_source="duckdb")

    assert seen[0].stages.keywords == {"plan_source": "umbra"}
    assert seen[1].stages.keywords == {"plan_source": "duckdb"}
    assert optim_plan("duckdb").stages.keywords == {"plan_source": "duckdb"}


def test_builtin_plans_have_the_expected_shapes():
    assert storage_plan_plan().supervision == SupervisionPolicy.OFF
    assert storage_plan_plan().finish_interactive is False

    assert base_impl_plan().supervision == SupervisionPolicy.STRICT

    optim = optim_plan()
    assert optim.supervision == SupervisionPolicy.RELAXED
    assert optim.finish_interactive is True
    assert optim.offer_trace_option is True
    assert optim.prepare == PrepareFeatures.optim()

    mt = mt_plan()
    assert mt.parallelism is Parallelism.MULTI_THREADED
    assert mt.prepare == PrepareFeatures.mt()

    check = check_sf_plan(100)
    assert check.prepare is None  # replay the source snapshot's record
    assert check.finish_interactive is True


def test_check_sf_result_builder_carries_target_sf(tmp_path):
    report = check_sf_plan(42).result("rid", "snap", tmp_path, None)
    assert report.target_sf == 42.0


def test_create_base_impl_requires_exactly_one_source(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="neither"):
        db.createBaseImpl()
    plan_artifact = StoragePlan(
        "rid", tmp_path, None, tmp_path / "storage_plan.txt", "TEXT"
    )
    with pytest.raises(ValueError, match="both"):
        db.createBaseImpl(plan_artifact, storage_plan_wandb_id="run1")


# ------------------------ abort surfacing to the live UI -----------------------
def _minimal_run_config():
    """A stand-in RunConfig carrying only the attributes run_conv_wrapper reads
    before it enters the run coroutine (the setup seams are patched out)."""
    import types

    return types.SimpleNamespace(
        usecase=None,
        db_storage=DBStorage.IN_MEMORY,
        continue_run=False,
        benchmark="tpch",
        queries_str="1",
        model="model",
        bespoke_storage=False,
        sdk="openai",
        disable_openai_tracing=False,
        notify=False,
        verbose=False,
    )


@pytest.mark.parametrize(
    "raised, expected_message",
    [
        (KeyboardInterrupt(), "KeyboardInterrupt"),  # str() is empty -> type name
        (RuntimeError("boom"), "boom"),
    ],
)
def test_run_conv_wrapper_surfaces_abort_to_live_dashboard(
    raised, expected_message, tmp_path
):
    """A run aborting mid-flight must be reported to the live dashboard so the
    watcher sees a banner and a frozen timer instead of one ticking forever on a
    dead run. KeyboardInterrupt is a BaseException (not an Exception), so it would
    slip past a too-narrow ``except Exception`` handler entirely - this pins that
    both an ordinary error and a Ctrl-C are surfaced, and that the empty
    KeyboardInterrupt message degrades to the exception's type name."""
    from synnodb import main as main_mod

    reported: dict = {}

    def _capture(message, *, traceback_text=None, log_file=None):
        reported.update(message=message, traceback=traceback_text)

    def _abort(coro):
        coro.close()  # the run coroutine is never awaited in this path
        raise raised

    with (
        patch.object(main_mod, "_setup", lambda: None),
        patch.object(
            main_mod, "get_effective_db_storage", lambda u, d: DBStorage.IN_MEMORY
        ),
        patch.object(main_mod, "generate_conv_name", lambda **kw: ("conv", "conv_dt")),
        patch.object(main_mod, "setup_logging", lambda *a, **k: None),
        patch.object(main_mod.settings, "log_dir", lambda: tmp_path),
        patch.object(main_mod, "_run_coroutine", _abort),
        patch.object(main_mod, "report_live_dashboard_error", _capture),
    ):
        with pytest.raises(type(raised)):
            main_mod.run_conv_wrapper(
                run_config=_minimal_run_config(), plan=_trivial_plan()
            )

    assert reported["message"] == expected_message
    assert type(raised).__name__ in (reported["traceback"] or "")
