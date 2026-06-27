import re
from typing import Any, Dict, List, Optional

import pandas as pd


def _normalize_query_id(raw_query_id: Any) -> str:
    """Normalize query ids so '020' and '20' map to the same key."""
    q = str(raw_query_id)
    if q.isdigit():
        return str(int(q))
    return q


def _is_true(value: Any) -> bool:
    return bool(pd.notna(value) and value == True)


def _is_not_true(value: Any) -> bool:
    return pd.isna(value) or value != True


def annotate_total_speedup_per_turn(history, cmp_to: str = "duckdb") -> str | None:
    """
    Track latest per-query runtimes at the largest scale factor seen so far and
    compute total speedup (sum baseline / sum impl) per turn.

    ``cmp_to`` selects the baseline system whose runtime the bespoke impl is
    compared against: ``"duckdb"`` (bespoke vs. DuckDB) or ``"umbra"`` (bespoke
    vs. Umbra). Speedups are computed as ``baseline_ms / impl_ms``.
    """
    if cmp_to not in ("duckdb", "umbra"):
        raise ValueError(f"Unknown cmp_to: {cmp_to!r}; expected 'duckdb' or 'umbra'")

    baseline_kind = f"{cmp_to}_runtime_ms"

    out_col = "validation/largest_sf_total_speedup"
    history[out_col] = None

    # apply filtering

    if "validation/skip_validate" in history.columns:
        filtered_history = history[
            (history["type"] == "validate")
            # & (history["validation/compile_with_optimize"] == True)
            # & (history["validation/trace_mode"] != True)
            & (history["validation/skip_validate"] != True)
        ]
    else:
        # not yet reportet, i.e. not found in history
        filtered_history = history[history["type"] == "validate"]

    runtime_regex = re.compile(
        rf"^validation/query_([a-zA-Z0-9]+)/(?P<kind>{re.escape(baseline_kind)}|impl_runtime_ms)$"
    )
    total_speedup_regex = re.compile(
        r"^validation/sf(?P<sf>[0-9]+(?:\.[0-9]+)?)_all_queries_total_speedup$"
    )

    # Map query -> runtime column names.
    runtime_cols: Dict[str, Dict[str, str]] = {}
    for col in filtered_history.columns:
        m = runtime_regex.match(col)
        if not m:
            continue
        qid = _normalize_query_id(m.group(1))
        runtime_cols.setdefault(qid, {})[m.group("kind")] = col

    # The precomputed total-speedup columns are always logged against DuckDB.
    # For any other baseline we recompute the total from the per-query runtimes
    # and ignore these shortcut columns.
    total_speedup_cols: Dict[float, str] = {}
    has_row_total_speedup = False
    if cmp_to == "duckdb":
        for col in filtered_history.columns:
            m = total_speedup_regex.match(col)
            if not m:
                continue
            total_speedup_cols[float(m.group("sf"))] = col

        has_row_total_speedup = "validation/total_speedup" in filtered_history.columns

    if not runtime_cols and not total_speedup_cols and not has_row_total_speedup:
        return None

    # Determine expected query set.
    expected_queries = set()
    if "validation/query_ids_executed" in filtered_history.columns:
        for qids in filtered_history["validation/query_ids_executed"]:
            if isinstance(qids, list):
                expected_queries.update(_normalize_query_id(qid) for qid in qids)

    # Fallback for runs without query_ids_executed in history.
    available_query_pairs = {
        qid
        for qid, kinds in runtime_cols.items()
        if baseline_kind in kinds and "impl_runtime_ms" in kinds
    }
    if expected_queries and available_query_pairs:
        expected_queries = expected_queries.intersection(available_query_pairs)
    elif available_query_pairs:
        # Fallback for runs without query_ids_executed in history.
        expected_queries = available_query_pairs

    if (
        runtime_cols
        and not total_speedup_cols
        and not has_row_total_speedup
        and not expected_queries
    ):
        return out_col

    # Track the latest runtime per query, separately for each scale factor. The
    # plotted series uses the largest scale factor that is computable so far.
    # This avoids dropping the line when a higher-SF validation appears before
    # it has enough correct, optimized runtime data to produce a total speedup.
    largest_computable_sf: Optional[float] = None
    runtimes_by_sf: Dict[float, Dict[str, Dict[str, float]]] = {}
    speedups_by_sf: Dict[float, float] = {}
    speedup_series: List[Optional[float]] = []

    for _, row in history.iterrows():
        row_sf = pd.to_numeric(row.get("validation/scale_factor"), errors="coerce")  # type: ignore
        optimize_compile_flag_set = row.get("validation/compile_with_optimize")
        trace_set = row.get("validation/trace_mode")
        skip_validate = row.get("validation/skip_validate")
        output_correct = row.get("validation/correct")

        if (
            row.get("type") == "validate"
            and pd.notna(row_sf)
            and _is_true(optimize_compile_flag_set)
            and _is_not_true(trace_set)
            and _is_not_true(skip_validate)
            and _is_true(output_correct)
        ):
            row_sf_float = float(row_sf)

            total_speedup_col = total_speedup_cols.get(row_sf_float)
            if total_speedup_col is not None:
                total_speedup = row.get(total_speedup_col)
                if pd.notna(total_speedup):
                    speedups_by_sf[row_sf_float] = float(total_speedup)
                    if (
                        largest_computable_sf is None
                        or row_sf_float > largest_computable_sf
                    ):
                        largest_computable_sf = row_sf_float
            elif has_row_total_speedup:
                total_speedup = row.get("validation/total_speedup")
                if pd.notna(total_speedup):
                    speedups_by_sf[row_sf_float] = float(total_speedup)
                    if (
                        largest_computable_sf is None
                        or row_sf_float > largest_computable_sf
                    ):
                        largest_computable_sf = row_sf_float

            current_runtimes = runtimes_by_sf.setdefault(row_sf_float, {})
            for qid in expected_queries:
                cols_for_query = runtime_cols.get(qid, {})
                impl_col = cols_for_query.get("impl_runtime_ms")
                baseline_col = cols_for_query.get(baseline_kind)
                if impl_col is None or baseline_col is None:
                    continue

                impl_val = row.get(impl_col)
                baseline_val = row.get(baseline_col)

                if pd.notna(impl_val):
                    current_runtimes.setdefault(qid, {})["impl_runtime_ms"] = float(
                        impl_val
                    )
                if pd.notna(baseline_val):
                    current_runtimes.setdefault(qid, {})[baseline_kind] = float(
                        baseline_val
                    )

            have_all_for_row_sf = bool(expected_queries) and all(
                qid in current_runtimes
                and "impl_runtime_ms" in current_runtimes[qid]
                and baseline_kind in current_runtimes[qid]
                for qid in expected_queries
            )
            if have_all_for_row_sf and (
                largest_computable_sf is None or row_sf_float > largest_computable_sf
            ):
                largest_computable_sf = row_sf_float

        if largest_computable_sf is None:
            speedup_series.append(None)
            continue

        if largest_computable_sf in speedups_by_sf:
            speedup_series.append(speedups_by_sf[largest_computable_sf])
            continue

        current_runtimes = runtimes_by_sf.get(largest_computable_sf, {})
        have_all = bool(expected_queries) and all(
            qid in current_runtimes
            and "impl_runtime_ms" in current_runtimes[qid]
            and baseline_kind in current_runtimes[qid]
            for qid in expected_queries
        )

        if not have_all:
            speedup_series.append(None)
            continue

        total_impl = sum(
            current_runtimes[qid]["impl_runtime_ms"] for qid in expected_queries
        )
        total_baseline = sum(
            current_runtimes[qid][baseline_kind] for qid in expected_queries
        )

        if total_impl <= 0:
            speedup_series.append(None)
        else:
            speedup_series.append(total_baseline / total_impl)

    history[out_col] = speedup_series
    return out_col
