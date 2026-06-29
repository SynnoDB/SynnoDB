from __future__ import annotations

from pathlib import Path

from synnodb import settings

W = 1920
H = 1080
FPS = 30
MAX_VISIBLE_LINES = 38
PLOT_WINDOW_TITLE = "metrics_live.py"
PROMPT_WINDOW_TITLE = "current_prompt.txt"
COST_WINDOW_TITLE = "Cost"

DEFAULT_VIDEO_PATH = Path(__file__).resolve().parent / "terminal_demo.mp4"

BG = (30, 34, 40)
TERM_BG = (16, 20, 24)
TITLE_BG = (44, 49, 56)
WIN_BORDER = (88, 93, 100)
TEXT = (235, 235, 235)
BLACK_TEXT = (20, 20, 20)
MUTED = (170, 175, 182)
GREEN = (120, 230, 145)
BLUE = (104, 164, 255)
YELLOW = (245, 205, 90)
RED = (255, 120, 120)

DEFAULT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
DEFAULT_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TERMINAL_WINDOW_TITLE = "bespoke_agent.py - Terminal"
WINDOW_TITLE_HEIGHT = 42
WINDOW_CONTENT_TOP_PAD = 18
WINDOW_TITLE_TEXT_X = 70
BACKGROUND_IMAGE_PATH = Path(__file__).resolve().parent / "background.jpeg"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "frames"
COMBINED_HISTORY_CSV_PATH = Path(__file__).resolve().parent / "combined_history.csv"
TERMINAL_WINDOW_XYWH = (20, 30, 700, 1000)
PLOT_WINDOW_XYWH = (740, 20, 1170, 700)
PROMPT_WINDOW_XYWH = (1050, 740, 830, 325)
COST_WINDOW_XYWH = (750, 880, 280, 140)
COMBINED_HISTORY_COLUMNS = [
    "turn",
    "total_runtime",
    "current_prompt",
    "shell/commands",
    "shell/outputs",
    "apply_patch/string",
    "cost_usd",
    "total/cost_usd",
    "input_tokens",
    "code_size",
    "speedup",
]


def wandb_run_cache_path() -> Path:
    """wandb run-cache dir, resolved lazily so importing this config needs no
    SYNNO_DATA_DIR (config is resolved on first use via settings)."""
    return settings.get_data_dir() / "wandb_cache"


def frames_dir_for_run_ids(run_ids: list[str]) -> Path:
    suffix = "_".join(run_id.strip() for run_id in run_ids if run_id.strip())
    if not suffix:
        return DEFAULT_OUTPUT_DIR
    return Path(__file__).resolve().parent / f"frames_{suffix}"
