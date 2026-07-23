#!/usr/bin/env python3
"""Instantiate the MusicBrainz query templates in ``templates.json``.

Each template is a SQL string with ``[NAME]`` placeholders plus a set of
generation rules describing how to draw a value for every placeholder. All
rules are self-contained - value pools and numeric bounds are hardcoded in
``templates.json``, so generation needs no access to the dataset:

  int_uniform    an integer drawn uniformly from ``[low, high]``
  int_offset     ``value(base) + uniform(low_offset, high_offset)`` - used for
                 the upper end of correlated ranges such as year intervals
  float_uniform  a float drawn uniformly from ``[low, high]``, rounded to
                 ``decimals`` places
  choice         a value drawn uniformly from a hardcoded ``values`` list
  choice_list  ``n_min..n_max`` distinct values from a hardcoded list,
               rendered as a parenthesized ``IN`` list

Values with ``quote: true`` are emitted as SQL string literals (with ``''``
escaping); parameter dictionaries store the literal exactly as it appears in
the instantiated query, matching the ``gen_stack`` template format.

The output JSON mirrors ``gen_stack/stack_templates.json``: per template class
the template text, parameter name lists, and one parameter dictionary per
generated query. Generation is deterministic for a given ``--seed``.

Passing ``--db`` validates every instance with ``EXPLAIN`` against a DuckDB
file; ``--execute`` additionally runs the first N instances of each class end
to end. Without ``--db`` no database is touched at all.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def draw(rule: dict, resolved: dict[str, int], rng: random.Random) -> tuple[str, int | None]:
    """Return ``(literal, numeric_value)`` for one placeholder.

    ``literal`` is the exact text substituted into the query. The numeric
    value is returned separately so ``int_offset`` rules can reference it;
    it is ``None`` for non-integer rules.
    """
    kind = rule["kind"]
    if kind == "int_uniform":
        v = rng.randint(rule["low"], rule["high"])
        return str(v), v
    if kind == "int_offset":
        base = resolved[rule["base"]]
        v = base + rng.randint(rule["low_offset"], rule["high_offset"])
        return str(v), v
    if kind == "float_uniform":
        return f"{rng.uniform(rule['low'], rule['high']):.{rule['decimals']}f}", None
    if kind == "choice":
        v = rng.choice(rule["values"])
        return sql_quote(v) if rule.get("quote") else str(v), None
    if kind == "choice_list":
        n = rng.randint(rule["n_min"], min(rule["n_max"], len(rule["values"])))
        values = rng.sample(rule["values"], n)
        rendered = (sql_quote(v) if rule.get("quote") else str(v) for v in values)
        return "(" + ", ".join(rendered) + ")", None
    raise ValueError(f"unknown rule kind: {kind}")


def instantiate(template: str, params: dict[str, str]) -> str:
    out = template
    for name, literal in params.items():
        out = out.replace(f"[{name}]", literal)
    return out


def generate_class(cls: str, spec: dict, rng: random.Random, num: int,
                   max_attempts_factor: int = 20) -> list[dict[str, str]]:
    """Generate ``num`` distinct parameter dictionaries for one template."""
    rules = spec["generation_rules"]
    # int_offset rules reference another placeholder's value, so resolve
    # base rules first; one level of dependency is all the rules need.
    order = sorted(spec["parameters"], key=lambda p: "base" in rules[p])

    seen: set[tuple] = set()
    instances: list[dict[str, str]] = []
    attempts = 0
    while len(instances) < num and attempts < num * max_attempts_factor:
        attempts += 1
        numeric: dict[str, int] = {}
        params: dict[str, str] = {}
        for name in order:
            literal, value = draw(rules[name], numeric, rng)
            params[name] = literal
            if value is not None:
                numeric[name] = value
        key = tuple(params[p] for p in spec["parameters"])
        if key in seen:
            continue
        seen.add(key)
        instances.append({p: params[p] for p in spec["parameters"]})
    if len(instances) < num:
        print(f"  {cls}: parameter space exhausted, generated "
              f"{len(instances)}/{num} distinct instances")
    return instances


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--templates", default=ROOT / "templates.json", type=Path)
    parser.add_argument("--out", default=ROOT / "musicbrainz_templates.json", type=Path)
    parser.add_argument("--num-instances", default=100, type=int,
                        help="queries to generate per template")
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--db", default=None,
                        help="DuckDB file to validate the generated SQL against")
    parser.add_argument("--execute", default=0, type=int, metavar="N",
                        help="with --db, additionally execute the first N instances per template")
    args = parser.parse_args()

    con = None
    if args.db:
        import duckdb
        con = duckdb.connect(args.db, read_only=True)

    templates = json.loads(args.templates.read_text())
    rng = random.Random(args.seed)

    result = {}
    for cls, spec in templates.items():
        instances = generate_class(cls, spec, rng, args.num_instances)

        queries = []
        for i, params in enumerate(instances, start=1):
            if con is not None:
                sql = instantiate(spec["template"], params)
                con.execute(f"explain {sql}")  # binder check: tables, columns, types
                if i <= args.execute:
                    con.execute(sql).fetchall()
            queries.append({
                "parameters": params,
                "column_name_parameters": {},
                "operator_parameters": {},
            })

        result[cls] = {
            "template": spec["template"],
            "parameters": spec["parameters"],
            "column_name_parameters": [],
            "operator_parameters": [],
            "num_queries": len(queries),
            "queries": queries,
        }
        print(f"{cls}: {len(queries)} queries "
              f"({len(spec['parameters'])} parameters: {', '.join(spec['parameters'])})")

    args.out.write_text(json.dumps(result, indent=2))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
