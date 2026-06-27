from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

_SYSTEM_MARKERS: dict[str, str] = {
    "bespoke": "o",
    "duckdb": "s",
    "umbra": "D",
    "clickhouse": "^",
}


def plot_by_threads(
    data,
    SYSTEM_COLORS: dict[str, str],
    JOURNAL_EDGE_BLUE: str,
    output: Path,
    title: str | None = None,
    show_speedup: bool = True,
    show_runtime: bool = True,
    speedup: bool | None = None,
    runtime: bool | None = None,
    max_threads: int | None = None,
    legend_pos: str = "up",  # up/top | bottom
    product_plot: bool = False,
    annotate_points: bool = False,
) -> Figure:
    if speedup is not None:
        show_speedup = speedup
    if runtime is not None:
        show_runtime = runtime
    if not show_speedup and not show_runtime:
        raise ValueError("At least one of show_speedup or show_runtime must be True.")
    legend_pos = legend_pos.lower()
    if legend_pos == "top":
        legend_pos = "up"
    if legend_pos not in {"up", "bottom"}:
        raise ValueError(f"legend_pos must be 'up' or 'bottom', got {legend_pos!r}.")
    label_fontsize = 14 if product_plot else 11
    tick_fontsize = 12 if product_plot else 9
    legend_fontsize = 11 if product_plot else 9
    annotation_fontsize = 10 if product_plot else 8
    grid_alpha = 0.18 if product_plot else 0.3
    grid_linewidth = 0.8 if product_plot else 0.5
    axis_label_weight = "semibold" if product_plot else "bold"
    line_width = 2.4 if product_plot else None
    marker_size = 7 if product_plot else None
    tick_params = {"labelsize": tick_fontsize}
    if product_plot:
        tick_params["length"] = 0

    if "num_threads" not in data.columns:
        raise ValueError(
            "Column 'num_threads' not found in the benchmark logs. "
            "Re-run the benchmark with --num_threads to capture thread counts."
        )
    data = data.copy()
    data["num_threads"] = pd.to_numeric(data["num_threads"])
    if max_threads is not None:
        if max_threads < 1:
            raise ValueError(f"max_threads must be >= 1, got {max_threads}.")
        data = data[data["num_threads"] <= max_threads]
        if data.empty:
            raise ValueError(
                f"No benchmark rows remain after filtering to num_threads <= {max_threads}."
            )

    per_query = data.groupby(
        ["benchmark", "system", "scale_factor", "num_threads", "query_id"],
        as_index=False,
    ).agg(median_time_ms=("time_ms", "median"))
    summary = (
        per_query.groupby(
            ["benchmark", "system", "scale_factor", "num_threads"], as_index=False
        )
        .agg(total_time_ms=("median_time_ms", "sum"))
        .sort_values(["benchmark", "scale_factor", "num_threads", "system"])
    )
    summary["run_label"] = (
        summary["benchmark"].astype(str) + " sf" + summary["scale_factor"].astype(str)
    )

    def sf_fmt(sf):
        return f"{int(sf)}" if sf == int(sf) else f"{sf}"

    bespoke_ref = summary[summary["system"].str.lower() == "bespoke"][
        ["benchmark", "scale_factor", "num_threads", "total_time_ms"]
    ].rename(columns={"total_time_ms": "bespoke_time_ms"})

    non_bespoke = summary[summary["system"].str.lower() != "bespoke"].copy()
    non_bespoke = non_bespoke.merge(
        bespoke_ref, on=["benchmark", "scale_factor", "num_threads"], how="inner"
    )
    non_bespoke["ratio"] = non_bespoke["total_time_ms"] / non_bespoke["bespoke_time_ms"]
    non_bespoke["comparison"] = (
        "bespoke vs. "
        + non_bespoke["system"].str.lower()
        + " ("
        + non_bespoke["run_label"]
        + ")"
    )
    non_bespoke = non_bespoke.sort_values("num_threads")

    _WORKLOAD_ORDER = ["tpch", "ceb"]
    _WORKLOAD_DISPLAY_NAMES = {"tpch": "TPC-H", "ceb": "CEB"}

    def _workload_sort_key(w):
        w_lower = w.lower()
        return (
            _WORKLOAD_ORDER.index(w_lower)
            if w_lower in _WORKLOAD_ORDER
            else len(_WORKLOAD_ORDER),
            w_lower,
        )

    workloads = sorted(
        summary["benchmark"].astype(str).unique(), key=_workload_sort_key
    )
    n_workloads = len(workloads)
    subplot_count = int(show_runtime) + int(show_speedup)
    total_cols = n_workloads * subplot_count

    figsize = (3.5 * total_cols, 3.6)

    fig, axes_flat = plt.subplots(1, total_cols, figsize=figsize, dpi=300)
    if product_plot:
        fig.patch.set_facecolor("white")
    if total_cols == 1:
        axes_flat = [axes_flat]
    else:
        axes_flat = list(axes_flat)

    # Legends placed after tight_layout so their figure-space bounding boxes don't
    # confuse tight_layout's spacing calculation (it converts them to axes space and
    # gets a huge number, opening enormous horizontal gaps between subplots).
    _deferred_legends: list[dict] = []

    # Pre-compute sorted SFs per workload so the scale-factor legend can show
    # combined labels like "TPC-H: 50 / CEB: 5" instead of just "50".
    workload_sf_map: dict[str, list] = {
        w: sorted(
            summary[summary["benchmark"].astype(str) == w]["scale_factor"].unique()
        )
        for w in workloads
    }

    for wi, workload in enumerate(workloads):
        summary_w = summary[summary["benchmark"].astype(str) == workload]
        non_bespoke_w = non_bespoke[non_bespoke["benchmark"].astype(str) == workload]
        show_legend = wi == 0  # draw legend only once to avoid clutter
        col_start = wi * subplot_count
        axes_w = list(axes_flat[col_start : col_start + subplot_count])

        if show_runtime:
            ax_left = axes_w.pop(0)
            summary_cat = summary_w.assign(
                num_threads=summary_w["num_threads"].astype(str),
                scale_factor_label=summary_w["scale_factor"].map(sf_fmt),
                total_time_s=summary_w["total_time_ms"] / 1000,
            )
            left_palette = {
                s: SYSTEM_COLORS.get(s.lower(), "#888888")
                for s in summary_cat["system"].unique()
            }
            sf_labels = [
                sf_fmt(sf) for sf in sorted(summary_cat["scale_factor"].unique())
            ]
            legend_linestyle_cycle = [
                "-",
                "--",
                ":",
                "-.",
                (0, (1, 2)),
                (0, (8, 2)),
                (0, (3, 1, 1, 1)),
            ]
            sf_legend_linestyles = {
                sf: legend_linestyle_cycle[i % len(legend_linestyle_cycle)]
                for i, sf in enumerate(sf_labels)
            }
            # Markers encode system; linestyles encode scale factor.
            _sys_marker_fallback = ["o", "s", "D", "^", "v", "P", "X"]
            _sorted_systems_lower = sorted(
                s.lower() for s in summary_w["system"].unique()
            )
            system_marker_map = {
                s_lower: _SYSTEM_MARKERS.get(
                    s_lower,
                    _sys_marker_fallback[
                        _sorted_systems_lower.index(s_lower) % len(_sys_marker_fallback)
                    ],
                )
                for s_lower in _sorted_systems_lower
            }
            _lw = (
                line_width
                if line_width is not None
                else plt.rcParams.get("lines.linewidth", 1.5)
            )
            _ms = (
                marker_size
                if marker_size is not None
                else plt.rcParams.get("lines.markersize", 5)
            )
            for sf_str in sf_labels:
                ls = sf_legend_linestyles[sf_str]
                sf_subset = summary_w[summary_w["scale_factor"].map(sf_fmt) == sf_str]
                for system, grp in sf_subset.groupby("system"):
                    grp = grp.sort_values("num_threads")
                    ax_left.plot(
                        grp["num_threads"].astype(
                            str
                        ),  # categorical axis → even spacing like seaborn
                        grp["total_time_ms"] / 1000,
                        color=left_palette.get(system, "#888888"),
                        marker=system_marker_map.get(system.lower(), "o"),
                        linestyle=ls,
                        linewidth=_lw,
                        markersize=_ms,
                        alpha=0.95 if product_plot else 1.0,
                    )
            ax_left.set_yscale("log")
            ax_left.set_ylim(bottom=0.4)
            ax_left.set_xlabel(
                "Number of Threads",
                fontsize=label_fontsize,
                fontweight=axis_label_weight,
            )
            if wi == 0:
                ax_left.set_ylabel(
                    "$\\mathbf{Total\\ Query\\ Time}$\n(seconds, log scale)",
                    fontsize=label_fontsize,
                    fontweight=axis_label_weight,
                )
            else:
                ax_left.set_ylabel("")
            ax_left.tick_params(axis="both", **tick_params)
            ax_left.grid(
                axis="y",
                alpha=grid_alpha,
                linestyle="-",
                linewidth=grid_linewidth,
                zorder=1,
            )
            ax_left.set_axisbelow(True)
            for spine in ax_left.spines.values():
                spine.set_visible(True)
            if product_plot:
                ax_left.set_facecolor("#fbfcff")
                ax_left.spines["left"].set_color("#c6ccd6")
                ax_left.spines["bottom"].set_color("#c6ccd6")

            # Remove the seaborn auto-legend *before* annotating: while present it
            # uses loc="best", whose position search re-scans every annotation text
            # on each draw, making the collision avoidance below quadratic.
            legend = ax_left.get_legend()
            if legend is not None:
                legend.remove()

            if annotate_points:
                _annotate_total_time_points_without_collisions(
                    fig,
                    ax_left,
                    [
                        {
                            "x": str(int(row["num_threads"])),
                            "y": row["total_time_ms"] / 1000,
                            "text": f"{row['total_time_ms'] / 1000:.1f}",
                        }
                        for _, row in summary_w.iterrows()
                    ],
                    fontsize=annotation_fontsize,
                )

            if show_legend:
                _system_order = ["bespoke", "umbra", "duckdb"]

                def _system_sort_key(s):
                    return (
                        _system_order.index(s.lower())
                        if s.lower() in _system_order
                        else len(_system_order),
                        s.lower(),
                    )

                system_handles = [
                    Line2D(
                        [0],
                        [0],
                        color=left_palette.get(system, "#888888"),
                        marker=system_marker_map.get(system.lower(), "o"),
                        linestyle="-",
                        label=system,
                    )
                    for system in sorted(
                        summary_cat["system"].unique(), key=_system_sort_key
                    )
                ]
                # Build combined labels: "50 (TPC-H), 5 (CEB)" per position.
                # Falls back to bare SF value when only one workload is present.
                _combined_sf_labels = []
                for _i, _sf in enumerate(sf_labels):
                    _parts = []
                    for _w in workloads:
                        _w_sfs = [sf_fmt(s) for s in workload_sf_map.get(_w, [])]
                        _w_disp = _WORKLOAD_DISPLAY_NAMES.get(_w.lower(), _w)
                        _parts.append(
                            f"{_w_sfs[_i] if _i < len(_w_sfs) else '?'} ({_w_disp})"
                        )
                    _combined_sf_labels.append(
                        ", ".join(_parts)
                        if len(_parts) > 1
                        else (_parts[0] if _parts else _sf)
                    )
                scale_factor_handles = [
                    Line2D(
                        [0],
                        [0],
                        color="0.25",
                        marker="None",
                        linestyle=sf_legend_linestyles[sf],
                        label=combined_label,
                    )
                    for sf, combined_label in zip(sf_labels, _combined_sf_labels)
                ]
                x_pos = (0.05, 0.95)
                y_pos = (-0.08, 0.97)
                system_legend_kwargs = {
                    "loc": "upper left" if legend_pos == "bottom" else "lower left",
                    "bbox_to_anchor": (x_pos[0], y_pos[0])
                    if legend_pos == "bottom"
                    else (x_pos[0], y_pos[1]),
                }
                scale_factor_legend_kwargs = {
                    "loc": "upper right" if legend_pos == "bottom" else "lower right",
                    "bbox_to_anchor": (x_pos[1], y_pos[0])
                    if legend_pos == "bottom"
                    else (x_pos[1], y_pos[1]),
                }
                if legend_pos == "up":
                    system_legend_kwargs["bbox_transform"] = fig.transFigure
                    scale_factor_legend_kwargs["bbox_transform"] = fig.transFigure
                _deferred_legends.append(
                    {
                        "ax": ax_left,
                        "add_artist": True,
                        "call_kwargs": dict(
                            handles=system_handles,
                            title="System",
                            ncol=max(1, len(system_handles)),
                            frameon=False,
                            fontsize=legend_fontsize,
                            title_fontsize=legend_fontsize,
                            columnspacing=1.0 if product_plot else 0.8,
                            handletextpad=0.5 if product_plot else 0.3,
                            **system_legend_kwargs,
                        ),
                    }
                )
                _deferred_legends.append(
                    {
                        "ax": ax_left,
                        "add_artist": False,
                        "call_kwargs": dict(
                            handles=scale_factor_handles,
                            title="Scale factor",
                            ncol=1,
                            frameon=False,
                            fontsize=legend_fontsize,
                            title_fontsize=legend_fontsize,
                            columnspacing=1.0 if product_plot else 0.8,
                            handletextpad=0.5 if product_plot else 0.3,
                            handlelength=3,
                            **scale_factor_legend_kwargs,
                        ),
                    }
                )

        if show_speedup:
            ax_right = axes_w.pop(0)
            if not non_bespoke_w.empty:
                sfs = sorted(non_bespoke_w["scale_factor"].unique())
                sf_hatches = dict(zip(sfs, ["", "//", "\\\\", "xx", "oo", "--"]))

                comp_meta = (
                    non_bespoke_w[["comparison", "system", "scale_factor"]]
                    .drop_duplicates("comparison")
                    .set_index("comparison")
                )
                palette = {
                    c: SYSTEM_COLORS.get(comp_meta.loc[c, "system"].lower(), "#888888")
                    for c in comp_meta.index
                }

                hue_order = (
                    comp_meta.reset_index()
                    .sort_values(["scale_factor", "system"])["comparison"]
                    .tolist()
                )
                sns.barplot(
                    data=non_bespoke_w,
                    x="num_threads",
                    y="ratio",
                    hue="comparison",
                    hue_order=hue_order,
                    palette=palette,
                    ax=ax_right,
                )
                # ax.containers: one BarContainer per hue category, matches hue_order
                for container, comp in zip(ax_right.containers, hue_order):
                    sf = comp_meta.loc[comp, "scale_factor"]
                    for patch in container:
                        patch.set_hatch(sf_hatches.get(sf, ""))
                        patch.set_edgecolor("white")
                        patch.set_linewidth(0.8 if product_plot else 0.4)
                        patch.set_zorder(3)
                        if product_plot:
                            patch.set_alpha(0.92)

                legend = ax_right.get_legend()
                if legend is not None:
                    legend.remove()

                for container in ax_right.containers:
                    ax_right.bar_label(
                        container,
                        fmt=lambda v: f"{v:.2f}x" if v > 0 else "",
                        label_type="edge",
                        padding=4 if product_plot else 2,
                        fontsize=annotation_fontsize,
                        fontweight="semibold" if product_plot else "bold",
                    )

                if show_legend:
                    _speedup_system_order = ["bespoke", "umbra", "duckdb"]

                    def _speedup_system_sort_key(s):
                        return (
                            _speedup_system_order.index(s)
                            if s in _speedup_system_order
                            else len(_speedup_system_order),
                            s,
                        )

                    color_handles = [
                        mpatches.Patch(
                            facecolor=SYSTEM_COLORS.get(s, "#888888"),
                            edgecolor="white",
                            linewidth=0.4,
                            label=s,
                        )
                        for s in sorted(
                            comp_meta["system"].str.lower().unique(),
                            key=_speedup_system_sort_key,
                        )
                    ]
                    hatch_handles = [
                        mpatches.Patch(
                            facecolor="#dddddd",
                            edgecolor=JOURNAL_EDGE_BLUE,
                            linewidth=0.5,
                            hatch=sf_hatches[sf],
                            label=sf_fmt(sf),
                        )
                        for sf in sfs
                    ]
                    _pad = mpatches.Patch(visible=False, label="")
                    _ncol = 1 + max(len(color_handles), len(hatch_handles))
                    handles = (
                        [mpatches.Patch(visible=False, label="system:")]
                        + color_handles
                        + [_pad] * (_ncol - 1 - len(color_handles))
                        + [mpatches.Patch(visible=False, label="scale factor:")]
                        + hatch_handles
                        + [_pad] * (_ncol - 1 - len(hatch_handles))
                    )
                    speedup_legend_kwargs = {
                        "loc": "upper center"
                        if legend_pos == "bottom"
                        else "lower center",
                        "bbox_to_anchor": (0.5, -0.08)
                        if legend_pos == "bottom"
                        else (0.5, 0.97),
                    }
                    if legend_pos == "up":
                        speedup_legend_kwargs["bbox_transform"] = fig.transFigure
                    _deferred_legends.append(
                        {
                            "ax": ax_right,
                            "add_artist": False,
                            "call_kwargs": dict(
                                handles=handles,
                                ncol=_ncol,
                                frameon=False,
                                fontsize=legend_fontsize,
                                columnspacing=1.0 if product_plot else 0.8,
                                handletextpad=0.5 if product_plot else 0.3,
                                **speedup_legend_kwargs,
                            ),
                        }
                    )
            ax_right.axhline(
                1.0,
                linestyle="--",
                color="#aaaaaa",
                linewidth=1.4 if product_plot else 0.8,
                label="baseline",
                zorder=2,
            )
            ax_right.set_xlabel(
                "Number of Threads",
                fontsize=label_fontsize,
                fontweight=axis_label_weight,
            )
            if wi == 0 and not show_runtime:
                ax_right.set_ylabel(
                    "Bespoke Speedup" if product_plot else "Speedup of Bespoke",
                    fontsize=label_fontsize,
                    fontweight=axis_label_weight,
                )
            else:
                ax_right.set_ylabel("")
            ax_right.tick_params(axis="both", **tick_params)
            ax_right.grid(
                axis="y",
                alpha=grid_alpha,
                linestyle="-",
                linewidth=grid_linewidth,
                zorder=1,
            )
            ax_right.set_axisbelow(True)
            for spine in ax_right.spines.values():
                spine.set_visible(True)
            if product_plot:
                ax_right.set_facecolor("#fbfcff")
                ax_right.spines["left"].set_color("#c6ccd6")
                ax_right.spines["bottom"].set_color("#c6ccd6")

        # Label the workload above the first axis of this group when there are
        # multiple workloads so the reader can tell the subplots apart.
        if n_workloads > 1:
            axes_flat[col_start].set_title(
                _WORKLOAD_DISPLAY_NAMES.get(workload.lower(), workload),
                fontsize=label_fontsize,
                fontweight="semibold" if product_plot else "bold",
            )

    if title:
        fig.suptitle(
            title,
            y=0.98 if product_plot else 1.01,
            fontsize=22 if product_plot else 12,
            fontweight="semibold" if product_plot else "bold",
        )
    fig.tight_layout()
    # Place legends after tight_layout so figure-space bbox_to_anchor values are
    # not mistaken for axes-space coordinates during layout computation.
    for ld in _deferred_legends:
        lg = ld["ax"].legend(**ld["call_kwargs"])
        if lg.get_title().get_text():
            lg.get_title().set_fontweight("bold")
        if ld["add_artist"]:
            ld["ax"].add_artist(lg)
    plt.savefig(output, dpi=300, bbox_inches="tight", facecolor="white")

    return fig


def _annotate_total_time_points_without_collisions(
    fig, ax, points, *, fontsize: int = 8
) -> None:
    """Place marker value labels at a fixed offset (collision avoidance disabled)."""
    for point in points:
        ax.annotate(
            point["text"],
            xy=(point["x"], point["y"]),
            xytext=(0, 5),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=fontsize,
            fontweight="bold",
            zorder=10,
        )
