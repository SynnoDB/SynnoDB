"""CLI entrypoint for capture analysis."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..capture_files import load_records
from .repetition import (
    aggregate_query_entries,
    display_query_structure,
    find_repetitions,
    format_query_entry,
    rendered_values,
    repetition_reuse_score,
)


def _aggregate_rendered_groups(rendered_groups: list[dict[str, object]]) -> list[dict[str, object]]:
    for group in rendered_groups:
        group["queries"] = aggregate_query_entries(group["queries"])
        group["reuse_score"] = repetition_reuse_score(group)
    rendered_groups.sort(
        key=lambda group: (
            -int(group["reuse_score"]),
            -int(group["count"]),
            -int(group["distinct_value_count"]),
            str(group["normalized_query"]),
        )
    )
    return rendered_groups


def analyze_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze pgrouter captures for repeated query structures.")
    parser.add_argument("--jsonl-path", default="queries.jsonl", help="path to queries.jsonl")
    parser.add_argument("--limit", type=int, default=20, help="maximum number of repetition groups to print")
    parser.add_argument("--min-count", type=int, default=2, help="minimum number of matching queries per group")
    parser.add_argument("--aggregate", action="store_true", help="collapse exact repeated occurrences inside each group")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of text")
    args = parser.parse_args(argv)

    jsonl_path = Path(args.jsonl_path)
    if not jsonl_path.exists():
        print(f"missing capture file: {jsonl_path}", file=sys.stderr)
        return 1

    repetitions = find_repetitions(load_records(jsonl_path), min_count=args.min_count)
    rendered_groups = [asdict(item) for item in repetitions]
    for group in rendered_groups:
        group["display_structure"] = display_query_structure(group)
    if args.aggregate:
        rendered_groups = _aggregate_rendered_groups(rendered_groups)
    rendered_groups = rendered_groups[: args.limit]

    if args.json:
        print(json.dumps(rendered_groups, ensure_ascii=True, indent=2))
        return 0
    if not repetitions:
        print("no repeated query structures found")
        return 0

    for index, repetition in enumerate(rendered_groups, start=1):
        if args.aggregate:
            print(
                f"group {index} | reuse_score={repetition['reuse_score']} | runs={repetition['count']} | "
                f"query_variants={repetition['distinct_query_count']} | value_variants={repetition['distinct_value_count']}"
            )
        else:
            print(
                f"group {index} | count={repetition['count']} | query_variants={repetition['distinct_query_count']} | "
                f"value_variants={repetition['distinct_value_count']}"
            )
        print(f"  structure: {repetition['display_structure']}")
        for entry in repetition["queries"]:
            if args.aggregate:
                values = rendered_values(entry.get("values", []))
                rendered_entry = f"values={values} | runs={entry['run_count']}"
                example_query = " ".join(str(entry.get("query") or "").split())
                if example_query and example_query != repetition["display_structure"]:
                    rendered_entry = f"{rendered_entry} | example={example_query}"
            else:
                rendered_entry = format_query_entry(entry)
            print(f"  {rendered_entry}")
    return 0


def main() -> None:
    raise SystemExit(analyze_main())
