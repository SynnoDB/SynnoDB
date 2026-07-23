"""Coverage guard: every TPC-H query must derive a routable template and bind concrete
instantiations back to the exact parameter values.

This is the end-to-end check for the router's template layer against the real workload: each
query's ``[NAME]`` template plus the authoritative generator's sample values must (1) self-validate
into a :class:`QueryTemplate` and (2) recover the generator's values from a freshly instantiated
concrete query. It pins the shapes that used to fall back to DuckDB - the LIKE affixes (Q2/Q9/Q16),
the two-word ``LIKE '%[W1]%[W2]%'`` (Q13), the CTE with generator metadata (Q15), and the parameter
IN-lists (Q22) - so a regression in any of them fails here loudly.
"""

from __future__ import annotations

import random

import pytest
from tutorials.workloads.tpch.gen_tpch_query import gen_query

from synnodb.router.guards import GuardContext, placeholder_arity_guard
from synnodb.router.normalize import bind_template, binding_groups, normalize_sql
from synnodb.router.registry import EngineBinding
from synnodb.workloads.engine_publish import build_query_templates
from synnodb.workloads.query_params import substitute
from tutorials.workloads.tpch.tpch_queries import tpc_h

QIDS = [str(k[1:]) for k in tpc_h if k.startswith("Q")]


def _samples(qid: str, n: int, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    out: list[dict] = []
    for _ in range(n):
        try:
            _, _, ph = gen_query(query_name=f"Q{qid}", rnd=rnd)
        except Exception:
            break
        if ph:
            out.append(dict(ph))
    return out


@pytest.fixture(scope="module")
def templates() -> dict:
    tb = {qid: tpc_h[f"Q{qid}"] for qid in QIDS}
    ab = {qid: _samples(qid, 3, seed=0) for qid in QIDS}
    return {t.query_id: t for t in build_query_templates(tb, ab)}


def test_all_22_queries_derive(templates):
    assert sorted(int(q) for q in templates) == list(range(1, 23))


@pytest.mark.parametrize("qid", QIDS)
def test_parameterized_call_passes_arity_guard(qid, templates):
    # A prepared-statement caller supplies one value per `?` in the template. The arity guard
    # must accept exactly that count for every real workload shape - including Q13, whose
    # single `?` carries two engine parameters packed in one literal.
    template = templates[qid]
    binding = EngineBinding(
        template_id=f"eng::{qid}",
        normalized_sql=normalize_sql(template.sql_template),
        query_id=qid,
        engine_id="eng",
        placeholders=template.placeholders,
        output_schema=(),
        tables=frozenset(),
        schema_fingerprint="fp",
        template_sql=template.sql_template,
    )
    values = ["v"] * len(binding_groups(template.placeholders))
    ok, detail = placeholder_arity_guard(
        GuardContext(
            sql=template.sql_template,
            binding=binding,
            conn=None,
            registry=None,
            parameters=values,
        )
    )
    assert ok, f"Q{qid}: {detail}"


@pytest.mark.parametrize("qid", QIDS)
def test_query_binds_exact_values(qid, templates):
    template = templates[qid]
    names = {p.name for p in template.placeholders}
    for assignment in _samples(qid, 20, seed=999):
        concrete = substitute(tpc_h[f"Q{qid}"], assignment)
        # The concrete instantiation must still resolve to the stored structural key.
        assert normalize_sql(concrete) == normalize_sql(template.sql_template)
        if not template.placeholders:
            continue
        bound = bind_template(template.sql_template, concrete, template.placeholders)
        want = {k: str(v) for k, v in assignment.items() if k in names}
        got = {k: str(v) for k, v in (bound or {}).items()}
        assert got == want, f"Q{qid}: recovered {got}, expected {want}"
