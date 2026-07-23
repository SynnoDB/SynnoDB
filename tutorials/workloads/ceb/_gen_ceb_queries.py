"""One-shot script to (re)write the tutorial's self-describing ``ceb_queries.json``.

The CEB / JOB (Cardinality Estimation Benchmark) queries are not simple declarative parameter
spaces like TPC-H: each template's placeholders (``IN ID1``, ``<= YEAR1``, ``ILIKE NAME`` ...)
are filled from the *real* IMDB value distributions recorded as per-query bindings under
``<CEB_DIR>/<id>/*.pkl``. So instead of a ``params``/``param_groups`` table this emits one fully
substituted, runnable query per id - a static bring-your-own workload, the exact shape
``sync_from_duckdb`` consumes and a real query log would produce.

``CEB_DIR`` defaults to the canonical bindings tree and can be overridden with ``SYNNO_CEB_DIR``.
"""

import json
import os
from pathlib import Path

from tutorials.workloads.ceb.gen_ceb_query import build_ceb_query_set

TUTORIAL_DIR = Path(
    __file__
).parent.parent  # tutorials/, where the demo reads ceb_queries.json

CEB_DIR = Path(
    os.environ.get("SYNNO_CEB_DIR", "/mnt/labstore/bespoke_olap/datasets/ceb/imdb")
)


def build() -> dict:
    # Each entry is a plain SQL string: the bring-your-own parser reads a bare string as a static
    # (parameterless) query, so no per-query param section is needed.
    return build_ceb_query_set(CEB_DIR)


if __name__ == "__main__":
    if not CEB_DIR.exists():
        raise SystemExit(
            f"CEB bindings directory not found: {CEB_DIR}\n"
            "Point SYNNO_CEB_DIR at a tree holding one <query-id>/ folder of recorded bindings."
        )
    data = build()
    out = TUTORIAL_DIR / "ceb_queries.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Written: {out} ({len(data)} queries)")
