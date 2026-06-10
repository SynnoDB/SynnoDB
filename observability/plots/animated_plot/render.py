from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from observability.plots.animated_plot.config import (
    WANDB_RUN_CACHE_PATH,
    H,
    W,
    frames_dir_for_run_ids,
)
from observability.plots.animated_plot.demo_timeline_engine import (
    FrameViewportConfig,
    XAxisMode,
)
from observability.plots.animated_plot.live_plot import (
    enrich_turns,
    load_turns_from_wandb,
)
from observability.plots.animated_plot.screen_renderer import render_frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render synthetic terminal screen-recording frames from W&B history."
    )
    parser.add_argument(
        "--run-ids",
        required=True,
        dest="run_ids",
        help="Comma-separated W&B run ids, e.g. szm0buc8,h625m177,c4a8b64x",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Render only the first N output frames.",
    )
    parser.add_argument(
        "--start-turn-index",
        type=int,
        default=0,
        help="Zero-based turn index to start writing frames from. Earlier frames are kept.",
    )
    parser.add_argument(
        "--skip-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to skip the W&B cache when loading histories. Defaults to true.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes for frame rendering. Use 1 for sequential rendering.",
    )
    parser.add_argument(
        "--viewport-mode",
        choices=[mode.value for mode in XAxisMode],
        default=XAxisMode.SLIDING.value,
        help="Viewport mode for the embedded metrics plot.",
    )
    parser.add_argument(
        "--sliding-window-turns",
        type=int,
        default=100,
        help="Window size for sliding viewport mode.",
    )
    parser.add_argument(
        "--min-visible-turns",
        type=int,
        default=25,
        help="Minimum visible turns before viewport limiting kicks in.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_ids = [run_id.strip() for run_id in args.run_ids.split(",") if run_id.strip()]
    if not run_ids:
        raise ValueError("--run-ids must contain at least one run id")

    print(f"Run IDs: {', '.join(run_ids)}")
    print(f"Viewport mode: {args.viewport_mode}  sliding_window={args.sliding_window_turns}  min_visible={args.min_visible_turns}")

    viewport = FrameViewportConfig(
        mode=XAxisMode(args.viewport_mode),
        sliding_window_turns=args.sliding_window_turns,
        min_visible_turns=args.min_visible_turns,
    )

    print(f"Loading W&B histories (skip_cache={args.skip_cache})...")
    turns, plot_renderer = load_turns_from_wandb(
        run_ids=run_ids,
        wandb_run_cache_path=WANDB_RUN_CACHE_PATH,
        skip_cache=args.skip_cache,
        viewport=viewport,
    )
    print(f"Loaded {len(turns)} raw turns from W&B")

    print("Enriching turns (elapsed time, cumulative cost)...")
    turns = enrich_turns(turns)
    print(f"Enriched {len(turns)} turns")

    output_dir = frames_dir_for_run_ids(run_ids)
    print(f"Output directory: {output_dir}")

    render_frames(
        turns=turns,
        output_dir=output_dir,
        width=W,
        height=H,
        plot_renderer=plot_renderer,
        max_frames=args.max_frames,
        start_turn_index=args.start_turn_index,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
