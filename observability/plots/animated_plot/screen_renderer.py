from __future__ import annotations

import shutil
import subprocess
import textwrap
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from tqdm import tqdm

from observability.plots.animated_plot.config import (
    BACKGROUND_IMAGE_PATH,
    BG,
    BLACK_TEXT,
    BLUE,
    COST_WINDOW_TITLE,
    COST_WINDOW_XYWH,
    DEFAULT_MONO,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SANS,
    DEFAULT_VIDEO_PATH,
    FPS,
    GREEN,
    MAX_VISIBLE_LINES,
    MUTED,
    PLOT_WINDOW_TITLE,
    PLOT_WINDOW_XYWH,
    PROMPT_WINDOW_TITLE,
    PROMPT_WINDOW_XYWH,
    RED,
    TERM_BG,
    TERMINAL_WINDOW_TITLE,
    TERMINAL_WINDOW_XYWH,
    TEXT,
    TITLE_BG,
    WIN_BORDER,
    WINDOW_CONTENT_TOP_PAD,
    WINDOW_TITLE_HEIGHT,
    WINDOW_TITLE_TEXT_X,
    YELLOW,
    H,
    W,
)
from observability.plots.animated_plot.demo_timeline_engine import (
    DemoTimelineEngine,
)
from observability.plots.animated_plot.live_plot import TimelinePlotRenderer
from observability.plots.animated_plot.terminal_input_builder import (
    PROMPT_CWD,
    PROMPT_PREFIX,
    PROMPT_USER_HOST,
)

_WORKER_PLOT_RENDERER: TimelinePlotRenderer | None = None
_WORKER_STATIC_BASE: Image.Image | None = None
_WORKER_FONTS: tuple | None = None

WrappedTerminalLine = tuple[str, str]


@dataclass
class FrameSpec:
    frame_no: int
    out_path: Path
    visible_lines: list[str] | None
    visible_wrapped_lines: list[WrappedTerminalLine] | None
    current_prompt: str
    cumulative_cost: float
    turn_idx: int
    elapsed_sec: float
    history_index: int
    width: int
    height: int


def load_all_fonts(
    mono_path: str = DEFAULT_MONO, sans_path: str = DEFAULT_SANS
) -> tuple:
    return (
        load_font(sans_path, 17, DEFAULT_SANS),
        load_font(mono_path, 21, DEFAULT_MONO),
        load_font(sans_path, 58, DEFAULT_SANS),
        load_font(sans_path, 18, DEFAULT_SANS),
        load_font(mono_path, 18, DEFAULT_MONO),
    )


def precompute_frame_base(
    width: int, height: int, title_font: ImageFont.ImageFont | None = None
) -> Image.Image:
    base = make_background(width, height)
    term_rect = rect_from_xywh(TERMINAL_WINDOW_XYWH)
    plot_rect = rect_from_xywh(PLOT_WINDOW_XYWH)
    prompt_rect = rect_from_xywh(PROMPT_WINDOW_XYWH)
    cost_rect = rect_from_xywh(COST_WINDOW_XYWH)
    base = draw_shadow(base, term_rect)
    base = draw_shadow(base, plot_rect)
    base = draw_shadow(base, prompt_rect)
    base = draw_shadow(base, cost_rect)
    base = base.convert("RGB")

    if title_font is not None:
        draw = ImageDraw.Draw(base)
        draw_window(
            draw,
            term_rect,
            TERMINAL_WINDOW_TITLE,
            TERM_BG,
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
        draw_window(
            draw,
            plot_rect,
            PLOT_WINDOW_TITLE,
            (248, 248, 248),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
        draw_window(
            draw,
            prompt_rect,
            PROMPT_WINDOW_TITLE,
            (248, 248, 248),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
        draw_window(
            draw,
            cost_rect,
            COST_WINDOW_TITLE,
            (28, 30, 36),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )

    return base


def _init_frame_render_worker(
    history: Any,
    summary: Any,
    drill_down_to_query_level: bool,
    viewport: Any,
    dpi: int,
    width: int,
    height: int,
    mono_font_path: str,
    sans_font_path: str,
) -> None:
    global _WORKER_PLOT_RENDERER, _WORKER_STATIC_BASE, _WORKER_FONTS
    _WORKER_FONTS = load_all_fonts(mono_font_path, sans_font_path)
    _WORKER_STATIC_BASE = precompute_frame_base(width, height, _WORKER_FONTS[0])

    if history is None:
        _WORKER_PLOT_RENDERER = None
        return

    _WORKER_PLOT_RENDERER = TimelinePlotRenderer(
        engine=DemoTimelineEngine(
            history,
            summary,
            drill_down_to_query_level=drill_down_to_query_level,
        ),
        viewport=viewport,
        dpi=dpi,
    )


def _render_frame_spec(spec: FrameSpec) -> None:
    plot_rect = rect_from_xywh(PLOT_WINDOW_XYWH)
    plot_body_rect = get_window_body_rect(plot_rect)
    plot_image_width = plot_body_rect[2] - plot_body_rect[0]
    plot_image_height = plot_body_rect[3] - plot_body_rect[1]

    plot_image = None
    if _WORKER_PLOT_RENDERER is not None:
        plot_image = _WORKER_PLOT_RENDERER.render(
            history_index=spec.history_index,
            width=plot_image_width,
            height=plot_image_height,
        )

    render_screen_frame(
        out_path=spec.out_path,
        visible_lines=spec.visible_lines or [],
        visible_wrapped_lines=spec.visible_wrapped_lines,
        current_prompt=spec.current_prompt,
        plot_image=plot_image,
        cumulative_cost=spec.cumulative_cost,
        turn_idx=spec.turn_idx,
        elapsed_sec=spec.elapsed_sec,
        width=spec.width,
        height=spec.height,
        static_base=_WORKER_STATIC_BASE,
        static_base_has_windows=True,
        fonts=_WORKER_FONTS,
    )


def load_font(
    path: str | None, size: int, fallback: str
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [path, fallback]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                pass
    return ImageFont.load_default()


def create_mp4_from_frames(
    frames_dir: Path,
    output_path: Path = DEFAULT_VIDEO_PATH,
    fps: int = FPS,
    target_duration_seconds: float | None = None,
    web_compatible: bool = True,
    max_frames: int | None = None,
    crf: int = 34,
    preset: str = "veryslow",
) -> None:
    if fps not in (30, 60):
        raise ValueError("--fps must be 30 or 60")
    frame_files = sorted(frames_dir.glob("frame-*.png"))
    if not frame_files:
        raise FileNotFoundError(f"No frame PNGs found in {frames_dir}")
    if max_frames is not None:
        if max_frames <= 0:
            raise ValueError("--max-frames must be > 0")
        frame_files = frame_files[:max_frames]
        if not frame_files:
            raise FileNotFoundError(f"No frame PNGs selected from {frames_dir}")

    input_fps = float(fps)
    if target_duration_seconds is not None:
        if target_duration_seconds <= 0:
            raise ValueError("--target-duration-seconds must be > 0")
        input_fps = len(frame_files) / target_duration_seconds
    if not 0 <= crf <= 51:
        raise ValueError("--crf must be between 0 and 51")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        f"{input_fps:.6f}",
        "-i",
        str(frames_dir / "frame-%06d.png"),
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    if web_compatible:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output_path))
    subprocess.run(cmd, check=True)
    print(f"Wrote MP4 to {output_path}")


def draw_shadow(
    base: Image.Image,
    rect: tuple[int, int, int, int],
    radius: int = 18,
    blur: int = 16,
    offset: tuple[int, int] = (0, 8),
    alpha: int = 100,
) -> Image.Image:
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = rect
    ox, oy = offset
    draw.rounded_rectangle(
        (x0 + ox, y0 + oy, x1 + ox, y1 + oy), radius=radius, fill=(0, 0, 0, alpha)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base.convert("RGBA"), shadow)


def draw_window(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    title: str,
    body_fill: tuple[int, int, int],
    title_fill: tuple[int, int, int],
    border: tuple[int, int, int],
    sans_font: ImageFont.ImageFont,
    radius: int = 18,
) -> None:
    x0, y0, x1, y1 = rect
    draw.rounded_rectangle(rect, radius=radius, fill=body_fill, outline=border, width=1)
    draw.rounded_rectangle(
        (x0, y0, x1, y0 + WINDOW_TITLE_HEIGHT), radius=radius, fill=title_fill
    )
    draw.rectangle(
        (x0, y0 + WINDOW_TITLE_HEIGHT // 2, x1, y0 + WINDOW_TITLE_HEIGHT),
        fill=title_fill,
    )
    draw.text(
        (x0 + WINDOW_TITLE_TEXT_X, y0 + 10), title, font=sans_font, fill=(230, 230, 230)
    )

    radius_px = 6
    cy = y0 + 19
    colors = [(255, 95, 87), (255, 189, 46), (39, 201, 63)]
    for i, color in enumerate(colors):
        cx = x0 + 18 + i * 18
        draw.ellipse(
            (cx - radius_px, cy - radius_px, cx + radius_px, cy + radius_px), fill=color
        )


def classify_plain_line_kind(line: str) -> str:
    low = line.lower()
    if line.startswith("@@"):
        return "plain_hunk"
    if line.startswith("+") and not line.startswith("+++"):
        return "plain_add"
    if line.startswith("-") and not line.startswith("---"):
        return "plain_remove"
    if (
        line.startswith("diff ")
        or line.startswith("index ")
        or line.startswith("+++ ")
        or line.startswith("--- ")
    ):
        return "plain_meta"
    if "error" in low or "failed" in low or "exception" in low:
        return "plain_error"
    if "warn" in low:
        return "plain_warn"
    return "plain"


def color_for_line_kind(line_kind: str) -> tuple[int, int, int]:
    if line_kind == "plain_add":
        return GREEN
    if line_kind == "plain_remove":
        return RED
    if line_kind == "plain_hunk":
        return BLUE
    if line_kind == "plain_meta":
        return MUTED
    if line_kind == "plain_error":
        return RED
    if line_kind == "plain_warn":
        return YELLOW
    return TEXT


def wrap_terminal_lines(
    lines: list[str],
    mono_font: ImageFont.ImageFont,
    available_width_px: int,
) -> list[tuple[str, str]]:
    prompt_prefix_display = f"{PROMPT_USER_HOST}:{PROMPT_CWD}$ "
    prompt_prefix_width = mono_font.getlength(prompt_prefix_display)

    def wrap_text_to_width(text: str, max_width: float) -> list[str]:
        if not text:
            return [""]

        single_char_w = mono_font.getlength("M")
        if single_char_w <= 0 or max_width <= 0:
            return [text]

        chars_per_line = max(1, int(max_width / single_char_w))
        wrapped: list[str] = []
        remaining = text
        while remaining:
            end = min(chars_per_line, len(remaining))
            chunk = remaining[:end]
            # Fine-tune near the boundary (O(1) adjustments for monospace)
            while len(chunk) > 1 and mono_font.getlength(chunk) > max_width:
                end -= 1
                chunk = remaining[:end]
            while (
                end < len(remaining)
                and mono_font.getlength(chunk + remaining[end]) <= max_width
            ):
                chunk += remaining[end]
                end += 1
            if not chunk:
                end = 1
                chunk = remaining[0]
            wrapped.append(chunk)
            remaining = remaining[end:]

        return wrapped

    wrapped: list[tuple[str, str]] = []
    for line in lines:
        if line.startswith(PROMPT_PREFIX):
            command_text = line[len(PROMPT_PREFIX) :]
            first_line_width = max(8.0, available_width_px - prompt_prefix_width)
            first_chunk = wrap_text_to_width(command_text, first_line_width)[0]
            wrapped.append(("prompt", first_chunk))

            remaining = command_text[len(first_chunk) :]
            if remaining:
                continuation_chunks = wrap_text_to_width(
                    remaining, max(8.0, float(available_width_px))
                )
                for chunk in continuation_chunks:
                    wrapped.append(("prompt_cont", chunk))
            continue

        plain_kind = classify_plain_line_kind(line)
        plain_chunks = wrap_text_to_width(line, max(8.0, float(available_width_px)))
        for chunk in plain_chunks:
            wrapped.append((plain_kind, chunk))

    return wrapped


def draw_terminal_text(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    lines: list[str],
    mono_font: ImageFont.ImageFont,
    max_visible_lines: int,
    blink_cursor: bool,
    wrapped_lines: list[WrappedTerminalLine] | None = None,
) -> None:
    x0, y0, x1, y1 = rect
    pad_x = 18
    pad_y = WINDOW_TITLE_HEIGHT + WINDOW_CONTENT_TOP_PAD
    line_h = 24
    y = y0 + pad_y
    available_height = max(0, (y1 - 22) - y)
    max_fit_lines = max(0, available_height // line_h)

    if wrapped_lines is None:
        wrapped_lines = wrap_terminal_lines(lines, mono_font, x1 - x0 - 2 * pad_x)
    visible_line_budget = min(max_visible_lines, max_fit_lines)
    visible = wrapped_lines[-visible_line_budget:] if visible_line_budget > 0 else []
    prompt_cwd = f":{PROMPT_CWD}"

    for line_kind, line_text in visible:
        if line_kind == "prompt":
            cursor_x = x0 + pad_x
            draw.text((cursor_x, y), PROMPT_USER_HOST, font=mono_font, fill=GREEN)
            cursor_x += mono_font.getlength(PROMPT_USER_HOST)
            draw.text((cursor_x, y), prompt_cwd, font=mono_font, fill=BLUE)
            cursor_x += mono_font.getlength(prompt_cwd)
            draw.text((cursor_x, y), "$ " + line_text, font=mono_font, fill=TEXT)
        elif line_kind == "prompt_cont":
            draw.text((x0 + pad_x, y), line_text, font=mono_font, fill=TEXT)
        else:
            draw.text(
                (x0 + pad_x, y),
                line_text,
                font=mono_font,
                fill=color_for_line_kind(line_kind),
            )
        y += line_h

    if blink_cursor:
        cursor_x = x0 + pad_x
        if not visible:
            draw.text((cursor_x, y), PROMPT_USER_HOST, font=mono_font, fill=GREEN)
            cursor_x += mono_font.getlength(PROMPT_USER_HOST)
            draw.text((cursor_x, y), prompt_cwd, font=mono_font, fill=BLUE)
            cursor_x += mono_font.getlength(prompt_cwd)
            draw.text((cursor_x, y), "$ ", font=mono_font, fill=TEXT)
            cursor_x += mono_font.getlength("$ ")
        draw.text((cursor_x, y), "█", font=mono_font, fill=GREEN)


def draw_cost_panel(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    cumulative_cost: float,
    turn_idx: int,
    elapsed_sec: float,
    big_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    x0, y0, x1, y1 = rect
    draw.text(
        (x0 + 20, y0 + 42), format_cost(cumulative_cost), font=big_font, fill=TEXT
    )
    draw.text(
        (x0 + 20, y1 - 24),
        f"turn {turn_idx}   elapsed {format_elapsed(elapsed_sec)}",
        font=small_font,
        fill=MUTED,
    )


def draw_prompt_panel(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    prompt_text: str,
    body_font: ImageFont.ImageFont,
) -> None:
    x0, y0, x1, y1 = rect
    char_width = max(1, int(round(body_font.getlength("M"))))
    available_chars = max(20, (x1 - x0 - 40) // char_width)
    wrapped_lines: list[str] = []
    for paragraph in (prompt_text or "").splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph,
            width=available_chars,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        wrapped_lines.extend(wrapped or [""])

    line_height = 24
    max_lines = max(1, (y1 - y0 - 72) // line_height + 1)
    visible_lines = wrapped_lines[:max_lines]

    y = y0 + 56
    for line in visible_lines:
        draw.text((x0 + 20, y), line, font=body_font, fill=BLACK_TEXT)
        y += line_height


def make_background(width: int, height: int) -> Image.Image:
    if not BACKGROUND_IMAGE_PATH.exists():
        return Image.new("RGB", (width, height), BG)

    bg = Image.open(BACKGROUND_IMAGE_PATH).convert("RGB")
    scale = max(width / bg.width, height / bg.height)
    resized = bg.resize(
        (max(1, round(bg.width * scale)), max(1, round(bg.height * scale)))
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def rect_from_xywh(xywh: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = xywh
    return (x, y, x + w, y + h)


def get_window_body_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    return (x0 + 1, y0 + WINDOW_TITLE_HEIGHT + 1, x1 - 1, y1 - 1)


def format_elapsed(elapsed_sec: float) -> str:
    total_seconds = max(0, int(round(elapsed_sec)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def format_cost(cumulative_cost: float) -> str:
    value = abs(cumulative_cost)
    if value >= 100:
        decimals = 2
    elif value >= 10:
        decimals = 3
    else:
        decimals = 4
    return f"${cumulative_cost:,.{decimals}f}"


def render_screen_frame(
    out_path: Path,
    visible_lines: list[str],
    current_prompt: str,
    plot_image: Image.Image | None,
    cumulative_cost: float,
    turn_idx: int,
    elapsed_sec: float,
    width: int = W,
    height: int = H,
    mono_font_path: str = DEFAULT_MONO,
    sans_font_path: str = DEFAULT_SANS,
    plot_title: str = PLOT_WINDOW_TITLE,
    prompt_title: str = PROMPT_WINDOW_TITLE,
    cost_title: str = COST_WINDOW_TITLE,
    max_visible_lines: int = MAX_VISIBLE_LINES,
    blink_cursor: bool = True,
    visible_wrapped_lines: list[WrappedTerminalLine] | None = None,
    static_base: Image.Image | None = None,
    static_base_has_windows: bool = False,
    fonts: tuple | None = None,
) -> None:
    term_rect = rect_from_xywh(TERMINAL_WINDOW_XYWH)
    plot_rect = rect_from_xywh(PLOT_WINDOW_XYWH)
    prompt_rect = rect_from_xywh(PROMPT_WINDOW_XYWH)
    cost_rect = rect_from_xywh(COST_WINDOW_XYWH)

    using_precomposed_base = static_base is not None and static_base_has_windows
    if static_base is not None:
        img = static_base.copy()
    else:
        base = make_background(width, height)
        base = draw_shadow(base, term_rect)
        base = draw_shadow(base, plot_rect)
        base = draw_shadow(base, prompt_rect)
        base = draw_shadow(base, cost_rect)
        img = base.convert("RGB")

    draw = ImageDraw.Draw(img)

    if fonts is not None:
        title_font, mono_font, cost_big_font, cost_small_font, prompt_body_font = fonts
    else:
        title_font = load_font(sans_font_path, 17, DEFAULT_SANS)
        mono_font = load_font(mono_font_path, 21, DEFAULT_MONO)
        cost_big_font = load_font(sans_font_path, 58, DEFAULT_SANS)
        cost_small_font = load_font(sans_font_path, 18, DEFAULT_SANS)
        prompt_body_font = load_font(mono_font_path, 18, DEFAULT_MONO)

    assert isinstance(title_font, ImageFont.ImageFont)
    assert isinstance(mono_font, ImageFont.ImageFont)
    assert isinstance(cost_big_font, ImageFont.ImageFont)
    assert isinstance(cost_small_font, ImageFont.ImageFont)
    assert isinstance(prompt_body_font, ImageFont.ImageFont)

    if not using_precomposed_base:
        draw_window(
            draw,
            term_rect,
            TERMINAL_WINDOW_TITLE,
            TERM_BG,
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
    draw_terminal_text(
        draw,
        term_rect,
        visible_lines,
        mono_font,
        max_visible_lines,
        blink_cursor,
        wrapped_lines=visible_wrapped_lines,
    )

    if not using_precomposed_base:
        draw_window(
            draw,
            plot_rect,
            plot_title,
            (248, 248, 248),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
    if plot_image is not None:
        plot_body_rect = get_window_body_rect(plot_rect)
        chart_target_w = plot_body_rect[2] - plot_body_rect[0]
        chart_target_h = plot_body_rect[3] - plot_body_rect[1]
        if plot_image.size == (chart_target_w, chart_target_h):
            fitted_plot = plot_image
        else:
            fitted_plot = plot_image.resize((chart_target_w, chart_target_h))
        img.paste(fitted_plot, (plot_body_rect[0], plot_body_rect[1]))

    if not using_precomposed_base:
        draw_window(
            draw,
            prompt_rect,
            prompt_title,
            (248, 248, 248),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
    draw_prompt_panel(
        draw=draw,
        rect=prompt_rect,
        prompt_text=current_prompt,
        body_font=prompt_body_font,
    )

    if not using_precomposed_base:
        draw_window(
            draw,
            cost_rect,
            cost_title,
            (28, 30, 36),
            TITLE_BG,
            WIN_BORDER,
            title_font,
        )
    draw_cost_panel(
        draw=draw,
        rect=cost_rect,
        cumulative_cost=cumulative_cost,
        turn_idx=turn_idx,
        elapsed_sec=elapsed_sec,
        big_font=cost_big_font,
        small_font=cost_small_font,
    )

    img.save(out_path)


def render_frames(
    turns: list[dict[str, object]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    width: int = W,
    height: int = H,
    plot_renderer: TimelinePlotRenderer | None = None,
    max_frames: int | None = None,
    start_turn_index: int = 0,
    workers: int = 1,
) -> None:
    if start_turn_index < 0:
        raise ValueError("start_turn_index must be >= 0")
    if workers <= 0:
        raise ValueError("workers must be >= 1")

    if start_turn_index == 0:
        if output_dir.exists():
            print(f"Clearing existing output directory: {output_dir}")
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"Resuming from turn index {start_turn_index}, keeping existing frames in {output_dir}"
        )

    print(f"Building frame specs from {len(turns)} turns...")

    frame_no = 1
    written_frames = 0
    spec_fonts = load_all_fonts()
    terminal_mono_font = spec_fonts[1]
    term_rect = rect_from_xywh(TERMINAL_WINDOW_XYWH)
    terminal_available_width = term_rect[2] - term_rect[0] - 2 * 18
    wrapped_terminal_lines: list[WrappedTerminalLine] = []
    terminal_tail_budget = MAX_VISIBLE_LINES
    frame_specs: list[FrameSpec] = []

    for turn_index, turn in enumerate(turns):
        if max_frames is not None and written_frames >= max_frames:
            break

        turn_num = int(turn["turn"])  # type: ignore
        elapsed_sec = float(turn["elapsed_sec"])  # type: ignore
        cumulative_cost = float(turn["cumulative_cost"])  # type: ignore
        current_prompt = str(turn.get("current_prompt", ""))  # type: ignore
        history_index = int(turn.get("history_index", turn_num))  # type: ignore
        new_lines = list(turn.get("log_lines", []))  # type: ignore

        new_wrapped_lines = wrap_terminal_lines(
            new_lines, terminal_mono_font, terminal_available_width
        )
        frame_wrapped_lines = wrapped_terminal_lines + new_wrapped_lines
        visible_wrapped_lines = frame_wrapped_lines[-terminal_tail_budget:]

        if turn_index >= start_turn_index:
            out_path = output_dir / f"frame-{frame_no:06d}.png"
            frame_specs.append(
                FrameSpec(
                    frame_no=frame_no,
                    out_path=out_path,
                    visible_lines=None,
                    visible_wrapped_lines=list(visible_wrapped_lines),
                    current_prompt=current_prompt,
                    cumulative_cost=cumulative_cost,
                    turn_idx=turn_num,
                    elapsed_sec=elapsed_sec,
                    history_index=history_index,
                    width=width,
                    height=height,
                )
            )
            written_frames += 1

        frame_no += 1
        wrapped_terminal_lines = frame_wrapped_lines[-terminal_tail_budget:]

    print(f"Rendering {len(frame_specs)} frames with {workers} worker(s)...")

    if workers == 1:
        global _WORKER_PLOT_RENDERER, _WORKER_STATIC_BASE, _WORKER_FONTS
        _WORKER_PLOT_RENDERER = plot_renderer
        _WORKER_FONTS = spec_fonts
        _WORKER_STATIC_BASE = precompute_frame_base(width, height, _WORKER_FONTS[0])
        for spec in tqdm(frame_specs, desc="Rendering frames", unit="frame"):
            _render_frame_spec(spec)
        _WORKER_PLOT_RENDERER = None
        _WORKER_STATIC_BASE = None
        _WORKER_FONTS = None
    else:
        history = None
        summary = None
        drill_down_to_query_level = False
        viewport = None
        dpi = 100
        if plot_renderer is not None:
            history = plot_renderer.engine.history
            summary = plot_renderer.engine.summary
            drill_down_to_query_level = plot_renderer.engine.drill_down_to_query_level
            viewport = plot_renderer.viewport
            dpi = plot_renderer.dpi

        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_frame_render_worker,
            initargs=(
                history,
                summary,
                drill_down_to_query_level,
                viewport,
                dpi,
                width,
                height,
                DEFAULT_MONO,
                DEFAULT_SANS,
            ),
        ) as executor:
            list(
                tqdm(
                    executor.map(_render_frame_spec, frame_specs),
                    total=len(frame_specs),
                    desc="Rendering frames",
                    unit="frame",
                )
            )

    print(f"Rendered {written_frames} frames to {output_dir}")
