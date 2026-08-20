"""Template-corpus coverage for the five tutorial workloads.

Every catalog query must survive the production publish path end to end: it derives a
router template (zero skips), fresh fixed-seed instantiations from the workload's own
generator bind back through the template, and the template describes cleanly against a
zero-row schema (``tests/fixtures/workload_schemas.py``).

Sources mirror the demos: tpch/clickbench/ceb use their checked-in queries JSON;
musicbrainz and stack build theirs with the tutorials' own builders. The stack builder
distills its templates from the gitignored ``so_queries/`` download cache, so that test
skips when the cache is absent.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import duckdb
import pytest

from synnodb.router.normalize import bind_template
from synnodb.router.registration import describe_output
from synnodb.workloads.byo_workload import register_workload_from_json
from synnodb.workloads.engine_publish import (
    _binds_match,
    _distinct_names,
    _lookup_template,
    _sample_assignments,
    build_query_templates,
)
from tutorials.workloads.music_brainz._gen_musicbrainz_queries import (
    build_musicbrainz_queries_json,
)
from tutorials.workloads.stack import extract_templates
from tutorials.workloads.stack.gen_stack_query import build_stack_queries_json

from fixtures.workload_schemas import schema_statements

REPO_ROOT = Path(__file__).resolve().parent.parent

# Full catalog sizes - 74 queries across the five workloads, none may skip.
EXPECTED_COUNTS = {
    "tpch": 22,
    "clickbench": 10,
    "ceb": 16,
    "musicbrainz": 10,
    "stack": 16,
}

# Matches publish_from_provider's default sample count per query.
NUM_SAMPLES = 3
# Distinct from _sample_assignments' seed, so binding is checked on instantiations the
# derivation's self-validation never saw.
ROUND_TRIP_SEED = 1


class _SpecProvider:
    """The provider surface publish_from_provider reads: the workload's sql_dict plus its
    query generator (bring-your-own factories ignore the provider argument)."""

    def __init__(self, spec):
        self.spec = spec
        self.sql_dict = spec.sql_dict()

    def _get_query_gen_fn(self):
        return self.spec.query_gen_factory(self)


def _queries_json(workload: str, tmp_path: Path) -> Path:
    """The workload's ``queries.json``, exactly as the demos source or build it."""
    if workload == "tpch":
        return REPO_ROOT / "tutorials/workloads/tpch/tpch_queries.json"
    if workload == "clickbench":
        return REPO_ROOT / "tutorials/clickbench_queries.json"
    if workload == "ceb":
        return REPO_ROOT / "tutorials/workloads/ceb/ceb_queries.json"
    if workload == "musicbrainz":
        queries = build_musicbrainz_queries_json(num_instances=100, seed=42)
    else:
        so_queries = REPO_ROOT / "tutorials/workloads/stack/so_queries"
        if not any(so_queries.glob("q*/*.sql")):
            pytest.skip(
                "stack so_queries cache absent (gitignored); fetch it once with "
                "tutorials.workloads.stack.extract_templates.ensure_so_queries()"
            )
        queries = build_stack_queries_json(
            templates=extract_templates.build_templates(download=False)
        )
    path = tmp_path / f"{workload}_queries.json"
    path.write_text(json.dumps(queries))
    return path


def _tables_of(schema_stmts: list[str]) -> list[str]:
    con = duckdb.connect()
    try:
        for stmt in schema_stmts:
            con.execute(stmt)
        rows = con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


@pytest.mark.parametrize("workload", list(EXPECTED_COUNTS))
def test_workload_templates(workload, tmp_path):
    schema_stmts = schema_statements(workload)
    # An empty parquet root: tables are explicit, so registration never touches data.
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()
    spec = register_workload_from_json(
        f"templates_{workload}",
        _queries_json(workload, tmp_path),
        parquet_dir,
        tables=_tables_of(schema_stmts),
    )
    provider = _SpecProvider(spec)

    templates_by_qid: dict[str, str] = {}
    assignments_by_qid: dict[str, list] = {}
    for qid in spec.all_query_ids:
        qid = str(qid)
        bracket = _lookup_template(provider.sql_dict, qid)
        assert bracket, f"{workload} Q{qid}: no template in the workload's sql_dict"
        templates_by_qid[qid] = bracket
        assignments_by_qid[qid] = _sample_assignments(provider, qid, NUM_SAMPLES)

    derived = build_query_templates(templates_by_qid, assignments_by_qid)

    # 1. Every catalog query derives a validated template - zero skips.
    assert len(spec.all_query_ids) == EXPECTED_COUNTS[workload]
    assert [t.query_id for t in derived] == [str(q) for q in spec.all_query_ids]

    # 2. Fresh fixed-seed instantiations bind back through the derived template.
    gen = provider._get_query_gen_fn()
    for qt in derived:
        names = set(_distinct_names(templates_by_qid[qt.query_id]))
        rnd = random.Random(ROUND_TRIP_SEED)
        for _ in range(NUM_SAMPLES):
            _, concrete, assignment = gen(query_name=f"Q{qt.query_id}", rnd=rnd)
            # Keep only the template's holes (derive_template drops incidental
            # generator keys the same way).
            assignment = {k: v for k, v in assignment.items() if k in names}
            bound = bind_template(qt.sql_template, concrete, qt.placeholders)
            assert bound is not None, (
                f"{workload} Q{qt.query_id}: template did not bind {concrete!r}"
            )
            assert _binds_match(bound, assignment), (
                f"{workload} Q{qt.query_id}: bound {bound!r} != sampled {assignment!r}"
            )

    # 3. Every derived template describes cleanly against the zero-row schema.
    con = duckdb.connect()
    try:
        for stmt in schema_stmts:
            con.execute(stmt)
        for qt in derived:
            output = describe_output(con, qt.sql_template, qt.placeholders)
            assert output, f"{workload} Q{qt.query_id}: empty output schema"
    finally:
        con.close()
