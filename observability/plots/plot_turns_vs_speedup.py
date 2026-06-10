"""
Plot: Turns vs Speedup

Shows speedup progression over turns for multiple W&B runs.
Each run is a separate series (line + dot markers).
Legend shows model + date.
"""

import sys
import warnings
from pathlib import Path
from typing import List, Optional, Tuple

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

sys.path.append(Path(__file__).parent.parent.parent.as_posix())

from observability.plots.utils.annotate_speedup_col import (
    annotate_total_speedup_per_turn,
)
from observability.plots.utils.wandb_utils import get_wandb_stats

warnings.filterwarnings("ignore")

# Match plot_timeline.py rcParams
plt.rcParams["figure.dpi"] = 300
plt.rcParams["font.size"] = 11
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.linewidth"] = 1.2
plt.rcParams["xtick.major.width"] = 2
plt.rcParams["ytick.major.width"] = 1.2
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.3
plt.rcParams["grid.linestyle"] = "-"
plt.rcParams["grid.linewidth"] = 0.5

# Colorblind-friendly palette (mirrors _SPAN_PALETTE in plot_timeline.py)
_PALETTE = [
    "#4C72B0",
    "#DD8452",
    "#55A868",
    "#C44E52",
    "#8172B3",
    "#937860",
    "#DA8BC3",
    "#8C8C8C",
]


def _strip_model_prefix(model: str) -> str:
    """Strip 'anthropic/claude-', 'anthropic/', or 'claude-' prefix from model name."""
    lower = model.lower()
    for prefix in ["anthropic/claude-", "anthropic/", "claude-"]:
        if lower.startswith(prefix):
            return model[len(prefix) :]
    return model


def _extract_label(
    config: dict, summary: dict, history: pd.DataFrame, run_id: str
) -> str:
    """Build legend label: '<model> | <date> | $<cost> [run_id]'."""
    model = None
    for key in ["model", "llm_model", "model_name", "agent_model", "llm"]:
        if key in config and config[key]:
            model = str(config[key])
            break
    if model is None:
        model = run_id
    model = _strip_model_prefix(model)

    date_str = ""
    if "_timestamp" in history.columns:
        ts = pd.to_numeric(history["_timestamp"], errors="coerce").dropna()
        if len(ts) > 0:
            date_str = pd.to_datetime(ts.iloc[0], unit="s").strftime("%Y-%m-%d")

    cost_str = ""
    cost_val = summary.get("total/cost_usd")
    if cost_val is not None:
        try:
            cost_str = f"${float(cost_val):.2f}"
        except (ValueError, TypeError):
            pass

    ingest_str = ""
    ingest_val = summary.get("validation/ingest_time_ms")
    if ingest_val is not None:
        try:
            ingest_ms = float(ingest_val)
            ingest_str = f"{ingest_ms / 1000:.1f}s ingest"
        except (ValueError, TypeError):
            pass

    parts = [model]
    if date_str:
        parts.append(date_str)
    if cost_str:
        parts.append(cost_str)
    if ingest_str:
        parts.append(ingest_str)
    parts.append(f"[{run_id}]")
    return " | ".join(parts)


def _place_annotations(fig, ax, annotations):
    """
    Place annotations without vertical overlaps using a single-pass greedy sort.
    annotations: list of (x, y, text, color)
    """
    PAD_PTS = 4

    ann_objs = []
    for x, y, txt, color in annotations:
        a = ax.annotate(
            txt,
            xy=(x, y),
            xytext=(10, 6),
            textcoords="offset points",
            fontsize=8,
            color=color,
            va="bottom",
            ha="left",
            arrowprops=dict(arrowstyle="-", color="gray", lw=0.5),
            zorder=10,
        )
        ann_objs.append(a)

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    pts_to_px = fig.dpi / 72.0

    # Sort bottom-to-top by anchor y, then greedily push each label up
    order = sorted(range(len(ann_objs)), key=lambda i: annotations[i][1])
    prev_y1_px = None
    for i in order:
        a = ann_objs[i]
        bb = a.get_window_extent(renderer)
        if prev_y1_px is not None and bb.y0 < prev_y1_px + PAD_PTS * pts_to_px:
            shift_px = prev_y1_px + PAD_PTS * pts_to_px - bb.y0
            a.xyann = (a.xyann[0], a.xyann[1] + shift_px / pts_to_px)
            prev_y1_px = bb.y1 + shift_px
        else:
            prev_y1_px = bb.y1


def plot_turns_vs_speedup(
    run_ids: List[str],
    figsize: Tuple[int, int] = (7, 4),
    save_path: Optional[str] = None,
    last_only: bool = False,
    title: Optional[str] = None,
    skip_cache: bool = False,
    cmp_to: str = "duckdb",
) -> Tuple[plt.Figure, plt.Axes]:
    """
    Plot speedup vs turns for one or more W&B runs.

    Args:
        run_ids: List of W&B run IDs to visualize as separate series.
        figsize: Figure size (width, height).
        save_path: Optional path to save the figure.
        last_only: If True, only plot the final (turn, speedup) point per run.
        cmp_to: Baseline system to compute speedups against ("duckdb" or
            "umbra"); speedups are baseline_ms / impl_ms.

    Returns:
        Tuple of (figure, axes).
    """
    fig, ax = plt.subplots(figsize=figsize)

    _pending_annotations = []  # (x, y, text, color) — deferred for overlap removal

    for i, run_id in enumerate(run_ids):
        summary, history, config = get_wandb_stats(
            run_id,
            skip_cache=skip_cache,
        )
        if history is None or len(history) == 0:
            print(f"  Skipping {run_id}: no history data")
            continue

        speedup_col = annotate_total_speedup_per_turn(history, cmp_to=cmp_to)
        if speedup_col is None:
            print(f"  Skipping {run_id}: no speedup data")
            continue

        data = history[["turn", speedup_col]].dropna()
        if len(data) == 0:
            print(f"  Skipping {run_id}: speedup column is all NaN")
            continue

        label = _extract_label(config, summary, history, run_id)
        color = _PALETTE[i % len(_PALETTE)]

        print(
            f" Last speedup: {data[speedup_col].iloc[-1]:.2f}x at turn {data['turn'].iloc[-1]} for run {run_id}"
        )

        if last_only:
            last = data.iloc[-1]
            ax.scatter(
                [last["turn"]],
                [last[speedup_col]],
                s=60,
                label=label,
                color=color,
                zorder=5,
            )
            # Build annotation from label parts (model already stripped)
            # Format: model | date | $cost | [run_id]  (date/cost optional)
            label_parts = label.split(" | ")
            inner = label_parts[:-1]  # drop [run_id] tail
            model_suffix = inner[0]
            date_part = inner[1] if len(inner) > 1 else ""
            cost_part = inner[2] if len(inner) > 2 else ""
            ingest_part = inner[3] if len(inner) > 3 else ""
            annotation_lines = [model_suffix]
            if date_part:
                annotation_lines.append(date_part)
            if cost_part:
                annotation_lines.append(cost_part)
            if ingest_part:
                annotation_lines.append(ingest_part)
            annotation = "\n".join(annotation_lines)
            _pending_annotations.append(
                (last["turn"], last[speedup_col], annotation, color)
            )
        else:
            ax.plot(
                data["turn"],
                data[speedup_col],
                marker="o",
                markersize=4,
                label=label,
                color=color,
                linewidth=1.8,
            )

    ax.set_xlabel("Turn", fontsize=12, fontweight="bold")
    ax.set_ylabel("Total Speedup", fontsize=12, fontweight="bold")
    ax.set_ylim(bottom=0)
    ax.set_xlim(left=0)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1f}x"))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_axisbelow(True)

    n_series = len(ax.get_lines()) + len(ax.collections)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        frameon=False,
        fontsize=10,
        ncol=1,
    )

    ax.set_title(title or "Speedup vs Turns", fontsize=14, fontweight="bold", pad=30)

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.28)

    # Place annotations after layout is finalized; tight_layout/subplots_adjust
    # changes transforms and can reintroduce overlaps if done earlier.
    if _pending_annotations:
        _place_annotations(fig, ax, _pending_annotations)

    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches="tight", facecolor="white")
        print(f"✓ Plot saved to {save_path}")

    return fig, ax


if __name__ == "__main__":
    run_ids = [
        "nkqryfsw",  # 3/21 sonnet
        # "6bclperc",  # 3/20 sonnet
        "eg8oes6m",  # 3/19 sonnet
        "gr1uswqm",  # 3/18 opus
        "osfnlgy4",  # 3/16 gpt-5.4
    ]

    plot_turns_vs_speedup(run_ids)
    plt.show()
