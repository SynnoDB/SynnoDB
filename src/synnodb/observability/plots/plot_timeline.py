"""
Timeline Plot Engine for W&B experiment data visualization.

Provides a modular, configurable plotting system for analyzing LLM agent optimization runs.
"""

# add parent to path
import random
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

from synnodb.observability.plots.utils.annotate_speedup_col import (
    annotate_total_speedup_per_turn,
)
from synnodb.observability.plots.utils.wandb_trace_preprocessor import (
    SECTION_RULES,
    DataCleaner,
    WorkedOnSpan,
)
from synnodb.observability.plots.utils.wandb_utils import get_wandb_stats

warnings.filterwarnings("ignore")

# Publication-quality rcParams (mirrors journal_page1.ipynb)
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


# ============================================================================
# Plotting Engine
# ============================================================================

# Series color and marker mappings — muted, print-safe palette
SERIES_STYLES = {
    "input_tokens": {
        "color": "#8B8B8B",
        "axis_label_color": "#1D1D1D",
        # "marker": "o",
        "label": "Context Size (# Tokens)",
    },
    "reasoning_tokens": {
        "color": "#E07A5F",
        "marker": "s",
        "label": "Reasoning Tokens",
    },
    "cached_tokens": {
        "color": "#6B4C93",
        "marker": "^",
        "label": "Cached Tokens",
    },
    "speedup": {
        "color": "#398caf",
        # "marker": "D",
        "label": "Total Speedup",
    },
    "code_size": {
        "color": "#FF7700",
        # "marker": "v",
        "label": "Code Size\n(# Lines of Code)",
    },
}

# Categorical palette for stage spans (colorblind-friendly, ordered)
_SPAN_PALETTE = [
    "#4C72B0",
    "#DD8452",
    "#55A868",
    "#C44E52",
    "#8172B3",
    "#937860",
    "#DA8BC3",
    "#8C8C8C",
]


@dataclass
class PlotConfig:
    """Configuration for timeline plot rendering."""

    left_axis_series: List[str] = field(
        default_factory=lambda: ["input_tokens", "reasoning_tokens"]
    )
    right_axis_series: List[str] = field(default_factory=list)
    right_axis2_series: List[str] = field(default_factory=list)
    highlight_correction_span: bool = True
    highlight_worked_on: bool = True

    left_ylabel: Optional[str] = None

    figsize: Tuple[int, int] = (14, 6)
    grid_alpha: float = 0.3
    error_span_color: str = "#ff6b6b"
    error_span_alpha: float = 1

    turn_start: Optional[int] = None
    turn_end: Optional[int] = None

    legend_y_offset: Optional[float] = None

    query_row_ymin: float = -0.14
    query_row_height: float = 0.05
    row_gap: float = 0.03
    correctness_row_height: float = 0.05
    x_tick_pad: float = 30
    x_label_pad: float = 52


class TimelineEngine:
    """Main plotting engine for timeline visualization."""

    def __init__(
        self,
        history: pd.DataFrame,
        summary: Optional[Dict] = None,
        drill_down_to_query_level: bool = False,
        cmp_to: str = "duckdb",
    ):
        """
        Initialize the timeline engine.

        Args:
            history: W&B history dataframe
            summary: W&B summary dict (optional)
            cmp_to: Baseline system to compute speedups against ("duckdb" or
                "umbra"). Speedups are computed as baseline_ms / impl_ms.
        """
        self.history = history
        self.summary = summary or {}
        self.drill_down_to_query_level = drill_down_to_query_level
        self.cmp_to = cmp_to

        # Precompute data transformations
        self.error_spans = DataCleaner.extract_error_spans(history)
        self.queries_implemented = DataCleaner.extract_queries_implemented(history)
        self.worked_on_queries = DataCleaner.extract_worked_on_queries(
            history, drill_down_to_query_level=drill_down_to_query_level
        )

        self.worked_on_spans = DataCleaner.extract_worked_on_spans(
            self.worked_on_queries
        )

        # self.code_size = DataCleaner.extract_code_size(history)
        self.code_size = history["code/loc"] if "code/loc" in history.columns else None

        # append code_size to history for plotting
        if self.code_size is not None:
            self.history["code_size"] = self.code_size

        # annotate speedup at current turn
        self.speedup_annotation_col_name = annotate_total_speedup_per_turn(
            self.history, cmp_to=self.cmp_to
        )

    def _series_label(self, series: str) -> str:
        """Display label for a series, annotating the speedup baseline system."""
        base = SERIES_STYLES.get(series, {}).get("label", series)
        if series == "speedup":
            return f"{base} (vs. {self.cmp_to.capitalize()})"
        return base

    def _get_color_map(self, spans: List[WorkedOnSpan]) -> Dict[str, Any]:
        """Create a consistent color mapping keyed by section label."""
        # Color by section so the span background is stable regardless of which
        # queries are active within that section.
        unique_sections = list(
            set(
                span.section or span.queries
                for span in spans
                if (span.section or span.queries)
            )
        )

        return {
            section: _SPAN_PALETTE[i % len(_SPAN_PALETTE)]
            for i, section in enumerate(sorted(unique_sections))
        }

    def _apply_turn_filter(self, config: PlotConfig) -> Tuple[int, int]:
        """Apply turn filtering and return the valid range."""
        start = 0 if config.turn_start is None else config.turn_start
        end = len(self.history) - 1 if config.turn_end is None else config.turn_end
        return max(0, start), min(len(self.history) - 1, end)

    def _plot_series(
        self,
        ax: plt.Axes,  # type: ignore
        config: PlotConfig,
        turn_start: int,
        turn_end: int,
    ) -> Tuple[Optional[plt.Axes], Optional[plt.Axes]]:  # type: ignore
        """Plot series on left and (up to two) right axes."""
        plot_data = self.history.iloc[turn_start : turn_end + 1]

        # Plot left axis series
        for series in config.left_axis_series:
            if series not in self.history.columns:
                continue
            data = plot_data[["turn", series]].dropna()
            if len(data) > 0:
                style = SERIES_STYLES.get(series, {})
                ax.plot(
                    data["turn"],
                    data[series],
                    marker=style.get("marker", None),
                    markersize=3,
                    label=style.get("label", series),
                    color=style.get("color", "#808080"),
                    linewidth=1.4,
                )

        # Determine left y-axis label and color
        single_left = (
            len([s for s in config.left_axis_series if s in self.history.columns]) == 1
        )
        first_series = next(
            (s for s in config.left_axis_series if s in self.history.columns), None
        )
        if config.left_ylabel is not None:
            left_label = config.left_ylabel
        elif first_series is not None:
            left_label = SERIES_STYLES.get(first_series, {}).get("label", first_series)
        else:
            left_label = "Value"
        if single_left and first_series:
            _s = SERIES_STYLES.get(first_series, {})
            left_color = _s.get("axis_label_color", _s.get("color", "black"))
        else:
            left_color = "black"

        ax.set_xlabel(
            "Turns (includes LLM & Tool calls)", fontsize=12, fontweight="bold"
        )
        ax.xaxis.labelpad = config.x_label_pad
        ax.set_ylabel(left_label, fontsize=12, fontweight="bold", color=left_color)
        ax.tick_params(axis="y", labelcolor=left_color)
        ax.set_axisbelow(True)
        ax.grid(True, axis="x", alpha=config.grid_alpha, linestyle="-", linewidth=0.5)
        ax.set_ylim(bottom=0)
        ax.set_xlim(left=0, right=len(self.history) - 1)
        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(
                lambda x, _: f"{x / 1000:.0f}k" if x >= 1000 else f"{x:.0f}"
            )
        )
        ax.xaxis.set_major_formatter(
            mticker.FuncFormatter(lambda x, _: f"{int(x):,}".replace(",", "."))
        )

        # Plot right axis series
        assert len(config.right_axis_series) <= 1, (
            f"right_axis_series must have 0 or 1 elements, got {len(config.right_axis_series)}: {config.right_axis_series}"
        )
        assert len(config.right_axis2_series) <= 1, (
            f"right_axis2_series must have 0 or 1 elements, got {len(config.right_axis2_series)}: {config.right_axis2_series}"
        )
        if not config.right_axis_series and not config.right_axis2_series:
            return None, None

        ax2 = ax.twinx() if config.right_axis_series else None
        if ax2 is not None:
            ax2.grid(False)

        for series in config.right_axis_series:
            style = SERIES_STYLES.get(series, {})
            if series == "speedup":
                col_name = self.speedup_annotation_col_name
                if col_name is None:
                    continue
                color = style.get("color")
                label_color = style.get("axis_label_color", color)
                series_label = self._series_label(series)
                assert ax2 is not None
                ax2.set_ylabel(
                    series_label,
                    fontsize=12,
                    fontweight="bold",
                    color=label_color,
                )
                ax2.tick_params(axis="y", labelcolor=label_color)
                data = plot_data[["turn", col_name]].dropna()
                if len(data) == 0:
                    continue
                ax2.plot(
                    data["turn"],
                    data[col_name],
                    marker=style.get("marker", None),
                    markersize=4,
                    label=series_label,
                    color=color,
                    linewidth=2,
                )
                ax2.tick_params(axis="y", labelcolor=label_color)
            else:
                if series not in self.history.columns:
                    continue
                if "color" not in style:
                    rand_int = random.randint(0, len(mcolors.TABLEAU_COLORS) - 1)
                    color = mcolors.TABLEAU_COLORS[
                        list(mcolors.TABLEAU_COLORS.keys())[rand_int]
                    ]
                else:
                    color = style.get("color", "#808080")
                label_color = style.get("axis_label_color", color)
                assert ax2 is not None
                ax2.set_ylabel(
                    style.get("label", series),
                    fontsize=12,
                    fontweight="bold",
                    color=label_color,
                )
                ax2.tick_params(axis="y", labelcolor=label_color)
                data = plot_data[["turn", series]].dropna()
                if len(data) > 0:
                    ax2.plot(
                        data["turn"],
                        data[series],
                        marker=style.get("marker", None),
                        markersize=3,
                        label=style.get("label", series),
                        color=color,
                        linewidth=2,
                    )

            assert ax2 is not None
            ax2.set_ylim(bottom=0)
            ax2.set_xlim(left=0, right=len(self.history) - 1)
            ax2.yaxis.set_major_formatter(
                mticker.FuncFormatter(
                    lambda x, _: f"{x / 1000:.0f}k" if x >= 1000 else f"{x:.0f}"
                )
            )

        # Second right axis (offset further right)
        ax3: Optional[plt.Axes] = None  # type: ignore
        if config.right_axis2_series:
            ax3 = ax.twinx()
            ax3.grid(False)
            ax3.spines["right"].set_position(("outward", 65))

            for series in config.right_axis2_series:
                style = SERIES_STYLES.get(series, {})
                if series == "speedup":
                    col_name = self.speedup_annotation_col_name
                    if col_name is None:
                        continue
                    color = style.get("color")
                    label_color = style.get("axis_label_color", color)
                    series_label = self._series_label(series)
                    ax3.set_ylabel(
                        series_label,
                        fontsize=12,
                        fontweight="bold",
                        color=label_color,
                    )
                    ax3.tick_params(axis="y", labelcolor=label_color)
                    data = plot_data[["turn", col_name]].dropna()
                    if len(data) == 0:
                        continue
                    ax3.plot(
                        data["turn"],
                        data[col_name],
                        marker=style.get("marker", None),
                        markersize=4,
                        label=series_label,
                        color=color,
                        linewidth=2,
                    )
                else:
                    if series not in self.history.columns:
                        continue
                    color = style.get("color", "#808080")
                    label_color = style.get("axis_label_color", color)
                    ax3.set_ylabel(
                        style.get("label", series),
                        fontsize=12,
                        fontweight="bold",
                        color=label_color,
                    )
                    ax3.tick_params(axis="y", labelcolor=label_color)
                    data = plot_data[["turn", series]].dropna()
                    if len(data) > 0:
                        ax3.plot(
                            data["turn"],
                            data[series],
                            marker=style.get("marker", None),
                            markersize=3,
                            label=style.get("label", series),
                            color=color,
                            linewidth=2,
                        )

                ax3.set_ylim(bottom=0)
                ax3.set_xlim(left=0, right=len(self.history) - 1)
                ax3.yaxis.set_major_locator(mticker.MaxNLocator(nbins=5))
                ax3.yaxis.set_major_formatter(
                    mticker.FuncFormatter(lambda x, _: f"{x:.1f}")
                )

        return ax2, ax3

    def _plot_worked_on_spans(
        self,
        ax: plt.Axes,  # type: ignore
        config: PlotConfig,
        turn_start: int,
        turn_end: int,
    ) -> None:
        """Color chart background by section and annotate above the plot."""
        if not config.highlight_worked_on:
            return

        color_map = self._get_color_map(self.worked_on_spans)

        # Build merged section spans: consecutive spans sharing the same section
        # are collapsed into one region so stage boundaries are clean.
        merged_sections: List[Dict] = []
        for span in self.worked_on_spans:
            key = span.section or span.queries
            if merged_sections and merged_sections[-1]["key"] == key:
                merged_sections[-1]["end"] = span.end
            else:
                merged_sections.append(
                    {"key": key, "start": span.start, "end": span.end}
                )

        # Estimate character width in data (turn) units for overlap detection.
        data_range = max(turn_end - turn_start + 1, 1)
        chars_per_turn = data_range / (config.figsize[0] * 8.5)

        # Build a mapping from section key to display label using SectionRule.display_label
        _section_display = {
            rule.label: (rule.display_label or rule.label) for rule in SECTION_RULES
        }

        # Use two staggered rows so every section label is always visible
        label_rows = [1.01, 1.04]
        last_label_right_per_row = [-float("inf")] * len(label_rows)

        for sec in merged_sections:
            if sec["end"] < turn_start or sec["start"] > turn_end:
                continue

            span_start = max(sec["start"], turn_start)
            span_end = min(sec["end"], turn_end)
            color = color_map.get(sec["key"], "#808080")
            mid = (span_start + span_end) / 2

            # Fill the full chart background for this section
            ax.axvspan(
                span_start,
                span_end,
                ymin=0,
                ymax=1,
                alpha=0.15,
                color=color,
                zorder=0,
                linewidth=0,
            )

            # Vertical separator at the section boundary (skip the very first)
            if span_start > turn_start:
                ax.axvline(
                    x=span_start,
                    color=color,
                    linestyle="--",
                    linewidth=1.5,
                    alpha=0.8,
                    zorder=1,
                )

            # Prominent baseline-change marker at the start of "add mt":
            # speedup beyond this point compares single-threaded bespoke
            # against a multi-threaded baseline, so it drops.
            if sec["key"] == "add mt" and span_start > turn_start:
                _mt_line_top = 1.13
                ax.plot(
                    [span_start, span_start],
                    [0, _mt_line_top],
                    color="black",
                    linestyle="--",
                    linewidth=1.5,
                    alpha=0.8,
                    transform=ax.get_xaxis_transform(),
                    clip_on=False,
                    zorder=3,
                )
                ax.text(
                    span_start,
                    _mt_line_top + 0.015,
                    "switch to multi-threaded →",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                    fontstyle="italic",
                    color="black",
                    transform=ax.get_xaxis_transform(),
                    zorder=4,
                    clip_on=False,
                )

            # Annotation above the chart — place in the first row that fits,
            # fall back to the row with the most available space so the label
            # is always shown.
            raw_label = _section_display.get(sec["key"], sec["key"])
            assert raw_label is not None
            label = raw_label if len(raw_label) <= 40 else f"{raw_label[:40]}..."

            max_len = max([len(l) for l in label.split("\n")])
            label_half_width = max_len / 2 * chars_per_turn

            chosen_row = None
            for row_idx, row_y in enumerate(label_rows):
                if mid - label_half_width >= last_label_right_per_row[row_idx]:
                    chosen_row = row_idx
                    break
            if chosen_row is None:
                # All rows overlap — pick the one whose last label ends earliest
                chosen_row = int(
                    min(
                        range(len(label_rows)),
                        key=lambda r: last_label_right_per_row[r],
                    )
                )

            ax.text(
                mid,
                label_rows[chosen_row],
                label,
                ha="center",
                va="bottom",
                fontsize=9,
                color=color,
                fontweight="bold",
                transform=ax.get_xaxis_transform(),
                clip_on=False,
            )
            last_label_right_per_row[chosen_row] = mid + label_half_width

        # --- Per-query row below chart ---
        has_query_row = self.drill_down_to_query_level and any(
            span.queries for span in self.worked_on_spans
        )
        if has_query_row:
            y_min = config.query_row_ymin
            y_max = config.query_row_ymin + config.query_row_height

            ax.text(
                -0.005,
                (y_min + y_max) / 2,
                "Query:",
                ha="right",
                va="center",
                fontsize=10,
                fontstyle="italic",
                color="#000000",
                transform=ax.transAxes,
                clip_on=False,
            )
            last_query_label_right = -float("inf")
            for span in self.worked_on_spans:
                if not span.queries:
                    continue
                if span.end < turn_start or span.start > turn_end:
                    continue

                span_start = max(span.start, turn_start)
                span_end = min(span.end, turn_end)
                color = color_map.get(span.section or span.queries, "#808080")
                mid = (span_start + span_end) / 2
                query_label = (
                    span.queries
                    if len(span.queries) <= 20
                    else f"{span.queries[:20]}..."
                )

                ax.axvspan(
                    span_start,
                    span_end,
                    ymin=y_min,
                    ymax=y_max,
                    alpha=0.4,
                    color=color,
                    clip_on=False,
                    transform=ax.get_xaxis_transform(),
                    zorder=0,
                )
                label_half_width = len(query_label) / 2 * chars_per_turn
                if mid - label_half_width >= last_query_label_right:
                    ax.text(
                        mid,
                        (y_min + y_max) / 2,
                        query_label,
                        ha="center",
                        va="center",
                        fontsize=8,
                        transform=ax.get_xaxis_transform(),
                        zorder=2,
                    )
                    last_query_label_right = mid + label_half_width

        # if has_query_row:
        #     ax.xaxis.labelpad = 50
        ax.tick_params(
            axis="x",
            pad=config.x_tick_pad,
            # zorder=3,
        )

    def _plot_error_spans(
        self,
        ax: plt.Axes,  # type: ignore
        config: PlotConfig,
        turn_start: int,
        turn_end: int,
    ) -> None:
        """Plot correctness row: green background with red error spans, labelled on the left."""
        if not config.highlight_correction_span:
            return

        correctness_row_ymin: float = (
            config.query_row_ymin - config.query_row_height - config.row_gap
        )

        _YMIN = correctness_row_ymin
        _YMAX = correctness_row_ymin + config.correctness_row_height
        _kw = dict(
            clip_on=False, transform=ax.get_xaxis_transform(), linewidth=0, zorder=2
        )

        # Green background for the full turn range
        ax.axvspan(
            turn_start,
            turn_end,
            ymin=_YMIN,
            ymax=_YMAX,
            color="#4caf50",
            alpha=1.0,
            **_kw,
        )

        # Red overlay for each error span
        for error_span in self.error_spans:
            if error_span.end < turn_start or error_span.start > turn_end:
                continue
            span_start = max(error_span.start, turn_start)
            span_end = min(error_span.end, turn_end)
            ax.axvspan(
                span_start,
                span_end,
                ymin=_YMIN,
                ymax=_YMAX,
                color=config.error_span_color,
                alpha=config.error_span_alpha,
                **_kw,
            )

        # "Correctness" label to the left — same style as "Query:" label
        ax.text(
            -0.005,
            (_YMIN + _YMAX) / 2,
            "Correctness:",
            ha="right",
            va="center",
            fontsize=10.5,
            fontstyle="italic",
            color="#000000",
            transform=ax.transAxes,
            clip_on=False,
        )

    def plot(
        self, config: Optional[PlotConfig] = None, save_path: Optional[str] = None
    ) -> Tuple[plt.Figure, plt.Axes]:  # type: ignore
        """
        Generate the timeline plot with given configuration.

        Args:
            config: PlotConfig object with plot settings
            save_path: Optional path to save figure

        Returns:
            Tuple of (figure, axes)
        """
        config = config or PlotConfig()
        turn_start, turn_end = self._apply_turn_filter(config)

        fig, ax = plt.subplots(figsize=config.figsize)

        # Plot series on axes; capture ax2/ax3 so we can merge their legend entries
        ax2, ax3 = self._plot_series(ax, config, turn_start, turn_end)

        # Plot worked-on spans
        self._plot_worked_on_spans(ax, config, turn_start, turn_end)

        # Plot error spans
        self._plot_error_spans(ax, config, turn_start, turn_end)

        # Remove top and right spines for a cleaner academic look
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        # Merge legend handles from all axes
        handles, labels = ax.get_legend_handles_labels()
        if ax2 is not None:
            h2, l2 = ax2.get_legend_handles_labels()
            handles += h2
            labels += l2
        if ax3 is not None:
            h3, l3 = ax3.get_legend_handles_labels()
            handles += h3
            labels += l3

        has_spans = config.highlight_worked_on and bool(self.worked_on_spans)
        if has_spans:
            legend_y = -0.2
        else:
            legend_y = -0.13

        # prune \n from labels for cleaner legend
        labels = [label.replace("\n", " ") for label in labels]

        ax.legend(
            handles=handles,
            labels=labels,
            loc="upper center",
            bbox_to_anchor=(
                0.5,
                legend_y if config.legend_y_offset is None else config.legend_y_offset,
            ),
            ncol=len(handles),
            frameon=False,
            fontsize=10,
        )

        plt.tight_layout(pad=0)

        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches="tight", facecolor="white")
            print(f"✓ Plot saved to {save_path}")

        return fig, ax

    def get_statistics(self) -> Dict[str, Any]:
        """Get summary statistics from the data."""
        stats = {
            "total_turns": len(self.history),
            "num_error_spans": len(self.error_spans),
            "queries_implemented": int(
                self.queries_implemented.num_implemented.iloc[-1]
            )
            if len(self.queries_implemented.num_implemented) > 0
            else 0,
            "total_input_tokens": int(
                self.history["input_tokens"].sum()
                if "input_tokens" in self.history.columns
                else 0
            ),
            "total_reasoning_tokens": int(
                self.history["reasoning_tokens"].sum()
                if "reasoning_tokens" in self.history.columns
                else 0
            ),
            "num_worked_on_spans": len(self.worked_on_spans),
        }
        return stats


if __name__ == "__main__":
    summary, history, config = get_wandb_stats("3nvieip0")
    assert history is not None, "Failed to load history from W&B"
    engine = TimelineEngine(history, summary)
    engine.plot()
