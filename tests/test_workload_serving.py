"""Serve-path coverage for the five tutorial workloads.

test_workload_templates proves every catalog query derives, binds, and describes; this
file proves each one is actually bespoke-served end to end through the real pipeline:
``register_engine`` on a zero-row schema, then fresh generator instantiations routed
through ``SynnoConnection.execute`` with 100% cross-checking. The engine oracle answers
with the routed statement itself, so a served query also proves the placeholder values
the router handed the engine (they must match the generator's assignment).

Run: .venv/bin/python -m pytest tests/test_workload_serving.py -q
"""

from __future__ import annotations

import random

import duckdb
import pytest

import synnodb
from synnodb.router import (
    LocalCallableEngine,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)
from synnodb.workloads.byo_workload import register_workload_from_json
from synnodb.workloads.engine_publish import (
    _binds_match,
    _distinct_names,
    _lookup_template,
    _sample_assignments,
    build_query_templates,
)

from fixtures.workload_schemas import schema_statements
from test_workload_templates import (
    EXPECTED_COUNTS,
    NUM_SAMPLES,
    _SpecProvider,
    _queries_json,
    _tables_of,
)

# Distinct from the derivation (0) and round-trip (1) seeds, so serving is checked on
# instantiations neither the template derivation nor the bind test ever saw.
SERVE_SEED = 2


@pytest.mark.parametrize("workload", list(EXPECTED_COUNTS))
def test_workload_serves(workload, tmp_path):
    schema_stmts = schema_statements(workload)
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()
    spec = register_workload_from_json(
        f"serving_{workload}",
        _queries_json(workload, tmp_path),
        parquet_dir,
        tables=_tables_of(schema_stmts),
    )
    provider = _SpecProvider(spec)

    templates_by_qid: dict[str, str] = {}
    assignments_by_qid: dict[str, list] = {}
    for qid in spec.all_query_ids:
        qid = str(qid)
        templates_by_qid[qid] = _lookup_template(provider.sql_dict, qid)
        assignments_by_qid[qid] = _sample_assignments(provider, qid, NUM_SAMPLES)
    derived = build_query_templates(templates_by_qid, assignments_by_qid)
    assert len(derived) == EXPECTED_COUNTS[workload]

    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    oracle = duckdb.connect()
    try:
        for stmt in schema_stmts:
            con.duckdb.execute(stmt)
            oracle.execute(stmt)

        # The oracle engine serves the very statement being routed (from a second DuckDB
        # with the same schema) and records the placeholder values the router bound.
        current: dict[str, str] = {}
        received: dict[str, list] = {}

        def serve_fn(qid):
            def fn(placeholders):
                received.setdefault(qid, []).append(dict(placeholders))
                return oracle.execute(current["sql"]).to_arrow_table()

            return fn

        for qt in derived:
            register_engine(
                con,
                template_sql=qt.sql_template,
                engine=LocalCallableEngine(
                    f"eng-{workload}", {qt.query_id: serve_fn(qt.query_id)}
                ),
                query_id=qt.query_id,
                placeholders=list(qt.placeholders),
            )

        gen = provider._get_query_gen_fn()
        served = 0
        for qt in derived:
            names = set(_distinct_names(templates_by_qid[qt.query_id]))
            rnd = random.Random(SERVE_SEED)
            for _ in range(NUM_SAMPLES):
                _, concrete, assignment = gen(query_name=f"Q{qt.query_id}", rnd=rnd)
                assignment = {k: v for k, v in assignment.items() if k in names}
                current["sql"] = concrete
                seen = len(received.get(qt.query_id, []))
                con.execute(concrete).fetchall()
                assert (con._last or {}).get("served_by") == "engine", (
                    f"{workload} Q{qt.query_id}: not bespoke-served: {concrete!r} "
                    f"({con.router_stats()['session']['fallback_reasons']})"
                )
                served += 1
                bound = received[qt.query_id][seen]
                assert _binds_match(bound, assignment), (
                    f"{workload} Q{qt.query_id}: engine got {bound!r}, "
                    f"generator used {assignment!r}"
                )

        session = con.router_stats()["session"]
        assert session["routed"] == served == EXPECTED_COUNTS[workload] * NUM_SAMPLES
        assert session["cross_checked"] == served
        assert session["cross_check_mismatch"] == 0
    finally:
        con.close()
        oracle.close()
