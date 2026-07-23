"""One-shot script to (re)write the tutorial's self-describing ``stack_queries.json``.

The Stack (Cardinality Estimation Benchmark) queries are not a simple declarative parameter
space: each of the 16 classes shares one join skeleton and differs only in its filter literals,
drawn from the *real* StackExchange value distributions recorded by the template-extraction
pipeline in ``gen_stack`` (``stack_templates.json``). ``build_stack_queries_json`` turns those
into the templated bring-your-own shape ``sync_from_duckdb`` consumes: one fixed,
filter-literal-only template per id whose placeholders are bound - as a ``tuples`` parameter
group - to the real recorded literal rows, so every sampled instantiation is a query that
actually occurred with its predicates correlated exactly as logged.
"""

import json
from pathlib import Path

from synnodb.workloads.dataset.gen_stack.gen_stack_query import build_stack_queries_json

TUTORIAL_DIR = Path(
    __file__
).parent.parent  # tutorials/, where the demo reads stack_queries.json


def build() -> dict:
    return build_stack_queries_json()


if __name__ == "__main__":
    data = build()
    out = TUTORIAL_DIR / "stack_queries.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Written: {out} ({len(data)} query classes)")
