from __future__ import annotations

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from synnodb.observability.benchmark.plot_by_threads import _SYSTEM_MARKERS, plot_by_threads

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "query_id",
    "scale_factor",
    "benchmark",
    "system",
    "time_ms",
    "hostname",
    "snapshot",
}

X_MODES = ("scale_factor", "num_threads", "query_id")

SYSTEM_COLORS: dict[str, str] = {
    "bespoke": "#F02ABE",  # house blue
    "clickhouse": "#F4B400",  # logo-like gold
    "duckdb": "#FF6900",  # DuckDB hot orange
    "umbra": "#2E8B86",  # balanced green
}

JOURNAL_EDGE_BLUE = "#1a4d6f"
JOURNAL_ACCENT_RED = "#FF6B6B"

# Colors used in journal figures for line/speedup plots
_JOURNAL_LINE_COLORS: dict[str, str] = {
    "bespoke": "#2E86AB",
    "duckdb": "#666666",
    "umbra": "#7FB069",
    "clickhouse": "#F4B400",
}

_PAPER_RC: dict = {
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Helvetica", "Arial"],
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 10,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 7,
    "legend.title_fontsize": 8,
    "axes.linewidth": 0.8,
    "grid.linewidth": 0.5,
    "grid.color": "#cccccc",
    "lines.linewidth": 1.2,
    "lines.markersize": 4,
    "patch.linewidth": 0.5,
    "legend.framealpha": 0.9,
    "legend.edgecolor": "0.75",
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
}

_PRODUCT_RC: dict = {
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Helvetica", "Arial"],
    "font.size": 13,
    "axes.labelsize": 14,
    "axes.titlesize": 19,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "legend.fontsize": 11,
    "legend.title_fontsize": 11,
    "axes.linewidth": 1.4,
    "grid.linewidth": 0.8,
    "grid.color": "#d8dee9",
    "lines.linewidth": 2.4,
    "lines.markersize": 7,
    "patch.linewidth": 0.8,
    "legend.framealpha": 1.0,
    "legend.edgecolor": "0.85",
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.08,
}


def _apply_paper_style() -> None:
    sns.set_theme(style="ticks", context="paper")
    plt.rcParams.update(_PAPER_RC)


def _apply_product_style() -> None:
    sns.set_theme(style="whitegrid", context="talk")
    plt.rcParams.update(_PRODUCT_RC)


def _apply_plot_style(product_plot: bool = False) -> None:
    if product_plot:
        _apply_product_style()
    else:
        _apply_paper_style()


def _validate_log_paths(paths: list[str]) -> None:
    if not paths:
        raise ValueError("No benchmark logs provided.")
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"Benchmark log not found: {path}")


def _read_logs(paths: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for raw_path in paths:
        path = Path(raw_path)
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise ValueError(
                f"{path} is missing benchmark columns: {', '.join(sorted(missing))}"
            )
        frame["source_log"] = path.as_posix()
        frames.append(frame)

    if not frames:
        raise ValueError("No benchmark logs provided.")

    data = pd.concat(frames, ignore_index=True)
    data["query_id"] = data["query_id"].astype(str)
    data["scale_factor"] = pd.to_numeric(data["scale_factor"])
    data["time_ms"] = pd.to_numeric(data["time_ms"])
    return data.dropna(subset=["time_ms"])


def _workloads(data) -> list[str]:
    """Return the distinct workloads (benchmarks) in a stable order."""
    return sorted(data["benchmark"].astype(str).unique())


def _plot_by_scale(
    data, output: Path, title: str | None, show_speedup: bool = False,
    threads_to_show: list | None = None, show_value_labels: bool = True,
) -> None:
    summary = (
        data.groupby(["benchmark", "system", "scale_factor"], as_index=False)
        .agg(median_time_ms=("time_ms", "median"))
        .sort_values(["benchmark", "scale_factor", "system"])
    )
    summary["benchmark"] = summary["benchmark"].astype(str)
    workloads = _workloads(summary)

    if show_speedup:
        return _plot_by_scale_speedup(data, workloads, output, title,
                                      threads_to_show=threads_to_show,
                                      show_value_labels=show_value_labels)

    scale_palette = {
        s: SYSTEM_COLORS.get(s.lower(), "#888888") for s in summary["system"].unique()
    }
    fig, axes = plt.subplots(
        1,
        len(workloads),
        figsize=(6 * len(workloads), 3.2),
        squeeze=False,
        sharey=True,
    )
    axes = axes[0]
    for ax, workload in zip(axes, workloads):
        subset = summary[summary["benchmark"] == workload]
        _sys_markers = {s: _SYSTEM_MARKERS.get(s.lower(), "o") for s in subset["system"].unique()}
        sns.lineplot(
            data=subset,
            x="scale_factor",
            y="median_time_ms",
            hue="system",
            palette=scale_palette,
            style="system",
            markers=_sys_markers,
            dashes=False,
            ax=ax,
        )
        ax.set_xlabel("Scale factor")
        ax.set_ylabel("Median query time (ms)")
        ax.set_title(workload)
        ax.grid(True, axis="y")
        sns.despine(ax=ax)

    fig.suptitle(title or "Benchmark timings")
    fig.tight_layout()
    plt.savefig(output)
    return fig


_SPEEDUP_BASELINES = ["duckdb", "umbra"]
_SPEEDUP_MARKERS = {k: _SYSTEM_MARKERS[k] for k in ("duckdb", "umbra")}


def _speedup_workload_sort_key(name: str) -> tuple:
    n = name.lower().replace("-", "").replace("_", "")
    if "tpch" in n or "tpc" in n:
        return (0, name)
    if "ceb" in n:
        return (1, name)
    return (2, name)


def _workload_display_name(name: str) -> str:
    n = name.lower().replace("-", "").replace("_", "")
    if "tpch" in n or "tpc" in n:
        return "TPC-H"
    if "ceb" in n:
        return "CEB"
    return name


_MT_THREADS = 16
_THREAD_LINESTYLE: dict[int, str] = {}  # filled dynamically: 1→solid, MT→dashed


def _thread_linestyle(n: int) -> str:
    if n == 1:
        return "-"
    if n == _MT_THREADS:
        return "--"
    return (0, (3, 1, 1, 1))  # dash-dot for other counts


def _plot_by_scale_speedup(
    data: pd.DataFrame, workloads: list[str], output: Path, title: str | None,
    threads_to_show: list | None = None,
    show_value_labels: bool = True,
):
    has_threads = "num_threads" in data.columns
    group_cols = ["benchmark", "system", "scale_factor"]
    if has_threads:
        data = data.copy()
        data["num_threads"] = pd.to_numeric(data["num_threads"], errors="coerce")
        if threads_to_show is not None:
            keep = {float(t) for t in threads_to_show}
            data = data[data["num_threads"].isin(keep)]
        group_cols.append("num_threads")

    summary = (
        data.groupby(group_cols, as_index=False)
        .agg(median_time_ms=("time_ms", "median"))
    )
    summary["benchmark"] = summary["benchmark"].astype(str)

    thread_counts = sorted(summary["num_threads"].dropna().unique()) if has_threads else [None]

    workloads = sorted(workloads, key=_speedup_workload_sort_key)
    fig, axes = plt.subplots(
        1,
        len(workloads),
        figsize=(max(3.5, 2.8 * len(workloads)), 3.6),
        squeeze=False,
        sharey=True,
    )
    axes = axes[0]

    global_y_max = 0.0

    for idx, (ax, workload) in enumerate(zip(axes, workloads)):
        wb = summary[summary["benchmark"] == workload]
        sfs = sorted(wb["scale_factor"].unique())
        x = np.arange(len(sfs), dtype=float)

        for n_threads in thread_counts:
            if n_threads is not None:
                slice_ = wb[wb["num_threads"] == n_threads]
            else:
                slice_ = wb

            pivot = slice_.pivot_table(
                index="scale_factor", columns="system", values="median_time_ms"
            )
            pivot.columns = [c.lower() for c in pivot.columns]

            linestyle = _thread_linestyle(int(n_threads)) if n_threads is not None else "-"

            for baseline in _SPEEDUP_BASELINES:
                if baseline not in pivot.columns or "bespoke" not in pivot.columns:
                    continue
                y = np.array(
                    [
                        pivot.loc[sf, baseline] / pivot.loc[sf, "bespoke"]
                        if sf in pivot.index and pivot.loc[sf, "bespoke"] > 0
                        else np.nan
                        for sf in sfs
                    ]
                )
                color = _JOURNAL_LINE_COLORS.get(baseline, "#888888")
                ax.plot(
                    x,
                    y,
                    marker=_SPEEDUP_MARKERS.get(baseline, "o"),
                    linestyle=linestyle,
                    linewidth=2.2,
                    markersize=5,
                    color=color,
                    label=baseline.capitalize(),
                )
                valid = ~np.isnan(y)
                if valid.any():
                    local_max = float(np.nanmax(y[valid]))
                    label_offset = local_max * 0.04
                    global_y_max = max(global_y_max, local_max + label_offset)
                else:
                    label_offset = 0.1
                if show_value_labels:
                    for xv, yv in zip(x, y):
                        if np.isnan(yv):
                            continue
                        if yv > 100:
                            val_str = f"{yv:.0f}x"
                        elif yv > 10:
                            val_str = f"{yv:.1f}x"
                        else:
                            val_str = f"{yv:.2f}x"
                        ax.text(
                            xv,
                            yv + label_offset,
                            val_str,
                            ha="center",
                            va="bottom",
                            fontsize=8,
                            fontweight="bold",
                            color=color,
                        )

        ax.axhline(1.0, color="#aaaaaa", linewidth=0.8, linestyle="--")
        _label_size = plt.rcParams.get("axes.labelsize", 9)
        ax.set_xlabel("Scale factor", fontsize=_label_size, fontweight="bold")
        if idx == 0:
            ax.set_ylabel("Speedup\n(Bespoke vs. System)", fontsize=_label_size, fontweight="bold")
        ax.set_title(_workload_display_name(workload), fontsize=_label_size, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([str(int(sf)) if sf == int(sf) else str(sf) for sf in sfs])
        ax.set_xlim(-0.5, len(sfs) - 0.5)
        ax.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.5)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(True)

    # apply shared y-limit that accommodates the tallest text label across all subplots
    axes[0].set_ylim(0, global_y_max * 1.08)

    from matplotlib.lines import Line2D

    # deduplicate system handles (multiple thread series share the same system label)
    all_handles, all_labels = axes[0].get_legend_handles_labels()
    seen: dict[str, object] = {}
    for h, lbl in zip(all_handles, all_labels):
        seen.setdefault(lbl, h)
    sys_handles = list(seen.values())
    sys_labels = list(seen.keys())

    # synthetic handles for thread-count legend
    thread_handles, thread_labels = [], []
    if len(thread_counts) > 1:
        for n in thread_counts:
            ls = _thread_linestyle(int(n))
            thread_handles.append(
                Line2D([0], [0], color="#555555", linestyle=ls, linewidth=2.2)
            )
            thread_labels.append(str(int(n)))

    _legend_kw = dict(frameon=False, loc="upper center", handletextpad=0.3)
    _title_kw = dict(fontweight="bold")
    has_title = bool(title)
    top = 0.82 if has_title else 0.86
    legend_y = 0.995 if has_title else 0.985
    fig.tight_layout(rect=[0, 0, 1, top])
    if has_title:
        fig.suptitle(title)
    if sys_handles and thread_handles:
        leg1 = fig.legend(sys_handles, sys_labels, ncol=len(sys_handles),
                          title="Bespoke vs. System", bbox_to_anchor=(0.3, legend_y), **_legend_kw)
        leg1.get_title().update(_title_kw)
        leg2 = fig.legend(thread_handles, thread_labels, ncol=len(thread_handles),
                          title="Num-Threads", bbox_to_anchor=(0.75, legend_y), **_legend_kw)
        leg2.get_title().update(_title_kw)
    elif sys_handles:
        leg1 = fig.legend(sys_handles, sys_labels, ncol=len(sys_handles),
                          title="Bespoke vs. System", bbox_to_anchor=(0.5, legend_y), **_legend_kw)
        leg1.get_title().update(_title_kw)
    plt.savefig(output)
    return fig


def _plot_by_query(data, output: Path, title: str | None) -> None:
    summary = (
        data.groupby(
            ["benchmark", "system", "scale_factor", "query_id"], as_index=False
        )
        .agg(median_time_ms=("time_ms", "median"))
        .sort_values(["benchmark", "scale_factor", "query_id", "system"])
    )
    summary["benchmark"] = summary["benchmark"].astype(str)
    summary["query_label"] = (
        "sf"
        + summary["scale_factor"].astype(str)
        + " q"
        + summary["query_id"].astype(str)
    )

    query_palette = {
        s: SYSTEM_COLORS.get(s.lower(), "#888888") for s in summary["system"].unique()
    }
    workloads = _workloads(summary)
    height = max(
        3.5,
        min(
            12,
            0.28
            * summary.groupby("benchmark")["query_label"].nunique().max(),
        ),
    )
    fig, axes = plt.subplots(
        1,
        len(workloads),
        figsize=(7 * len(workloads), height),
        squeeze=False,
    )
    axes = axes[0]
    for ax, workload in zip(axes, workloads):
        subset = summary[summary["benchmark"] == workload]
        sns.barplot(
            data=subset,
            x="median_time_ms",
            y="query_label",
            hue="system",
            palette=query_palette,
            ax=ax,
        )
        ax.set_xlabel("Median query time (ms)")
        ax.set_ylabel("Query")
        ax.set_title(workload)
        ax.grid(True, axis="x")
        sns.despine(ax=ax)

    fig.suptitle(title or "Benchmark timings by query")
    fig.tight_layout()
    plt.savefig(output)
    return fig


def _print_summary(data: pd.DataFrame, x_mode: str, thread_plot_args: dict | None = None) -> None:
    from tabulate import tabulate

    thread_plot_args = thread_plot_args or {}
    has_threads = "num_threads" in data.columns

    if x_mode == "num_threads" and has_threads:
        # Mirror plot_by_threads: median per query → sum across queries → total seconds
        data = data.copy()
        data["num_threads"] = pd.to_numeric(data["num_threads"], errors="coerce")
        max_threads = thread_plot_args.get("max_threads")
        if max_threads is not None:
            data = data[data["num_threads"] <= max_threads]
        per_query = (
            data.groupby(
                ["benchmark", "system", "scale_factor", "num_threads", "query_id"],
                as_index=False,
            ).agg(median_time_ms=("time_ms", "median"))
        )
        summary = (
            per_query.groupby(
                ["benchmark", "system", "scale_factor", "num_threads"], as_index=False
            ).agg(value=("median_time_ms", "sum"))
        )
        summary["value"] = summary["value"] / 1000  # ms → s to match plot y-axis
        col_dim = "num_threads"
        value_label = "total time (s)"

    elif x_mode == "query_id":
        # Mirror _plot_by_query: median per (benchmark, system, scale_factor, query_id)
        summary = (
            data.groupby(
                ["benchmark", "system", "scale_factor", "query_id"], as_index=False
            ).agg(value=("time_ms", "median"))
        )
        col_dim = "query_id"
        value_label = "median time (ms)"

    else:
        # Mirror _plot_by_scale: median per (benchmark, system, scale_factor)
        summary = (
            data.groupby(
                ["benchmark", "system", "scale_factor"], as_index=False
            ).agg(value=("time_ms", "median"))
        )
        col_dim = "scale_factor"
        value_label = "median time (ms)"

    for benchmark, wb in summary.groupby("benchmark"):
        print(f"\n{benchmark}  [{value_label}]")

        if col_dim == "scale_factor":
            row_label = wb["system"].str.title()
        else:
            row_label = wb["system"].str.title() + " (SF " + wb["scale_factor"].astype(str) + ")"

        wb = wb.copy()
        wb["_row"] = row_label
        pivot = wb.pivot_table(index="_row", columns=col_dim, values="value", aggfunc="first")
        pivot.index.name = None
        pivot.columns.name = col_dim
        pivot = pivot.map(lambda v: f"{v:.2f}" if pd.notna(v) else "-")
        print(tabulate(pivot, headers="keys", tablefmt="simple"))


def plot_logs(args, show: bool = False, print_values: bool = False):
    logger.info("Plotting benchmark logs: %s", args.logs)
    thread_plot_args = dict(getattr(args, "thread_plot_args", {}) or {})
    product_plot = bool(
        getattr(args, "product_plot", False)
        or thread_plot_args.get("product_plot", False)
    )
    _apply_plot_style(product_plot=product_plot)
    _validate_log_paths(args.logs)
    data = _read_logs(args.logs)
    title = getattr(args, "title", None)

    x_mode = getattr(args, "x", None) or (
        "query_id" if getattr(args, "by_query", False) else "scale_factor"
    )

    if args.output is not None:
        output = Path(args.output)
    else:
        date_time_str = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        output = Path(__file__).parent / "plots" / f"{date_time_str}_{x_mode}.png"

    output.parent.mkdir(parents=True, exist_ok=True)

    if x_mode == "query_id":
        fig = _plot_by_query(data, output, title)
    elif x_mode == "num_threads":
        if getattr(args, "max_threads", None) is not None:
            thread_plot_args["max_threads"] = args.max_threads
        if getattr(args, "legend_pos", None) is not None:
            thread_plot_args["legend_pos"] = args.legend_pos
        if getattr(args, "product_plot", False):
            thread_plot_args["product_plot"] = True
        fig = plot_by_threads(
            data,
            _JOURNAL_LINE_COLORS,
            JOURNAL_EDGE_BLUE,
            output,
            title,
            **thread_plot_args,
        )
    else:
        show_speedup = bool(thread_plot_args.get("show_speedup", False))
        threads_to_show = thread_plot_args.get("threads_to_show", None)
        show_value_labels = bool(thread_plot_args.get("show_value_labels", True))
        fig = _plot_by_scale(
            data, output, title, show_speedup=show_speedup,
            threads_to_show=threads_to_show, show_value_labels=show_value_labels,
        )

    if print_values:
        _print_summary(data, x_mode, thread_plot_args=thread_plot_args)

    logger.info("Wrote benchmark plot to %s", output)

    if show:
        return fig
    else:
        plt.close()
        return
