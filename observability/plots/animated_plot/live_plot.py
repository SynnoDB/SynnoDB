from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
from PIL import Image

from observability.plots.animated_plot.config import (
    COMBINED_HISTORY_COLUMNS,
    COMBINED_HISTORY_CSV_PATH,
)
from observability.plots.animated_plot.demo_timeline_engine import (
    DemoTimelineEngine,
    FrameViewportConfig,
    XAxisMode,
)
from observability.plots.animated_plot.terminal_input_builder import (
    build_terminal_turns,
)
from observability.plots.plot_timeline import PlotConfig
from observability.plots.utils.wandb_utils import combine_histories, get_wandb_stats


@dataclass
class TimelinePlotRenderer:
    engine: DemoTimelineEngine
    viewport: FrameViewportConfig
    dpi: int = 100
    cache: dict[tuple[int, int, int], Image.Image] = field(default_factory=dict)

    def _build_plot_config(self, width: int, height: int) -> PlotConfig:
        return PlotConfig(
            left_axis_series=["input_tokens"],
            right_axis_series=["code_size"],
            right_axis2_series=["speedup"],
            highlight_correction_span=True,
            figsize=(width / self.dpi, height / self.dpi),
            legend_y_offset=1.22,
        )

    def render(self, history_index: int, width: int, height: int) -> Image.Image:
        key = (history_index, width, height)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        history_end = len(self.engine.history) - 1
        current_idx = max(0, min(history_index, history_end))
        x_min, x_max = self.engine._frame_turn_range(
            0,
            history_end,
            current_idx,
            self.viewport,
        )
        config = self._build_plot_config(width, height)
        config.turn_start = x_min
        config.turn_end = current_idx

        fig, _ = self.engine.plot(config)
        fig.subplots_adjust(
            left=0.09,
            right=0.90,
            bottom=0.28,
            top=0.86,
        )
        for axis in fig.axes:
            axis.set_xlim(left=x_min, right=max(x_max, x_min + 1))
            axis.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
            axis.xaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))
            axis.tick_params(axis="x", pad=4)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            dpi=self.dpi,
            facecolor="white",
            bbox_inches="tight",
            pad_inches=0.02,
        )
        plt.close(fig)
        buf.seek(0)
        rendered = Image.open(buf).convert("RGB")
        if rendered.size != (width, height):
            rendered = rendered.resize((width, height))
        self.cache[key] = rendered
        return rendered


def load_turns_from_wandb(
    run_ids: list[str],
    wandb_run_cache_path: Path,
    skip_cache: bool = True,
    viewport: FrameViewportConfig | None = None,
) -> tuple[list[dict[str, Any]], TimelinePlotRenderer]:
    histories: list[pd.DataFrame] = []
    summary = None

    for run_id in run_ids:
        summary, history, _ = get_wandb_stats(
            run_id,
            skip_cache=skip_cache,
            wandb_run_cache_path=wandb_run_cache_path,
        )
        histories.append(history)

    combined_history = combine_histories(histories)
    csv_columns = [
        col for col in COMBINED_HISTORY_COLUMNS if col in combined_history.columns
    ]
    combined_history.loc[:, csv_columns].to_csv(COMBINED_HISTORY_CSV_PATH, index=True)
    print(f"Wrote combined history CSV to {COMBINED_HISTORY_CSV_PATH}")
    turns = build_terminal_turns(combined_history)
    viewport = viewport or FrameViewportConfig(
        mode=XAxisMode.SLIDING,
        sliding_window_turns=100,
        min_visible_turns=25,
    )
    plot_renderer = TimelinePlotRenderer(
        engine=DemoTimelineEngine(
            combined_history, summary, drill_down_to_query_level=True
        ),
        viewport=viewport,
    )
    return turns, plot_renderer


def enrich_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    elapsed = 0.0
    cumulative_cost = 0.0

    for i, turn in enumerate(turns):
        if "duration_sec" not in turn:
            raise ValueError(f"Turn index {i} missing duration_sec")
        if "turn" not in turn:
            turn["turn"] = i + 1
        if "log_lines" not in turn:
            turn["log_lines"] = []
        if "current_prompt" not in turn:
            turn["current_prompt"] = ""

        elapsed += float(turn["duration_sec"])
        if "elapsed_sec" not in turn:
            turn["elapsed_sec"] = elapsed

        if "cumulative_cost" in turn:
            cumulative_cost = float(turn["cumulative_cost"])
        else:
            cumulative_cost += float(turn.get("cost_usd", 0.0))
            turn["cumulative_cost"] = cumulative_cost

    return turns
