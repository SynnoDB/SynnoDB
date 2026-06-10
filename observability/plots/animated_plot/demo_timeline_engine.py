from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import matplotlib.pyplot as plt

from observability.plots.plot_timeline import PlotConfig, TimelineEngine


class XAxisMode(str, Enum):
    FULL = "full"
    GROWING = "growing"
    SLIDING = "sliding"


@dataclass(frozen=True)
class FrameViewportConfig:
    mode: XAxisMode = XAxisMode.FULL
    sliding_window_turns: int = 100
    min_visible_turns: int = 25

    def validate(self) -> None:
        if self.mode is XAxisMode.SLIDING and self.sliding_window_turns <= 0:
            raise ValueError("sliding_window_turns must be > 0 for sliding mode")
        if self.min_visible_turns <= 0:
            raise ValueError("min_visible_turns must be > 0")


class DemoTimelineEngine(TimelineEngine):
    def _frame_turn_range(
        self,
        base_turn_start: int,
        base_turn_end: int,
        current_turn: int,
        viewport: FrameViewportConfig,
    ) -> tuple[int, int]:
        if viewport.mode is XAxisMode.FULL:
            return base_turn_start, base_turn_end
        if viewport.mode is XAxisMode.GROWING:
            return base_turn_start, current_turn

        available_turns = current_turn - base_turn_start + 1
        if available_turns < viewport.min_visible_turns:
            return base_turn_start, current_turn

        window_start = max(
            base_turn_start, current_turn - viewport.sliding_window_turns + 1
        )
        return window_start, current_turn

    def export_frames(
        self,
        config: PlotConfig,
        output_dir: str = "frames",
        filename_template: str = "frame_%06d.png",
        dpi: int = 300,
        viewport: FrameViewportConfig = FrameViewportConfig(),
        use_tight_bbox: bool = False,
    ) -> list[Path]:
        viewport.validate()
        base_turn_start, base_turn_end = self._apply_turn_filter(config)

        out_path = Path(output_dir)
        if out_path.exists():
            shutil.rmtree(out_path)
        out_path.mkdir(parents=True, exist_ok=True)

        written_frames: list[Path] = []
        total_frames = base_turn_end - base_turn_start + 1

        for current_turn in range(base_turn_start, base_turn_end + 1):
            x_min, x_max = self._frame_turn_range(
                base_turn_start, base_turn_end, current_turn, viewport
            )
            frame_config = PlotConfig(
                left_axis_series=list(config.left_axis_series),
                right_axis_series=list(config.right_axis_series),
                right_axis2_series=list(config.right_axis2_series),
                highlight_correction_span=config.highlight_correction_span,
                highlight_worked_on=config.highlight_worked_on,
                left_ylabel=config.left_ylabel,
                figsize=config.figsize,
                grid_alpha=config.grid_alpha,
                error_span_color=config.error_span_color,
                error_span_alpha=config.error_span_alpha,
                turn_start=x_min,
                turn_end=current_turn,
                legend_y_offset=config.legend_y_offset,
            )

            fig, ax = self.plot(frame_config)
            for axis in fig.axes:
                axis.set_xlim(left=x_min, right=max(x_max, x_min + 1))

            fig.tight_layout()
            frame_path = out_path / (filename_template % current_turn)
            savefig_kwargs = {
                "dpi": dpi,
                "facecolor": "white",
            }
            if use_tight_bbox:
                savefig_kwargs["bbox_inches"] = "tight"
            fig.savefig(frame_path, **savefig_kwargs)
            plt.close(fig)
            written_frames.append(frame_path)

            rendered = current_turn - base_turn_start + 1
            if rendered % 50 == 0 or current_turn == base_turn_end:
                print(f"Rendered {rendered}/{total_frames} frames")

        return written_frames
