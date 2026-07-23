"""Golden guardrail for the example conversations and their prompt bytes.

Scope: this test covers only the example conversations - the stage-list
builders in ``synnodb.conversations.examples`` that back the built-in
ConversationPlans (storage_plan, base_impl, optim, add_mt, check_sf). It does
not exercise user-defined plans.

Every LLM/tool cache keys on prompt bytes: an accidental one-character prompt
change silently invalidates every cache. This test builds each example
conversation's stage list via its builder (with a fixed, synthetic
ConvContext), renders every ``get_prompt`` / ``get_prompt_with_tracing`` with
fixed arguments, and compares descriptors, ordering, markers, and prompt text
against committed fixture files.

Refactors of the conversation machinery must keep this test green with
byte-identical prompt text. Where a refactor legitimately changes structure
(e.g. stage numbering or item kinds), only the structural header lines of the
fixtures may change - never the text between ``--- prompt ---`` markers.

Regenerate fixtures with:  UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/test_example_conversations_golden.py
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.conversation_engine import BRANCH_ANCHOR_PROMPT
from synnodb.conversations.filenames import Filenames
from synnodb.conversations.stage_items import (
    AssertCorrect,
    DynamicStageConfig,
    MarkerItem,
    MeasureBaselines,
    PerQueryLoop,
    PromptStage,
)
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.utils import DataSource, DBStorage
from synnodb.workloads.workload_provider import ExecSettings, WorkloadId
from synnodb.workloads.workload_provider_olap import OLAPExecSettings
from synnodb.workloads.workload_spec import get_workload_spec

GOLDEN_DIR = Path(__file__).parent / "golden_example_conversations"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

# ---------------------------- fixed synthetic inputs -------------------------
QUERY_IDS = ["1", "6"]
MODEL = "test-provider/test-model"
FIXED_RT_MS = 1234.5
FIXED_TRACE = "SYNTHETIC TRACING DATA (fixed for golden test)"
SQL_DICT = {
    "Q1": "SELECT l_returnflag FROM lineitem WHERE l_shipdate <= DATE ':1';",
    "Q6": "SELECT sum(l_extendedprice) FROM lineitem WHERE l_quantity < :1;",
}
SAMPLE_ARGS = {"1": "('1998-09-02')", "6": "(24)"}
SCHEMA = "CREATE TABLE lineitem (l_orderkey BIGINT, l_quantity DECIMAL(15,2));"
WORKSPACE = Path("/golden/workspace")
PARQUET_DIR = Path("/golden/data/tpch_parquet")
HW_CONTEXT = "SYNTHETIC HARDWARE CONTEXT (fixed for golden test)"


@pytest.fixture(autouse=True)
def _pin_hardware_context(monkeypatch):
    """base_optimize_build embeds host hardware info; pin it for byte-stable fixtures."""
    import synnodb.conversations.prompts_gen as prompts_gen

    monkeypatch.setattr(
        prompts_gen, "_detect_hardware_context", lambda *a, **k: HW_CONTEXT
    )


# ------------------------------- fixed ConvContext ---------------------------
def _mock_run_tool() -> MagicMock:
    run_tool = MagicMock(name="run_tool")
    run_tool.cwd = WORKSPACE
    run_tool.memory_budget_mb = 16384

    ingest_result = SimpleNamespace(
        success=True,
        ingest_time_ms=12345.0,
        query_batch=SimpleNamespace(exec_settings=ExecSettings()),
    )
    exhaustive_result = SimpleNamespace(success=False)

    def _run_worker(mode, **_kwargs):
        return ingest_result if mode == RunToolMode.INGEST else exhaustive_result

    run_tool.run_worker.side_effect = _run_worker
    return run_tool


@pytest.fixture(autouse=True)
def _golden_subset_root(tmp_path_factory) -> Path:
    """The parquet root the golden ConvContext points at.

    The planner / storage-plan prompts list the data subsets `query_data` may read, and that list
    is read off the parquet root, so the goldens need a real one. Materialize the TPC-H ladder
    (sf1, sf2, sf20) as empty files: `available_subsets` only checks that each subset's files
    exist, and the rendered menu names subset *values*, never paths - so the golden bytes stay
    byte-stable across machines and runs."""
    root = tmp_path_factory.mktemp("golden_parquet_root")
    spec = get_workload_spec("tpch")
    for sf in (1, 2, 20):
        subset_dir = root / f"sf{sf}"
        subset_dir.mkdir()
        for path in spec.subset_files(subset_dir):
            path.touch()
    return root


def _make_ctx(db_storage: DBStorage, subset_root: Path) -> ConvContext:
    """A ConvContext with every lazy input pinned to a fixed synthetic value.

    The lazy caches are pre-seeded so the builders render deterministic prompt
    bytes without touching a workload provider or the query-execution cache."""
    provider = MagicMock(name="workload_provider")
    provider.dataset_schema = SCHEMA
    # The data-subset menu in the prompts comes off the provider, so give it a real spec and a
    # real (if empty) parquet root: the prompt then renders the subset list an actual TPC-H run
    # shows. A MagicMock spec would not do - its `fast_check_sfs` is truthy but iterates empty.
    provider.spec = get_workload_spec("tpch")
    provider.benchmark_sf = 20
    provider.base_parquet_dir = subset_root
    provider.prepare = lambda: None

    ctx = ConvContext(
        query_ids=QUERY_IDS,
        filenames=Filenames.for_usecase(),
        workspace_path=WORKSPACE,
        db_storage=db_storage,
        threads=1,
        model=MODEL,
        run_tool=_mock_run_tool(),
        workload_provider=provider,
        sql_dict=SQL_DICT,
        workload=WorkloadId("tpch"),
        bespoke_storage=True,
        max_turns=None,
    )
    # pre-seed the lazy caches with fixed values
    ctx._sample_query_args = SAMPLE_ARGS
    ctx._sample_exec_settings = OLAPExecSettings(
        scale_factor=20.0,
        db_storage=DBStorage.IN_MEMORY,
        parquet_dir=PARQUET_DIR / "sf20",
        disk_db_dir=None,
        data_source=DataSource.FLAT,
    )
    ctx._reference_plans = {
        ("umbra", True): {
            qid: f"SYNTHETIC REFERENCE PLAN for query {qid} (fixed for golden test)"
            for qid in QUERY_IDS
        }
    }
    # the MeasureBaselines item fills this at execution time; the MT tuning
    # prompts close over it, so pin fixed values for the fixture
    ctx.single_threaded_rt_ms = {qid: 4321.0 for qid in QUERY_IDS}
    return ctx


# ---------------------------------- rendering --------------------------------
def _render_prompt_block(text: str) -> list[str]:
    return ["--- prompt ---", text, "--- end prompt ---"]


def _render_static_stage(stage: PromptStage) -> list[str]:
    if stage.get_prompt is not None:
        prompt_source = "get_prompt"
        text = stage.get_prompt(stage.exec_settings, FIXED_RT_MS)
    else:
        prompt_source = "get_prompt_with_tracing"
        assert stage.get_prompt_with_tracing is not None
        text = stage.get_prompt_with_tracing(
            stage.exec_settings, FIXED_RT_MS, FIXED_TRACE
        )
    return [
        "kind: prompt-stage",
        f"descriptor: {stage.descriptor}",
        f"max_turns: {stage.max_turns}",
        f"measure_performance_after_stage: {stage.measure_performance_after_stage}",
        f"measure_perf_qid: {stage.measure_perf_qid}",
        f"auto_revert_on_regression: {stage.auto_revert_on_regression}",
        f"feedback_on_incorrect: {stage.feedback_on_incorrect}",
        f"throw_exception_on_incorrect: {stage.throw_exception_on_incorrect}",
        f"has_post_stage_validate: {stage.post_stage_validate is not None}",
        f"benchmark_sf: {stage.benchmark_sf}",
        f"prompt_source: {prompt_source}",
        *_render_prompt_block(text),
    ]


def _render_dynamic_stage(stage: DynamicStageConfig) -> list[str]:
    lines = [
        "kind: dynamic-stage",
        f"class: {type(stage).__name__}",
        f"descriptor: {stage.descriptor}",
        f"max_turns: {stage.max_turns}",
    ]
    for _ in range(10):  # safety cap
        prompt = stage.next_prompt()
        if prompt is None:
            break
        lines.extend(_render_prompt_block(prompt))
    return lines


def _render_entry(entry, ctx: ConvContext | None = None) -> list[str]:
    if isinstance(entry, MarkerItem):
        return [
            "kind: marker",
            f"marker: {entry.marker}",
            f"benchmark_sf: {entry.benchmark_sf}",
        ]
    if isinstance(entry, AssertCorrect):
        return ["kind: assert-correct", f"query_ids: {entry.query_ids}"]
    if isinstance(entry, MeasureBaselines):
        return ["kind: measure-baselines", f"into: {entry.into}"]
    if isinstance(entry, PerQueryLoop):
        lines = [
            "kind: per-query-loop",
            f"conversation_branching: {entry.conversation_branching}",
            f"end_of_ring_benchmark: {entry.end_of_ring_benchmark}",
            f"branch_anchor: {entry.branch_anchor}",
            f"benchmark_sf: {entry.benchmark_sf}",
        ]
        if entry.branch_anchor:
            lines.append(
                "--- branch anchor (descriptor: Branch Anchor, max_turns: 5) ---"
            )
            lines.extend(_render_prompt_block(BRANCH_ANCHOR_PROMPT))
        for qid in QUERY_IDS:
            for j, stage in enumerate(entry.build(qid, ctx)):
                lines.append(f"----- loop stage {j} (query {qid}) -----")
                lines.extend(_render_entry(stage, ctx))
        return lines
    if isinstance(entry, PromptStage):
        return _render_static_stage(entry)
    if isinstance(entry, DynamicStageConfig):
        return _render_dynamic_stage(entry)
    raise AssertionError(f"Unknown stage-list entry: {entry!r}")


def _render_doc(
    sections: list[tuple[str, list]], ctx: ConvContext | None = None
) -> str:
    lines: list[str] = []
    for section_name, entries in sections:
        lines.append(f"########## section: {section_name} ##########")
        for i, entry in enumerate(entries):
            lines.append(f"===== entry {i} =====")
            lines.extend(_render_entry(entry, ctx))
        lines.append("")
    return "\n".join(lines) + "\n"


def _assert_matches_golden(name: str, doc: str) -> None:
    path = GOLDEN_DIR / f"{name}.golden.txt"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc, encoding="utf-8")
        return
    assert path.exists(), (
        f"Golden fixture {path} missing. Generate it with UPDATE_GOLDEN=1."
    )
    expected = path.read_text(encoding="utf-8")
    assert doc == expected, (
        f"Rendered stage list for {name!r} differs from the golden fixture {path}.\n"
        "If prompt text changed, this invalidates LLM/tool caches - treat as a "
        "blocker unless the change is deliberate. For deliberate structural "
        "changes, regenerate with UPDATE_GOLDEN=1 and review the diff carefully."
    )


# ------------------- rendered documents, one per example conversation --------
def _storage_plan_doc(subset_root: Path) -> str:
    from synnodb.conversations.examples import storage_plan

    ctx = _make_ctx(DBStorage.IN_MEMORY, subset_root)
    return _render_doc([("stages", storage_plan.build(ctx))], ctx)


def _base_impl_doc(subset_root: Path) -> str:
    from synnodb.conversations.examples import base_impl

    ctx = _make_ctx(DBStorage.IN_MEMORY, subset_root)
    return _render_doc([("stages", base_impl.build(ctx))], ctx)


def _optim_round1_doc(db_storage: DBStorage, subset_root: Path) -> str:
    from synnodb.conversations.examples import optim

    ctx = _make_ctx(db_storage, subset_root)
    return _render_doc([("stages", optim.build(ctx, plan_source="umbra"))], ctx)


def _mt_round2_doc(db_storage: DBStorage, subset_root: Path) -> str:
    from synnodb.conversations.examples import add_mt

    ctx = _make_ctx(db_storage, subset_root)
    return _render_doc([("stages", add_mt.build(ctx))], ctx)


def _check_sf_doc(subset_root: Path) -> str:
    from synnodb.conversations.examples import check_sf

    ctx = _make_ctx(DBStorage.IN_MEMORY, subset_root)
    return _render_doc([("stages", check_sf.build(ctx, target_sf=100))], ctx)


# ------------------------------------ tests ----------------------------------
def test_golden_storage_plan(_golden_subset_root):
    _assert_matches_golden("storage_plan", _storage_plan_doc(_golden_subset_root))


def test_golden_base_impl(_golden_subset_root):
    _assert_matches_golden("base_impl", _base_impl_doc(_golden_subset_root))


def test_golden_in_mem_1_optim(_golden_subset_root):
    _assert_matches_golden(
        "in_mem_1_optim", _optim_round1_doc(DBStorage.IN_MEMORY, _golden_subset_root)
    )


def test_golden_ssd_1_st_optim(_golden_subset_root):
    _assert_matches_golden(
        "ssd_1_st_optim", _optim_round1_doc(DBStorage.SSD, _golden_subset_root)
    )


def test_golden_in_mem_2_mt(_golden_subset_root):
    _assert_matches_golden(
        "in_mem_2_mt", _mt_round2_doc(DBStorage.IN_MEMORY, _golden_subset_root)
    )


def test_golden_ssd_2_mt(_golden_subset_root):
    _assert_matches_golden(
        "ssd_2_mt", _mt_round2_doc(DBStorage.SSD, _golden_subset_root)
    )


def test_golden_check_sf(_golden_subset_root):
    _assert_matches_golden("check_sf", _check_sf_doc(_golden_subset_root))
