"""Generate a bring-your-own ``queries.json`` for the Stack (CE benchmark) workload.

The Stack workload ships as 16 query classes (``q1`` .. ``q16``), each a join-heavy
``count(...)`` over the StackExchange schema that shares one join skeleton and differs only in
its filter predicates. :mod:`extract_templates` distilled the raw ``so_queries/`` log into
``stack_templates.json``: per class a tokenized template whose varying positions became
``[NAME]`` placeholders - split into filter literals (``parameters``), the filtered *column*
(``column_name_parameters``) and the comparison *operator* (``operator_parameters``) - plus,
for every concrete query, the literal each placeholder took.

This module turns that into the templated bring-your-own shape SynnoDB consumes
(:mod:`synnodb.workloads.byo_workload`): a ``queries.json`` mapping each id to
``{"sql": <template>, "param_groups": [<tuples spec>]}``.

Only filter literals are allowed to vary - the template's structure (its columns and
operators) stays fixed. A few classes (``q2``/``q3``/``q8``/``q11``-``q16``) parameterized the
filtered column and/or the operator too; for those we automatically pick the class's dominant
column+operator instantiation, bake it into the template text, and keep only the queries that
used it, so every class collapses to a single filter-literal-only skeleton.

The surviving queries' literal bindings become one ``tuples`` parameter group per class, so a
run samples a whole real ``(site, tag, threshold, ...)`` binding at once (drawn with the run's
seeded RNG): every instantiation is a query that actually occurred, with its predicates
correlated exactly as recorded rather than recombined across queries.

The extracted bindings are SQL-ready literals (string values include their single quotes);
each entry is finally converted to the framework convention - quotes in the template, bare
values - by :func:`~synnodb.workloads.query_params.hoist_literal_quotes`.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from synnodb.workloads.query_params import hoist_literal_quotes, substitute

# Canonical Stack query ids, in benchmark order.
STACK_QUERY_IDS: Tuple[str, ...] = tuple(f"q{i}" for i in range(1, 17))

# A bring-your-own ``queries.json`` value: a templated entry, or a bare SQL string for a class
# with no varying literals (a static query).
QueryEntry = Union[str, dict]


def _dominant_instantiation(entry: dict) -> Tuple[Dict[str, str], List[str]]:
    """The class's most common ``(filtered-column + operator)`` assignment.

    Returns ``(fixed, keys)`` where ``keys`` are the column/operator placeholder names and
    ``fixed`` maps each to the value that appears in the most queries. Empty when the class only
    ever varied filter literals (nothing to bake).
    """
    keys = entry["column_name_parameters"] + entry["operator_parameters"]
    if not keys:
        return {}, keys
    combos = Counter(
        tuple(
            {**q["column_name_parameters"], **q["operator_parameters"]}[k] for k in keys
        )
        for q in entry["queries"]
    )
    best = combos.most_common(1)[0][0]
    return dict(zip(keys, best)), keys


def _filter_only(entry: dict) -> Tuple[str, List[str], List[List[str]]]:
    """Collapse a class to a fixed template plus the literal rows that fit it.

    Bakes the dominant column/operator choice into the template so its only remaining
    placeholders are filter literals, then keeps the queries that used that choice and records
    each one's literal binding as a row aligned to the returned placeholder order. Rows are
    de-duplicated (first occurrence wins) so an over-represented binding is not weighted up.
    """
    fixed, keys = _dominant_instantiation(entry)
    template = substitute(entry["template"], fixed).strip()
    names = list(entry["parameters"])

    rows: List[List[str]] = []
    seen = set()
    for q in entry["queries"]:
        merged = {**q["column_name_parameters"], **q["operator_parameters"]}
        if any(merged[k] != fixed[k] for k in keys):
            continue  # a non-dominant column/operator query: dropped
        row = tuple(q["parameters"][n] for n in names)
        if row in seen:
            continue
        seen.add(row)
        rows.append(list(row))
    return template, names, rows


def _build_entry(entry: dict) -> QueryEntry:
    """Turn one extracted class into a bring-your-own ``queries.json`` value."""
    template, names, rows = _filter_only(entry)
    if not names:
        # No varying literals: a static (parameterless) query - a bare SQL string.
        return template
    # Extracted string values carry their SQL quotes; move them into the template
    # (see hoist_literal_quotes).
    template, rows = hoist_literal_quotes(template, names, rows)
    return {
        "sql": template,
        "param_groups": [
            {"type": "tuples", "placeholders": names, "values": rows},
        ],
    }


def _extract_templates_module():
    """Import the sibling ``extract_templates`` regardless of how this package was imported.

    The relative import works whether the package is reached as ``workloads.stack`` (the demo,
    with ``tutorials/`` on ``sys.path``) or ``tutorials.workloads.stack``; the by-path fallback
    covers running ``gen_stack_query.py`` as a bare top-level script (no package context).
    """
    try:
        from . import extract_templates
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "stack_extract_templates", Path(__file__).with_name("extract_templates.py")
        )
        extract_templates = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extract_templates)
    return extract_templates


def build_stack_queries_json(
    query_ids: Tuple[str, ...] = STACK_QUERY_IDS,
    *,
    templates: Optional[Dict[str, dict]] = None,
) -> Dict[str, QueryEntry]:
    """Build the templated bring-your-own ``queries.json`` mapping for the Stack workload.

    Each class becomes ``{"sql": <filter-literal-only template>, "param_groups": [<tuples>]}``
    (or a bare SQL string when a class has no varying literals). The ``tuples`` group binds all
    of the class's filter-literal placeholders jointly to the real recorded literal rows, so
    SynnoDB samples a whole correlated binding per execution.

    Args:
        query_ids: the Stack query ids to emit (default: the full ``q1`` .. ``q16`` set).
        templates: the per-class extraction (the ``stack_templates.json`` shape). When ``None``
            (default) it is derived on demand from the raw ``so_queries/`` log, which
            :func:`extract_templates.build_templates` downloads + caches on first use.

    Returns an insertion-ordered ``{query_id: entry}`` mapping.
    """
    if templates is None:
        templates = _extract_templates_module().build_templates()
    return {qid: _build_entry(templates[qid]) for qid in query_ids}


def main() -> None:
    data = build_stack_queries_json()
    dest = Path(__file__).with_name("queries.json")
    dest.write_text(json.dumps(data, indent=2))
    print(f"Wrote {dest} ({len(data)} query classes).")


if __name__ == "__main__":
    main()
