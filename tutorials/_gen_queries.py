"""One-shot script to (re)write the tutorial's self-describing queries.json.

Each entry is ``{"sql": <template>, "params": {PLACEHOLDER: <spec>}, "param_groups": [...]}``.
The specs are the authentic TPC-H parameter value spaces, taken verbatim from the declarative
``gen_tpch.tpch_param_specs.TPCH_PARAM_SPECS`` table (which mirrors the built-in generator's
ranges/choices). Scalar placeholders use typed ``int``/``float``/``date``/``categorical``
specs; correlated / distinct placeholders (Q7 nation pair, Q16/Q22 k-distinct, Q12 shipmodes)
use a joint ``param_groups`` spec. This is exactly the shape a BI dashboard would render as
sliders / dropdowns / date-pickers.
"""

import json
from pathlib import Path

from synnodb.workloads.dataset.gen_tpch.tpch_param_specs import TPCH_PARAM_SPECS
from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h

HERE = Path(__file__).parent


def build() -> dict:
    out: dict[str, dict] = {}
    for k in range(1, 23):
        qn = f"Q{k}"
        section = TPCH_PARAM_SPECS[str(k)]
        entry: dict = {"sql": tpc_h[qn]}
        if section.get("params"):
            entry["params"] = section["params"]
        if section.get("param_groups"):
            entry["param_groups"] = section["param_groups"]
        out[str(k)] = entry
    return out


if __name__ == "__main__":
    data = build()
    out = HERE / "queries.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Written: {out} ({len(data)} queries)")
