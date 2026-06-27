"""Grouping logic for repeated query structures."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .normalization import extract_record_values, normalize_query_structure


@dataclass
class QueryRepetition:
    normalized_query: str
    count: int
    distinct_query_count: int
    distinct_value_count: int
    queries: list[dict[str, Any]]


@dataclass
class QueryOccurrence:
    query_id: str | None
    query: str
    values: list[Any]
    value_signature: str
    source: str | None
    duration_ms: float | None
    row_count: int | None


def value_signature(values: list[Any]) -> str:
    return json.dumps(values, ensure_ascii=True, sort_keys=True)


def query_occurrence(record: dict[str, Any]) -> QueryOccurrence | None:
    if record.get("query_source") == "transaction":
        return transaction_occurrence(record)
    query = record.get("query")
    if not query:
        return None
    values = extract_record_values(record)
    return QueryOccurrence(
        query_id=record.get("query_id"),
        query=query,
        values=values,
        value_signature=value_signature(values),
        source=record.get("query_source"),
        duration_ms=record.get("duration_ms"),
        row_count=record.get("row_count"),
    )


def transaction_occurrence(record: dict[str, Any]) -> QueryOccurrence | None:
    statements = record.get("statements") or []
    if not statements:
        return None
    statement_queries = [str(statement.get("query") or "").strip() for statement in statements if statement.get("query")]
    joined_query = " ; ".join(statement_queries)
    normalized_statements = normalized_transaction_statements(statements)
    if not normalized_statements:
        return None
    values: list[Any] = []
    for statement in statements:
        values.extend(extract_record_values(statement))
    return QueryOccurrence(
        query_id=record.get("query_id"),
        query=joined_query,
        values=values,
        value_signature=value_signature(values),
        source=record.get("query_source"),
        duration_ms=record.get("duration_ms"),
        row_count=record.get("row_count"),
    )


def find_repetitions(records: Iterable[dict[str, Any]], *, min_count: int = 2) -> list[QueryRepetition]:
    groups: dict[str, list[QueryOccurrence]] = {}
    for record in records:
        occurrence = query_occurrence(record)
        if occurrence is None:
            continue
        normalized_query = normalized_structure_for_record(record, occurrence)
        groups.setdefault(normalized_query, []).append(occurrence)

    repetitions: list[QueryRepetition] = []
    for normalized_query, group in groups.items():
        if len(group) < min_count:
            continue
        distinct_queries = {entry.query for entry in group}
        distinct_value_signatures = {entry.value_signature for entry in group}
        if len(distinct_queries) < 2 and len(distinct_value_signatures) < 2:
            continue
        repetitions.append(
            QueryRepetition(
                normalized_query=normalized_query,
                count=len(group),
                distinct_query_count=len(distinct_queries),
                distinct_value_count=len(distinct_value_signatures),
                queries=[
                    {
                        "query_id": entry.query_id,
                        "query": entry.query,
                        "values": entry.values,
                        "source": entry.source,
                        "duration_ms": entry.duration_ms,
                        "row_count": entry.row_count,
                    }
                    for entry in group
                ],
            )
        )
    repetitions.sort(key=lambda item: (-item.count, -item.distinct_value_count, item.normalized_query))
    return repetitions


def normalized_structure_for_record(record: dict[str, Any], occurrence: QueryOccurrence) -> str:
    if record.get("query_source") != "transaction":
        return normalize_query_structure(occurrence.query)
    normalized_statements = normalized_transaction_statements(record.get("statements") or [])
    return " ; ".join(normalized_statements)


def normalized_transaction_statements(statements: list[dict[str, Any]]) -> list[str]:
    return [
        normalize_query_structure(str(statement.get("query") or ""))
        for statement in statements
        if statement.get("query")
    ]


def format_query_entry(entry: dict[str, Any]) -> str:
    return " | ".join(
        [
            entry.get("query_id") or "-",
            f"rows={entry.get('row_count', 0)}",
            f"values={rendered_values(entry.get('values', []))}",
            entry.get("query") or "",
        ]
    )


def aggregate_query_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    grouped_entries: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = json.dumps(
            {
                "query": entry.get("query"),
                "values": entry.get("values"),
                "source": entry.get("source"),
                "row_count": entry.get("row_count"),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        counts[key] += 1
        grouped_entries.setdefault(key, dict(entry))

    aggregated: list[dict[str, Any]] = []
    for key, count in counts.items():
        entry = dict(grouped_entries[key])
        entry["run_count"] = count
        aggregated.append(entry)
    aggregated.sort(
        key=lambda entry: (
            -(entry.get("run_count") or 0),
            entry.get("query") or "",
            json.dumps(entry.get("values", []), ensure_ascii=True, sort_keys=True),
        )
    )
    return aggregated


def display_query_structure(repetition: QueryRepetition | dict[str, Any]) -> str:
    normalized_query = repetition["normalized_query"] if isinstance(repetition, dict) else repetition.normalized_query
    distinct_query_count = int(
        repetition["distinct_query_count"] if isinstance(repetition, dict) else repetition.distinct_query_count
    )
    queries = repetition["queries"] if isinstance(repetition, dict) else repetition.queries
    if distinct_query_count == 1 and queries:
        query = queries[0].get("query")
        if query:
            return " ".join(str(query).split())
    return str(normalized_query)


def _render_value(value: Any) -> Any:
    if isinstance(value, dict):
        jsonb_value = value.get("value")
        if set(value) == {"jsonb_version", "value"} and value.get("jsonb_version") == 1 and isinstance(jsonb_value, str):
            try:
                return json.loads(jsonb_value)
            except json.JSONDecodeError:
                return jsonb_value
        return {key: _render_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item) for item in value]
    return value


def rendered_values(values: list[Any]) -> str:
    return json.dumps([_render_value(value) for value in values], ensure_ascii=True)


def repetition_reuse_score(repetition: QueryRepetition | dict[str, Any]) -> int:
    count = int(repetition["count"] if isinstance(repetition, dict) else repetition.count)
    distinct_query_count = int(
        repetition["distinct_query_count"] if isinstance(repetition, dict) else repetition.distinct_query_count
    )
    distinct_value_count = int(
        repetition["distinct_value_count"] if isinstance(repetition, dict) else repetition.distinct_value_count
    )
    reusable_dimensions = max(distinct_value_count, distinct_query_count, 1)
    return count * reusable_dimensions
