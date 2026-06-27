from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))


from synnodb.observability.plots.animated_plot.config import (
    DEFAULT_VIDEO_PATH,
    frames_dir_for_run_ids,
)
from synnodb.observability.plots.animated_plot.screen_renderer import create_mp4_from_frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a web-playable MP4 from already-rendered demo frames."
    )
    parser.add_argument(
        "--run-ids",
        default=None,
        dest="run_ids",
        help="Optional comma-separated run ids used to infer the default frames directory.",
    )
    parser.add_argument(
        "--frames-dir",
        type=Path,
        default=None,
        help="Directory containing frame-*.png files.",
    )
    parser.add_argument(
        "--video-path",
        type=Path,
        default=DEFAULT_VIDEO_PATH,
        help="Where to write the MP4.",
    )
    parser.add_argument(
        "--target-duration-seconds",
        type=float,
        default=None,
        help="Optional target MP4 duration in seconds. Output FPS remains 30 or 60.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        choices=(30, 60),
        default=30,
        help="Output MP4 frame rate. Must be 30 or 60.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Optional limit on how many frames to use from the beginning of the folder.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=36,
        help="x264 quality/compression level. Higher is smaller/lower quality; 23 is ffmpeg's default.",
    )
    parser.add_argument(
        "--preset",
        default="veryslow",
        help="x264 compression preset. Slower presets reduce size without changing resolution or FPS.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_ids = []
    if args.run_ids:
        run_ids = [
            run_id.strip() for run_id in args.run_ids.split(",") if run_id.strip()
        ]
    frames_dir = args.frames_dir or frames_dir_for_run_ids(run_ids)
    create_mp4_from_frames(
        frames_dir=frames_dir,
        output_path=args.video_path,
        fps=args.fps,
        target_duration_seconds=args.target_duration_seconds,
        max_frames=args.max_frames,
        crf=args.crf,
        preset=args.preset,
    )


if __name__ == "__main__":
    main()
