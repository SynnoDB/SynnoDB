"""Parity guard: the declarative TPC-H value-space table (``TPCH_PARAM_SPECS``, which seeds
the tutorial ``queries.json``) must mimic the authoritative imperative generator
(``gen_tpch.gen_tpch_query.gen_query``) for non-date placeholders - same per-placeholder
reachable values *and* the same joint distinctness/correlation. Date specs intentionally expose
only an input constraint (closed ISO min/max range), not the generator's month/year snapping.
"""

from __future__ import annotations

import datetime
import random
from collections import defaultdict

import pytest

from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query
from synnodb.workloads.dataset.gen_tpch.tpch_param_specs import TPCH_PARAM_SPECS
from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h
from synnodb.workloads.query_params import find_placeholders, parse_param_space

# Enough draws to cover the largest domain (Q8 TYPE = 150 combos) many times over.
N = 20000


def _space(qid: str):
    section = TPCH_PARAM_SPECS[qid]
    return parse_param_space(
        section.get("params"), section.get("param_groups"), tpc_h[f"Q{qid}"]
    )


def _date_bounds(qid: str, placeholder: str):
    spec = TPCH_PARAM_SPECS[qid].get("params", {}).get(placeholder)
    if spec and spec.get("type") == "date":
        return (
            datetime.date.fromisoformat(spec["min"]),
            datetime.date.fromisoformat(spec["max"]),
        )
    return None


@pytest.mark.parametrize("k", range(1, 23))
def test_reachable_values_match_generator(k):
    qn = f"Q{k}"
    phs = find_placeholders(tpc_h[qn])

    gen_sets: dict[str, set] = defaultdict(set)
    rg = random.Random(7)
    for _ in range(N):
        _, _, ph = gen_query(query_name=qn, rnd=rg)
        for p in phs:
            gen_sets[p].add(ph[p])

    space = _space(str(k))
    my_sets: dict[str, set] = defaultdict(set)
    rm = random.Random(11)
    for _ in range(N):
        a = space.sample(rm)
        for p in phs:
            my_sets[p].add(a[p])

    for p in phs:
        bounds = _date_bounds(str(k), p)
        if bounds:
            lo, hi = bounds
            assert all(lo <= datetime.date.fromisoformat(v) <= hi for v in my_sets[p])
            continue
        assert my_sets[p] == gen_sets[p], (
            f"{qn} placeholder {p}: spec/generator value sets differ "
            f"(generator-only={sorted(gen_sets[p] - my_sets[p])[:5]}, "
            f"spec-only={sorted(my_sets[p] - gen_sets[p])[:5]})"
        )


# Joint structure: groups that the generator samples without replacement must always be
# distinct; placeholders it draws independently (Q19 brands) must collide at the same rate.
@pytest.mark.parametrize(
    "k,keys,always_distinct",
    [
        (7, ["NATION1", "NATION2"], True),
        (12, ["SHIPMODE1", "SHIPMODE2"], True),
        (16, [f"SIZE{i}" for i in range(1, 9)], True),
        (22, [f"I{i}" for i in range(1, 8)], True),
        (19, ["BRAND1", "BRAND2", "BRAND3"], False),
    ],
)
def test_joint_distinctness_matches_generator(k, keys, always_distinct):
    def frac_distinct(draw_fn):
        rnd_draws = [draw_fn() for _ in range(N)]
        return sum(len({d[key] for key in keys}) == len(keys) for d in rnd_draws) / N

    rg = random.Random(3)
    gen_frac = frac_distinct(lambda: gen_query(query_name=f"Q{k}", rnd=rg)[2])

    space = _space(str(k))
    rm = random.Random(4)
    my_frac = frac_distinct(lambda: space.sample(rm))

    if always_distinct:
        assert gen_frac == 1.0 and my_frac == 1.0
    else:
        # independent draws collide sometimes; the rates must agree closely
        assert gen_frac < 1.0
        assert abs(gen_frac - my_frac) < 0.03
