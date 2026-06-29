"""One-shot script to (re)write the tutorial's self-describing queries.json.

Each entry is ``{"sql": <template>, "params": {PLACEHOLDER: [values...]}}``. The values are
authentic TPC-H parameters produced by the built-in generator
(``gen_tpch.gen_tpch_query.gen_query``) - run N times per query and transposed into
per-placeholder lists, so correlated placeholders (Q7 nation pairs, Q19 brand/quantity
triples) stay index-aligned. This is exactly the shape a BI dashboard would populate.
"""
import json
import random
import re
from pathlib import Path

from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query
from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h

HERE = Path(__file__).parent
N = 6  # instantiations per query
_PH = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]")


def build() -> dict:
    out: dict[str, dict] = {}
    for k in range(1, 23):
        qn = f"Q{k}"
        sql = tpc_h[qn]
        placeholders = list(dict.fromkeys(_PH.findall(sql)))  # template order, deduped
        rng = random.Random(100 + k)
        instances = [gen_query(query_name=qn, rnd=rng)[2] for _ in range(N)]
        # transpose to per-placeholder value lists, keeping only the template's placeholders
        params = {ph: [inst[ph] for inst in instances] for ph in placeholders}
        entry: dict = {"sql": sql}
        if params:
            entry["params"] = params
        out[str(k)] = entry
    return out


if __name__ == "__main__":
    data = build()
    out = HERE / "queries.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Written: {out} ({len(data)} queries)")
