"""Shared helpers for reading capture metadata and result files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def load_records(jsonl_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def resolve_result_path(jsonl_path: Path, result_file: str | None) -> Path | None:
    if not result_file:
        return None
    path = Path(result_file)
    if path.is_absolute():
        return path
    candidate = jsonl_path.parent / path
    if candidate.exists():
        return candidate
    return path


def load_result_rows(result_path: Path) -> list[dict[str, Any]]:
    if result_path.suffix == ".json":
        with open(result_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if result_path.suffix == ".pkl":
        return pd.read_pickle(result_path).to_dict(orient="records")
    raise ValueError(f"unsupported result file: {result_path}")
