import matplotlib.pyplot as plt
import numpy as np

from synnodb.observability.plots.utils.per_stage_data_prep import (
    INITIAL_STAGE,
    STAGES_CONFIG,
    _get_stage_starts,
    geomean,
)

prop_colors = [c["color"] for c in plt.rcParams["axes.prop_cycle"]]
journal_primary_blue = "#2E86AB"
journal_secondary_blue = "#62B4E4"
journal_edge_blue = "#1a4d6f"
journal_accent_red = "#FF6B6B"


def plot_ablation(
    all_stage_speedups,
    runs: list[tuple[str, str]],
    run_info: dict,
    stages: list[str],
    total_speedup_key: str,
    cmp_system: str,
):

    run_palette = [journal_primary_blue, journal_secondary_blue] + prop_colors
    run_colors = {
        tag: run_palette[i % len(run_palette)] for i, (_, tag) in enumerate(runs)
    }

    # Extra horizontal padding between single-threaded stages and the
    # multi-threaded stage so the divider/labels have visual breathing room.
    mt_idx = next(
        (i for i, s in enumerate(stages) if "multi" in s.lower()),
        None,
    )
    mt_gap = 0.4
    x = np.arange(len(stages), dtype=float)
    if mt_idx is not None and mt_idx > 0:
        x[mt_idx:] += mt_gap
    n_runs = len(all_stage_speedups)
    group_width = 0.78
    slot_width = group_width / max(n_runs, 1)
    bar_width = slot_width * 0.86
    offsets = (np.arange(n_runs) - (n_runs - 1) / 2) * slot_width

    fig, ax = plt.subplots(figsize=(6, 4))

    # geometric mean vs standard average toggle
    metric = "total"  # median, average, geomean, total
    metric_str = {
        "geomean": "Geometric Mean",
        "average": "Average",
        "median": "Median",
        "total": "Total",
    }[metric]

    def _stage_has_complete_query_data(stage_values, expected_qids):
        # A stage is complete only if every query has a non-NaN value (inf is allowed).
        for qid in expected_qids:
            v = stage_values.get(qid)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return False
        return True

    all_vals = []
    for offset, (tag, stage_data) in zip(offsets, all_stage_speedups.items()):
        expected_qids = run_info[tag]["query_ids"]
        complete_stage_mask = [
            _stage_has_complete_query_data(stage_data.get(s, {}), expected_qids)
            for s in stages
        ]

        if metric == "geomean":
            val = [geomean(list(stage_data.get(s, {}).values())) for s in stages]
        elif metric == "average":
            val = [np.mean(list(stage_data.get(s, {}).values())) for s in stages]
        elif metric == "median":
            val = [np.median(list(stage_data.get(s, {}).values())) for s in stages]
        elif metric == "total":
            val = [
                stage_data.get(total_speedup_key, {}).get(s, float("nan"))
                for s in stages
            ]
        else:
            raise ValueError(f"Unknown metric {metric}")

        # Only render aggregate bars for categories with full per-query coverage
        # (not applicable for "total", which is computed from available queries only).
        if metric != "total":
            val = [
                v if is_complete else float("nan")
                for v, is_complete in zip(val, complete_stage_mask)
            ]

        all_vals.extend([m for m in val if np.isfinite(m)])

        bars = ax.bar(
            x + offset,
            val,
            bar_width,
            label=tag,
            color=run_colors[tag],
            zorder=3,
            edgecolor=journal_edge_blue,
            linewidth=2.0,
            alpha=1,
        )

        for bar, val in zip(bars, val):
            if np.isfinite(val):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    val + 0.03,
                    f"{val:.2f}x",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                )

    ax.axhline(
        y=1,
        color=journal_accent_red,
        label=f"{cmp_system} baseline",
        linestyle="--",
        linewidth=1.2,
        # zorder=1,
    )
    # ax.text(
    #     0.01,
    #     1.07,
    #     "DuckDB baseline (speedup = 1x)",
    #     transform=ax.get_yaxis_transform(),
    #     color=journal_accent_red,
    #     fontsize=9,
    #     ha="left",
    #     va="bottom",
    # )

    ax.set_xlabel("Optimization Stage", fontsize=11, fontweight="bold")
    ax.set_ylabel(f"{metric_str} Speedup", fontsize=11, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(stages, fontsize=10)
    ax.tick_params(axis="y", labelsize=10)

    ax.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.5, zorder=1)
    ax.set_axisbelow(True)
    # ax.spines["top"].set_visible(False)
    # ax.spines["right"].set_visible(False)

    if all_vals:
        ymax = max(1.25, max(all_vals) * 1.18)
    else:
        ymax = 1.5
    ax.set_ylim(0, ymax)

    # Divider between single-threaded and multi-threaded stages.
    if mt_idx is not None and mt_idx > 0:
        divider_color = "#333333"
        divider_x = (x[mt_idx - 1] + x[mt_idx]) / 2
        label_offset_x = 0.04
        ax.axvline(
            x=divider_x,
            color=divider_color,
            linestyle="--",
            linewidth=1.2,
            zorder=2,
        )
        ax.text(
            divider_x - label_offset_x,
            0.5,
            "single-threaded",
            rotation=90,
            ha="right",
            va="center",
            fontsize=7.5,
            color=divider_color,
            transform=ax.get_xaxis_transform(),
        )
        ax.text(
            divider_x + label_offset_x,
            0.5,
            "multi-threaded",
            rotation=90,
            ha="left",
            va="center",
            fontsize=7.5,
            color=divider_color,
            transform=ax.get_xaxis_transform(),
        )

    legend_cols = min(3, len(all_stage_speedups) + 1)
    handles, labels = ax.get_legend_handles_labels()
    duckdb_idx = next(
        (
            i
            for i, lbl in enumerate(labels)
            if lbl.lower().startswith("duckdb baseline")
        ),
        None,
    )
    if duckdb_idx is not None:
        handles.append(handles.pop(duckdb_idx))
        labels.append(labels.pop(duckdb_idx))
    ax.legend(
        handles,
        labels,
        frameon=False,
        fontsize=10,
        ncols=legend_cols,
        loc="upper right",
        bbox_to_anchor=(1.0, 1.15),
    )

    plt.tight_layout()

    benchmarks_in_plot = sorted({info["benchmark"] for info in run_info.values()})
    if len(benchmarks_in_plot) == 1:
        benchmark = benchmarks_in_plot[0]
        target_sf_values = sorted({info["target_sf"] for info in run_info.values()})
        sf_suffix = target_sf_values[0] if len(target_sf_values) == 1 else "multi"
        out_path = f"figures/ablation_study_{benchmark}_sf{sf_suffix}_{metric}.pdf"
    else:
        out_path = f"figures/ablation_study_multi_benchmark_{metric}.pdf"

    plt.savefig(
        out_path,
        dpi=300,
        bbox_inches="tight",
        facecolor="white",
    )
    print(f"Saved: {out_path}")
    plt.show()


def plot_per_query_and_stage_speedups(
    all_stage_speedups,
    run_info,
    stages,
    benchmark_name_dict: dict[str, str],
):
    # Per-query breakdown: speedup at each stage as a grouped bar chart

    Y_MAX = 25

    for tag, stage_data in all_stage_speedups.items():
        benchmark = run_info[tag]["benchmark"]
        target_sf = run_info[tag]["target_sf"]
        query_ids_for_tag = run_info[tag]["query_ids"]
        query_ids_formatted = [qid.lstrip("0") or "0" for qid in query_ids_for_tag]

        fig, ax = plt.subplots(figsize=(12, 4), dpi=300)

        stage_colors = [prop_colors[i] for i in range(len(stages))]
        n_stages = len(stages)
        x = np.arange(len(query_ids_formatted))
        w = 0.9 / n_stages
        offsets = np.linspace(-(n_stages - 1) / 2, (n_stages - 1) / 2, n_stages) * w

        for offset, stage, color in zip(offsets, stages, stage_colors):
            vals = [
                stage_data.get(stage, {}).get(qid, float("nan"))
                for qid in query_ids_for_tag
            ]

            bars = ax.bar(
                x + offset,
                vals,
                w,
                label=stage,
                color=color,
                zorder=3,
                edgecolor="white",
                linewidth=0.4,
            )

            for bar, val in zip(bars, vals):
                if np.isfinite(val) and val > Y_MAX:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        Y_MAX - 0.35,
                        f"{val:.2f}x",
                        ha="center",
                        va="top",
                        fontsize=8.5,
                        fontweight="medium",
                        rotation=0,
                    )

        ax.axhline(
            y=1,
            color="red",
            linestyle="--",
            linewidth=1.0,
            label="DuckDB Baseline",
            zorder=2,
        )
        ax.set_xlabel(
            f"{benchmark_name_dict[benchmark]} Query ID", fontsize=11, fontweight="bold"
        )
        ax.set_ylabel(
            f"Speedup vs DuckDB (SF={target_sf})", fontsize=11, fontweight="bold"
        )
        ax.set_xticks(x)
        ax.set_xticklabels(query_ids_formatted, rotation=0, ha="center", fontsize=9)
        ax.tick_params(axis="y", labelsize=9)
        ax.legend(
            frameon=False,
            ncols=n_stages + 1,
            loc="upper left",
            bbox_to_anchor=(0.1, -0.15),
            fontsize=9,
        )
        ax.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.5, zorder=1)
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylim(0, Y_MAX)
        plt.tight_layout()
        out_path = f"figures/ablation_per_query_{tag.replace(' ', '_')}_{benchmark}_sf{target_sf}.pdf"
        plt.savefig(out_path, bbox_inches="tight")
        print(f"Saved: {out_path}")
        plt.show()


def plot_per_query_and_stage_runtimes(
    history_dict, run_info, benchmark_name_dict, stages
):
    # Per-query runtime breakdown: DuckDB runtime + implementation runtime by stage
    # One grouped bar plot per run/tag.

    RUNTIME_Y_SCALE = "log"  # "linear" or "log"

    def _extract_runtime_series(history, query_ids, target_sf, stages_config):
        val = history[history["type"] == "validate"].copy()

        if "validation/scale_factor" in val.columns:
            val = val[val["validation/scale_factor"] == target_sf]
        if "validation/trace_mode" in val.columns:
            val = val[~val["validation/trace_mode"].astype(bool)]
        val = val.sort_values("_step")

        stage_names: list[str] = [cfg[1] for cfg in stages_config]
        stage_values = {INITIAL_STAGE: {qid: float("nan") for qid in query_ids}}
        for name in stage_names:
            stage_values[name] = {qid: float("nan") for qid in query_ids}

        if val.empty:
            return stage_values, val

        stage_starts = _get_stage_starts(history, stages_config)

        next_stage_start = next((s for s in stage_starts if s is not None), int(1e18))
        windows: list[tuple[str, int | None, int | None]] = [
            (INITIAL_STAGE, -int(1e18), next_stage_start)
        ]

        for i, name in enumerate(stage_names):
            lo = stage_starts[i]
            if lo is None:
                windows.append((name, None, None))
                continue
            hi = next(
                (
                    stage_starts[j]
                    for j in range(i + 1, len(stage_starts))
                    if stage_starts[j] is not None
                ),
                int(1e18),
            )
            windows.append((name, lo, hi))

        for stage_name, lo, hi in windows:
            if lo is None:
                continue

            if stage_name == INITIAL_STAGE:
                rows = val[val["_step"] < hi]
            else:
                rows = val[(val["_step"] >= lo) & (val["_step"] < hi)]

            if rows.empty:
                continue

            for qid in query_ids:
                impl_col = f"validation/query_{qid}/bespoke_runtime_ms"
                if impl_col not in rows.columns:
                    continue
                series = rows[impl_col].dropna()
                if not series.empty:
                    stage_values[stage_name][qid] = float(series.iloc[-1])

        return stage_values, val

    for tag, hist in history_dict.items():
        benchmark = run_info[tag]["benchmark"]
        target_sf = run_info[tag]["target_sf"]
        query_ids_for_tag = run_info[tag]["query_ids"]
        query_ids_formatted = [qid.lstrip("0") or "0" for qid in query_ids_for_tag]

        stage_values, val = _extract_runtime_series(
            hist, query_ids_for_tag, target_sf, STAGES_CONFIG
        )

        if val.empty:
            print(f"Skipping runtime plot for {tag}: no validate rows after filtering")
            continue

        # DuckDB baseline: latest available value per query in filtered validate rows.
        duckdb_vals = []
        for qid in query_ids_for_tag:
            col = f"validation/query_{qid}/duckdb_runtime_ms"
            if col not in val.columns:
                duckdb_vals.append(float("nan"))
                continue
            series = val[col].dropna()
            duckdb_vals.append(
                float(series.iloc[-1]) if not series.empty else float("nan")
            )

        series_names = ["DuckDB"] + stages
        series_values = {"DuckDB": duckdb_vals}
        for stage in stages:
            series_values[stage] = [
                stage_values.get(stage, {}).get(qid, float("nan"))
                for qid in query_ids_for_tag
            ]

        n_series = len(series_names)
        x = np.arange(len(query_ids_for_tag))
        group_width = 0.9
        w = group_width / max(n_series, 1)
        offsets = (np.arange(n_series) - (n_series - 1) / 2) * w

        fig, ax = plt.subplots(figsize=(12, 4.5))

        runtime_stage_colors = [
            journal_accent_red,
            *[prop_colors[i % len(prop_colors)] for i in range(len(stages))],
        ]

        for offset, name, color in zip(offsets, series_names, runtime_stage_colors):
            ax.bar(
                x + offset,
                series_values[name],
                w,  # * 0.96,
                label=name,
                color=color,
                zorder=3,
                edgecolor="white",
                linewidth=0.4,
            )

        ax.set_xlabel(
            f"{benchmark_name_dict[benchmark]} Query ID", fontsize=11, fontweight="bold"
        )
        ax.set_ylabel(f"Runtime (ms, SF={target_sf})", fontsize=11, fontweight="bold")

        ax.set_xticks(x)
        ax.set_xticklabels(query_ids_formatted, rotation=0, ha="center", fontsize=9)
        ax.tick_params(axis="y", labelsize=9)

        if RUNTIME_Y_SCALE == "log":
            ax.set_yscale("log")

        ax.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.5, zorder=1)
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        ax.legend(
            frameon=False,
            ncols=n_series,
            loc="upper left",
            bbox_to_anchor=(0.15, -0.1),
            fontsize=9,
        )

        plt.tight_layout()
        out_path = f"figures/ablation_runtime_per_query_{tag.replace(' ', '_')}_{benchmark}_sf{target_sf}.pdf"
        plt.savefig(out_path, bbox_inches="tight")
        print(f"Saved: {out_path}")
        plt.show()
