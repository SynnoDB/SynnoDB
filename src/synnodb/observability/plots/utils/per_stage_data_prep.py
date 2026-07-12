import re
from pathlib import Path

import numpy as np

from synnodb.observability.plots.utils.wandb_trace_preprocessor import SECTION_RULES
from synnodb.observability.plots.utils.wandb_utils import (
    SCALE_FACTOR_COL,
    combine_histories,
    get_wandb_stats,
)

# ---------------------------------------------------------------------------
# Stage configuration
# Derived from SECTION_RULES in wandb_trace_preprocessor; maps section labels
# to ablation display names. Entries: (SectionRule, stage_display_name)
# ---------------------------------------------------------------------------
_ABLATION_SECTION_LABELS = {
    "implement queries": "Base\nImpl.",
    "optim card": "+Card\nStats. Info",
    "optim trace": "+Self-\nTracing",
    "optim expert": "+Exp.\nKnowledge",
    "optim human": "+Human-Ref.\nPrompting",
    "add mt": "+Multi-\nThreading",
}

STAGES_CONFIG = [
    (rule, _ABLATION_SECTION_LABELS.get(rule.label, rule.display_label))
    for rule in SECTION_RULES
]

INITIAL_STAGE = "Naive Impl."
STAGES = [cfg[1] for cfg in STAGES_CONFIG]
TOTAL_SPEEDUP_KEY = "__total_speedup__"
COMMIT_HASH_KEY = "__commit_hash__"
_SNAPSHOT_HASH_COL = "code/snapshot_hash"


def _target_sf_from_history(history) -> float | None:
    """Scale factor to report speedups at: the largest one actually validated.

    There is no longer a fixed per-benchmark scale factor. A run validates
    correctness cheapest-first across several rungs (e.g. 0.02 / 0.1 / 1.0) and
    the headline numbers are always taken at the final, largest rung, so the
    target is derived from the logged data rather than a hardcoded table. Returns
    ``None`` when the run logged no scale factor at all.
    """
    if SCALE_FACTOR_COL not in history.columns:
        return None
    scale_factors = history[SCALE_FACTOR_COL].dropna()
    if scale_factors.empty:
        return None
    return float(scale_factors.max())


def load_wandb_data(
    runs: list[tuple[str | list[str], str]],
    skip_cache: bool = False,
    target_sf: int | float | None = None,
    entity: str | None = None,
    project: str | None = None,
):
    summary_dict = {}
    history_dict = {}
    config_dict = {}

    for id, tag in runs:
        if isinstance(id, str):
            summary, history, config = get_wandb_stats(
                id,
                entity=entity,
                project=project,
                skip_cache=skip_cache,
                wandb_run_cache_path=Path("/mnt/labstore/bespoke_olap/wandb_cache"),
            )
        else:
            assert isinstance(id, list), (
                "Expected run ID to be a string or list of strings"
            )
            hists_list = []
            for run_id in id:
                summary, hist, config = get_wandb_stats(
                    run_id,
                    entity=entity,
                    project=project,
                    skip_cache=False,  # set to True to skip cache and fetch from W&B API
                    wandb_run_cache_path=Path("/mnt/labstore/bespoke_olap/wandb_cache"),
                )
                hists_list.append(hist)

            history = combine_histories(hists_list)

        summary_dict[tag] = summary
        history_dict[tag] = history
        config_dict[tag] = config

    if target_sf is None:
        target_sf = _target_sf_from_history(history_dict[next(iter(history_dict))])

    return history_dict, config_dict, summary_dict, target_sf


def process_data(history_dict, config_dict, summary_dict, cmp_to: str = "duckdb"):
    """Extract per-stage speedups against the reference system ``cmp_to``.

    ``cmp_to`` selects the baseline system whose runtime the bespoke impl is
    compared against: ``"duckdb"`` (bespoke vs. DuckDB) or ``"umbra"`` (bespoke
    vs. Umbra). All speedups/totals are computed as ``baseline_ms / impl_ms``.
    """
    if cmp_to not in ("duckdb", "umbra"):
        raise ValueError(f"Unknown cmp_to: {cmp_to!r}; expected 'duckdb' or 'umbra'")

    regex = re.compile(r"validation/query_([0-9a-zA-Z]+)/speedup")

    query_ids_by_tag = {}
    for tag, summary in summary_dict.items():
        query_ids_by_tag[tag] = sorted(
            {m.group(1) for k in summary.keys() if (m := regex.match(k))}
        )
        print(f"{tag}: {len(query_ids_by_tag[tag])} queries")

    # filter out 8a - it is a heavy outlier (produces 100+x speedup) - it seems duckdb is crazy bad on this query sometimes - but only simetimes...
    for tag in query_ids_by_tag:
        if "08a" in query_ids_by_tag[tag]:
            print(f"Removing query 08a from {tag} due to extreme outlier behavior")
            query_ids_by_tag[tag].remove("08a")

    def _total_speedup_from_runtime_rows(rows, query_ids):
        baseline_total = 0.0
        impl_total = 0.0
        for qid in query_ids:
            baseline_col = f"validation/query_{qid}/{cmp_to}_runtime_ms"
            impl_col = f"validation/query_{qid}/bespoke_runtime_ms"
            if baseline_col not in rows.columns or impl_col not in rows.columns:
                continue
            pairs = rows[[baseline_col, impl_col]].dropna()
            if pairs.empty:
                continue
            baseline_ms = float(pairs.iloc[-1][baseline_col])
            impl_ms = float(pairs.iloc[-1][impl_col])
            if np.isfinite(baseline_ms) and np.isfinite(impl_ms) and impl_ms > 0:
                baseline_total += baseline_ms
                impl_total += impl_ms

        if impl_total <= 0:
            return float("nan")
        return baseline_total / impl_total

    def _select_reference_row(rows, speedup_cols):
        if rows.empty:
            return None
        return rows.loc[rows[speedup_cols].notna().sum(axis=1).idxmax()]

    def extract_ablation_speedups(
        history, query_ids, num_total_queries, target_sf, stages_config
    ):
        stage_names = [cfg[1] for cfg in stages_config]

        val = history.copy()
        val = val[val["type"] == "validate"]
        if "validation/trace_mode" in val.columns:
            val = val[~val["validation/trace_mode"].astype(bool)]
        if "validation/compile_with_optimize" in val.columns:
            val = val[val["validation/compile_with_optimize"].astype(bool)]

        val = val.sort_values("_step").copy()

        # Recompute per-query speedup against the chosen baseline system. The
        # logged `.../speedup` column is always duckdb/impl, so for any other
        # baseline (e.g. umbra) we override it with baseline_ms / impl_ms.
        # Queries lacking baseline runtime data become NaN and drop out below.
        for qid in query_ids:
            baseline_col = f"validation/query_{qid}/{cmp_to}_runtime_ms"
            impl_col = f"validation/query_{qid}/bespoke_runtime_ms"
            speedup_col = f"validation/query_{qid}/speedup"
            if baseline_col in val.columns and impl_col in val.columns:
                impl_ms = val[impl_col].where(val[impl_col] > 0)
                val[speedup_col] = val[baseline_col] / impl_ms

        speedup_cols = [f"validation/query_{qid}/speedup" for qid in query_ids]
        speedup_cols = [c for c in speedup_cols if c in val.columns]

        stage_data = {}
        stage_totals = {}
        stage_commits = {}
        for name in stage_names:
            stage_data[name] = {}
            stage_totals[name] = float("nan")
            stage_commits[name] = None

        if val.empty or not speedup_cols:
            print("No usable validate rows / speedup columns for ablation extraction.")
            stage_data[TOTAL_SPEEDUP_KEY] = stage_totals
            stage_data[COMMIT_HASH_KEY] = stage_commits
            return stage_data

        # Stage windows: latest available value per query inside each stage window.
        stage_starts = _get_stage_starts(history, stages_config)
        print("Stage starts:", dict(zip(stage_names, stage_starts)))

        for i, name in enumerate(stage_names):
            lo = stage_starts[i]
            if lo is None:
                continue

            hi = next(
                (
                    stage_starts[j]
                    for j in range(i + 1, len(stage_starts))
                    if stage_starts[j] is not None
                ),
                int(1e18),
            )
            window_rows = val[(val["_step"] >= lo) & (val["_step"] < hi)]
            if window_rows.empty:
                continue

            # Prefer target_sf within the window; fall back to the largest SF
            # that actually has speedup data in this stage window (the MT stage,
            # for example, runs at a different SF than the rest of the pipeline).
            rows = window_rows
            if SCALE_FACTOR_COL in window_rows.columns:
                rows = window_rows[window_rows[SCALE_FACTOR_COL] == target_sf]
                if rows.empty or not any(
                    rows[c].notna().any() for c in speedup_cols if c in rows.columns
                ):
                    sfs = window_rows[SCALE_FACTOR_COL].dropna().unique().tolist()
                    sfs = sorted([s for s in sfs if np.isfinite(s)], reverse=True)
                    for sf in sfs:
                        candidate = window_rows[window_rows[SCALE_FACTOR_COL] == sf]
                        if any(
                            candidate[c].notna().any()
                            for c in speedup_cols
                            if c in candidate.columns
                        ):
                            print(
                                f"Stage {name!r}: target_sf={target_sf} unavailable, "
                                f"falling back to sf={sf}"
                            )
                            rows = candidate
                            break
            if rows.empty:
                continue

            # Commit hash: take it from the last entry of the df actually used
            # for this stage's speedups.
            if _SNAPSHOT_HASH_COL in rows.columns:
                hashes = rows.sort_values("_step")[_SNAPSHOT_HASH_COL].dropna()
                if not hashes.empty:
                    stage_commits[name] = str(hashes.iloc[-1])

            # Per-query bars: use whatever has already been measured in this stage.
            for qid in query_ids:
                col = f"validation/query_{qid}/speedup"
                if col not in rows.columns:
                    continue
                series = rows[col].dropna()
                if not series.empty:
                    stage_data[name][qid] = float(series.iloc[-1])

            # Stage totals: prefer full coverage if available, otherwise fallback.
            rows_for_total = rows
            if "validation/num_queries" in rows.columns:
                full_rows = rows[rows["validation/num_queries"] == num_total_queries]
                if not full_rows.empty:
                    rows_for_total = full_rows

            stage_totals[name] = _total_speedup_from_runtime_rows(
                rows_for_total, query_ids
            )

        stage_data[TOTAL_SPEEDUP_KEY] = stage_totals
        stage_data[COMMIT_HASH_KEY] = stage_commits
        return stage_data

    # Run extraction per series/run
    run_info = {}
    all_stage_speedups = {}
    for tag, hist in history_dict.items():
        benchmark = config_dict[tag]["benchmark"]
        target_sf = _target_sf_from_history(hist)
        qids = query_ids_by_tag[tag]

        run_info[tag] = {
            "benchmark": benchmark,
            "target_sf": target_sf,
            "query_ids": qids,
        }
        all_stage_speedups[tag] = extract_ablation_speedups(
            hist, qids, len(qids), target_sf, STAGES_CONFIG
        )

    print("All stage speedups:", all_stage_speedups)

    # apply overwrite - avoid inconsistent plots due to noise (measured in other benchmarking runs)
    # if "Bespoke CEB" in all_stage_speedups:
    #     all_stage_speedups["Bespoke CEB"][TOTAL_SPEEDUP_KEY][
    #         "w/ Human-Ref.\nPrompting"
    #     ] = 9.74

    # Print summary
    for tag, stage_data in all_stage_speedups.items():
        print(f"\n{tag}")
        for stage in STAGES:
            vals = [v for v in stage_data.get(stage, {}).values() if np.isfinite(v)]
            if vals:
                print(
                    f"  {stage!r:55s}: geomean={geomean(vals):.2f}x  "
                    f"avg={np.mean(vals):.2f}x  median={np.median(vals):.2f}x  "
                    f"total={stage_data[TOTAL_SPEEDUP_KEY].get(stage, float('nan')):.2f}x  "
                    f"n={len(vals)}"
                )
            else:
                print(f"  {stage!r:55s}: (no data)")

    active_stages = [
        s
        for s in STAGES
        if any(bool(stage_data.get(s)) for stage_data in all_stage_speedups.values())
    ]

    # skip the pin & tracing stage
    all_stage_speedups = {
        tag: {
            stage: vals
            for stage, vals in stage_data.items()
            if not ("pin" in stage.lower() and "trac" in stage.lower())
        }
        for tag, stage_data in all_stage_speedups.items()
    }

    # prune from __total_speedup__ and __commit_hash__ as well
    for tag, stage_data in all_stage_speedups.items():
        for special_key in (TOTAL_SPEEDUP_KEY, COMMIT_HASH_KEY):
            stage_data[special_key] = {
                stage: vals
                for stage, vals in stage_data.get(special_key, {}).items()
                if not ("pin" in stage.lower() and "trac" in stage.lower())
            }

    # prune from stages
    active_stages = [
        s for s in active_stages if not ("pin" in s.lower() and "trac" in s.lower())
    ]

    return all_stage_speedups, active_stages, run_info, TOTAL_SPEEDUP_KEY


def _get_stage_starts(history, stages_config):
    if "type" not in history.columns:
        return [None for _ in stages_config]

    prompt_cols = [
        c
        for c in ["current_prompt", "current_prompt_descriptor"]
        if c in history.columns
    ]
    if not prompt_cols:
        return [None for _ in stages_config]

    llm = history[history["type"].isin(["llm", "llm_call"])].copy()
    if llm.empty:
        return [None for _ in stages_config]

    # Prefer current_prompt, fallback to descriptor if prompt text is absent.
    llm["_prompt_text"] = ""
    if "current_prompt" in llm.columns:
        llm["_prompt_text"] = llm["current_prompt"].fillna("")
    if "current_prompt_descriptor" in llm.columns:
        llm["_prompt_text"] = llm["_prompt_text"].where(
            llm["_prompt_text"].str.len() > 0,
            llm["current_prompt_descriptor"].fillna(""),
        )

    llm = llm[llm["_prompt_text"].str.len() > 0][["_step", "_prompt_text"]].sort_values(
        "_step"
    )

    starts = []
    for rule, _ in stages_config:
        matched = llm[
            llm["_prompt_text"].apply(
                lambda s: rule.predicate(s) if isinstance(s, str) else False
            )
        ]
        starts.append(int(matched.iloc[0]["_step"]) if len(matched) else None)
    return starts


def geomean(values):
    finite = [v for v in values if np.isfinite(v) and v > 0]
    return np.exp(np.mean(np.log(finite))) if finite else float("nan")
