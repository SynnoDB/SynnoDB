"""Inspect captured queries and result files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .capture_files import load_records, load_result_rows, resolve_result_path


def iter_display_records(records: Iterable[dict[str, Any]], query_id: str | None, limit: int) -> list[dict[str, Any]]:
    selected = [record for record in records if query_id is None or record.get("query_id") == query_id]
    if query_id is None:
        return selected[:limit]
    return selected


def format_record_summary(record: dict[str, Any]) -> str:
    return " | ".join(
        [
            record.get("query_id", "-"),
            f"rows={record.get('row_count', 0)}",
            f"dur={record.get('duration_ms', 0)}ms",
            f"cmd={record.get('command') or '-'}",
            record.get("query") or "",
        ]
    )


def print_result_rows(
    jsonl_path: Path,
    result_file: str | None,
    show_rows: int,
    indent: str = "  ",
    *,
    show_missing: bool = True,
) -> None:
    result_path = resolve_result_path(jsonl_path, result_file)
    if result_path is None:
        if show_missing:
            print(f"{indent}no result file")
        return
    rows = load_result_rows(result_path)
    for row in rows[:show_rows]:
        print(f"{indent}{json.dumps(row, ensure_ascii=True)}")
    if len(rows) > show_rows:
        print(f"{indent}... {len(rows) - show_rows} more row(s)")


def inspect_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect pgrouter capture output.")
    parser.add_argument("--jsonl-path", default="queries.jsonl", help="path to queries.jsonl")
    parser.add_argument("--query-id", help="show only one query id")
    parser.add_argument("--limit", type=int, default=20, help="maximum number of queries to print")
    parser.add_argument("--show-rows", type=int, default=0, help="show up to N rows from each result file")
    args = parser.parse_args(argv)

    jsonl_path = Path(args.jsonl_path)
    if not jsonl_path.exists():
        print(f"missing capture file: {jsonl_path}", file=sys.stderr)
        return 1

    records = load_records(jsonl_path)
    selected = iter_display_records(records, args.query_id, args.limit)
    if not selected:
        print("no matching queries")
        return 0

    for record in selected:
        print(format_record_summary(record))
        if args.show_rows <= 0:
            continue
        if record.get("query_source") == "transaction":
            statements = record.get("statements") or []
            for index, statement in enumerate(statements, start=1):
                print(f"  statement {index} | cmd={statement.get('command') or '-'} | {statement.get('query') or ''}")
                print_result_rows(
                    jsonl_path,
                    statement.get("result_file"),
                    args.show_rows,
                    indent="    ",
                    show_missing=False,
                )
            continue
        print_result_rows(jsonl_path, record.get("result_file"), args.show_rows)
    return 0


def main() -> None:
    raise SystemExit(inspect_main())


if __name__ == "__main__":
    main()
