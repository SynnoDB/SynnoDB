"""Knowing before generation which queries can never be bespoke-served.

The exact-egress vocabulary is enforced at registration - which happens *after* an engine
has been generated, validated and published. A query whose output type is outside that
vocabulary would therefore cost a full generation and then always fall back to DuckDB.
``unroutable_queries`` runs the same check up front, against the live schema.

Run: .venv/bin/python -m pytest tests/test_routability_preflight.py -q
"""

from __future__ import annotations

import duckdb
import pytest

from synnodb.router.registration import unroutable_queries


@pytest.fixture
def con():
    con = duckdb.connect()
    con.execute("CREATE TABLE t(id INTEGER, doc JSON, ts TIMESTAMPTZ, tags INTEGER[])")
    yield con
    con.close()


def test_flags_only_what_cannot_be_served(con):
    bad = dict(
        unroutable_queries(
            con,
            {
                "ok_scalar": "SELECT count(*) AS n FROM t",
                "ok_timestamptz": "SELECT ts FROM t",  # zone-bearing, now supported
                "bad_json": "SELECT doc FROM t",  # JSON output: not reproducible
                "bad_list": "SELECT tags FROM t",
            },
        )
    )
    assert set(bad) == {"bad_json", "bad_list"}
    assert "JSON" in bad["bad_json"]
    assert "nested/array" in bad["bad_list"]


def test_reports_a_query_duckdb_cannot_even_describe(con):
    bad = dict(unroutable_queries(con, {"typo": "SELECT * FROM no_such_table"}))
    assert "typo" in bad and "could not describe" in bad["typo"]


def test_empty_when_every_query_is_servable(con):
    assert (
        unroutable_queries(con, {"a": "SELECT id FROM t", "b": "SELECT ts FROM t"})
        == []
    )


def _spec_with(tmp_path, queries):
    """A bring-your-own workload over the fixture schema, as sync_from_duckdb builds one."""
    import json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = tmp_path / "pq"
    parquet.mkdir(exist_ok=True)
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(queries))
    return register_workload_from_json("prune_probe", path, parquet, tables=["t"])


def test_unroutable_queries_are_pruned_from_the_workload(con, tmp_path):
    """Generating an engine for a query that can never be served is wasted work, so the
    workload is narrowed at registration - and the narrowing is what the pipeline sees."""
    from synnodb.api import _prune_unroutable
    from synnodb.workloads.workload_spec import get_workload_spec

    spec = _spec_with(
        tmp_path,
        {
            "1": "SELECT count(*) AS n FROM t",
            "2": "SELECT doc FROM t",  # JSON output: never servable
            "3": "SELECT ts FROM t",
        },
    )
    assert len(spec.all_query_ids) == 3

    pruned = _prune_unroutable(con, spec)
    assert set(pruned.all_query_ids) == {"1", "3"}
    # Re-registered under the same name, so generation sees the narrowed catalog too.
    assert set(get_workload_spec("prune_probe").all_query_ids) == {"1", "3"}


def test_a_wholly_unservable_workload_raises_instead_of_pruning_to_nothing(
    con, tmp_path
):
    from synnodb.api import _prune_unroutable
    from synnodb.errors import SynnoUnsupportedQuery

    spec = _spec_with(
        tmp_path,
        {"1": "SELECT doc FROM t", "2": "SELECT tags FROM t"},
    )
    with pytest.raises(SynnoUnsupportedQuery) as excinfo:
        _prune_unroutable(con, spec)
    assert "JSON" in str(excinfo.value)


def test_a_fully_servable_workload_is_left_alone(con, tmp_path):
    from synnodb.api import _prune_unroutable

    spec = _spec_with(tmp_path, {"1": "SELECT id FROM t", "2": "SELECT ts FROM t"})
    assert _prune_unroutable(con, spec) is spec
