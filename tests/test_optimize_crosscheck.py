"""The optimizer publish gate: cross-check the built engine against the source DuckDB.

``optimize.py`` has no agent-loop validator, but it holds the source database in memory (the
``inner`` DuckDB). The gate runs every publish query through the runtime engine path and compares
its Arrow output to DuckDB's - exactly the router's live cross-check - and refuses to publish on any
divergence or execution failure. These tests drive ``_validate_engine_against_source`` with a fake
engine over a real in-memory DuckDB, so the cross-check wiring is covered without compiling C++.
"""
from __future__ import annotations

import duckdb
import pyarrow as pa
import pytest

from synnodb.optimize import _validate_engine_against_source
from synnodb.workloads.engine_publish import _lookup_template
from synnodb.workloads.query_params import substitute
from synnodb.workloads.validation_receipt import PASS, PLANE_PARQUET


SQL = {
    "Q1": "select x from t where x >= [LO] order by x",
    "Q7": "select count(*) as n from t",  # constant query, no placeholders
}


class _Provider:
    """Minimal workload provider stub: a sql_dict and a deterministic placeholder generator, the
    two surfaces ``_sample_assignments`` / ``_lookup_template`` use."""

    sql_dict = SQL

    def _get_query_gen_fn(self):
        def gen(query_name, rnd):
            return None, None, ({"LO": "2"} if query_name == "Q1" else {})
        return gen


class _FakeEngine:
    """Stand-in for ProcessEngine/ShmHotLoadEngine. A "correct" engine reproduces DuckDB's answer
    by running the substituted SQL on the same data; a "broken" one returns a wrong table."""

    def __init__(self, inner, mode):
        self._inner = inner
        self._mode = mode
        self.ran: list = []

    def ingest(self, tables):  # only exercised on the shm plane
        pass

    def run(self, query_id, placeholders) -> pa.Table:
        self.ran.append((query_id, dict(placeholders)))
        bracket = _lookup_template(SQL, str(query_id))
        concrete = substitute(bracket, placeholders)
        if self._mode == "broken":
            return pa.table({"wrong": [-1]})
        return self._inner.execute(concrete).to_arrow_table()

    def close(self):
        pass


def _inner():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1),(2),(3),(4)) AS v(x)")
    return con


def _patch_engines(monkeypatch, inner, mode):
    import synnodb.router.process_engine as pe

    factory = lambda *a, **k: _FakeEngine(inner, mode)  # noqa: E731
    monkeypatch.setattr(pe, "ProcessEngine", factory)
    monkeypatch.setattr(pe, "ShmHotLoadEngine", factory)


def test_crosscheck_passes_for_a_correct_engine(tmp_path, monkeypatch):
    inner = _inner()
    _patch_engines(monkeypatch, inner, "correct")
    receipt = _validate_engine_against_source(
        tmp_path, _Provider(), ["1", "7"], inner,
        planes=[PLANE_PARQUET], bundle=tmp_path / "bundle",
        expected_tables={"t": ()}, dataset="tpch", scale_factor=1.0,
    )
    assert receipt.verdict == PASS and receipt.live_run is True
    assert receipt.data_planes == (PLANE_PARQUET,)
    assert {vq.query_id for vq in receipt.validated_queries} == {"1", "7"}
    assert receipt.validated_scale_factors == (1.0,)


def test_crosscheck_refuses_a_diverging_engine(tmp_path, monkeypatch):
    inner = _inner()
    _patch_engines(monkeypatch, inner, "broken")
    with pytest.raises(RuntimeError, match="does not match the source database"):
        _validate_engine_against_source(
            tmp_path, _Provider(), ["1"], inner,
            planes=[PLANE_PARQUET], bundle=tmp_path / "bundle",
            expected_tables={"t": ()}, dataset="tpch", scale_factor=1.0,
        )


def test_crosscheck_covers_the_shm_plane_when_requested(tmp_path, monkeypatch):
    inner = _inner()
    _patch_engines(monkeypatch, inner, "correct")
    receipt = _validate_engine_against_source(
        tmp_path, _Provider(), ["7"], inner,
        planes=["parquet", "shm"], bundle=tmp_path / "bundle",
        expected_tables={"t": ()}, dataset="tpch", scale_factor=None,
    )
    assert receipt.verdict == PASS
    assert set(receipt.data_planes) == {"parquet", "shm"}
    assert receipt.validated_scale_factors == ()  # no scale factor passed
