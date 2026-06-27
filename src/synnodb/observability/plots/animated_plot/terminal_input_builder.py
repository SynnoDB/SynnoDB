from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pandas as pd

PROMPT_USER_HOST = "mjasny@fn01"
PROMPT_CWD = "~/bespoke_agent"
PROMPT_PREFIX = f"{PROMPT_USER_HOST}:{PROMPT_CWD}/$ "


def _is_emptyish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        is_na = pd.isna(value)
    except TypeError:
        is_na = False
    if isinstance(is_na, bool) and is_na:
        return True
    if isinstance(value, str) and value.strip().lower() == "nan":
        return True
    return False


def _normalize_shell_commands(value: Any) -> list[str]:
    if _is_emptyish(value):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return _normalize_shell_commands(parsed)
        return [text]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if _is_emptyish(item):
                continue
            text = str(item).strip()
            if text and text.lower() != "nan":
                out.append(text)
        return out
    text = str(value).strip()
    return [] if not text or text.lower() == "nan" else [text]


def _normalize_shell_outputs(value: Any) -> list[str]:
    def clean_output_lines(lines: list[str]) -> list[str]:
        cleaned: list[str] = []
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.lower() == "nan":
                continue
            if idx == 0 and stripped.startswith("$ "):
                continue
            if stripped.startswith("stdout:"):
                remainder = stripped[len("stdout:") :].lstrip()
                if remainder:
                    cleaned.append(remainder)
                continue
            if stripped.startswith("stderr:"):
                remainder = stripped[len("stderr:") :].lstrip()
                if remainder:
                    cleaned.append(remainder)
                continue
            cleaned.append(line.rstrip())
        return cleaned

    if _is_emptyish(value):
        return []
    if isinstance(value, str):
        return clean_output_lines(value.splitlines())
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if _is_emptyish(item):
                continue
            if isinstance(item, str):
                out.extend(line.rstrip() for line in item.splitlines())
            else:
                out.extend(str(item).splitlines())
        return clean_output_lines(out)
    return clean_output_lines(str(value).splitlines())


def _normalize_apply_patch_string(value: Any) -> list[str]:
    if _is_emptyish(value):
        return []
    if isinstance(value, str):
        return [
            line
            for line in value.splitlines()
            if line.strip() and line.strip().lower() != "nan"
        ]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if _is_emptyish(item):
                continue
            out.extend(str(item).splitlines())
        return [
            line.rstrip()
            for line in out
            if line.strip() and line.strip().lower() != "nan"
        ]
    return [
        line.rstrip()
        for line in str(value).splitlines()
        if line.strip() and line.strip().lower() != "nan"
    ]


def _format_apply_patch_lines(value: Any) -> list[str]:
    raw_lines = _normalize_apply_patch_string(value)
    if not raw_lines:
        return []

    formatted: list[str] = []
    current_path: str | None = None
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.lower() == "nan":
            continue
        if stripped.startswith("=== UPDATING: ") and stripped.endswith(" ==="):
            current_path = stripped[len("=== UPDATING: ") : -len(" ===")]
            formatted.extend(
                _format_command_lines(f"apply_patch {Path(current_path).name}")
            )
            continue

        if current_path is None:
            continue
        formatted.append(line)

    return formatted


def _format_command_lines(command: str) -> list[str]:
    continuation_prefix = " " * len(PROMPT_PREFIX)
    raw_lines = command.splitlines() or [command]

    formatted: list[str] = []
    for idx, raw_line in enumerate(raw_lines):
        line = raw_line.rstrip()
        if idx == 0:
            formatted.append(f"{PROMPT_PREFIX}{line}")
        else:
            formatted.append(f"{continuation_prefix}{line}")
    return formatted


def build_terminal_turns(history: pd.DataFrame) -> list[dict[str, Any]]:
    required_cols = ["turn", "total/runtime", "current_prompt"]
    missing = [col for col in required_cols if col not in history.columns]
    if missing:
        raise ValueError(f"History is missing required columns: {missing}")
    if "total/cost_usd" not in history.columns:
        raise ValueError("History is missing required column: ['total/cost_usd']")

    history = history.copy()
    history["current_prompt"] = history["current_prompt"].ffill()
    history["_terminal_cumulative_cost"] = (
        pd.to_numeric(history["total/cost_usd"], errors="coerce").ffill().fillna(0.0)
    )

    turns: list[dict[str, Any]] = []
    last_prompt = ""
    previous_elapsed_sec = 0.0

    if "shell/commands" not in history.columns:
        history["shell/commands"] = None
    if "shell/outputs" not in history.columns:
        history["shell/outputs"] = None
    if "apply_patch/string" not in history.columns:
        history["apply_patch/string"] = None

    history["_terminal_elapsed_sec"] = (
        pd.to_numeric(history["total/runtime"], errors="coerce").ffill().fillna(0.0)
    )

    for turn_num, group in history.groupby("turn", sort=False):
        log_lines: list[str] = []
        for _, row in group.iterrows():
            commands = _normalize_shell_commands(row["shell/commands"])
            outputs = _normalize_shell_outputs(row["shell/outputs"])
            apply_patch_value = row["apply_patch/string"]
            apply_patch_lines = _format_apply_patch_lines(apply_patch_value)

            for command in commands:
                log_lines.extend(_format_command_lines(command))
            log_lines.extend(outputs)
            if apply_patch_lines:
                log_lines.extend(apply_patch_lines)

        elapsed_sec = float(group["_terminal_elapsed_sec"].iloc[-1])
        duration_sec = max(0.0, elapsed_sec - previous_elapsed_sec)
        previous_elapsed_sec = elapsed_sec
        history_index = int(group.index[-1])
        cumulative_cost = float(group["_terminal_cumulative_cost"].iloc[-1])
        prompt_val = group["current_prompt"].iloc[-1]
        if pd.notna(prompt_val):
            last_prompt = str(prompt_val)
        current_prompt = last_prompt

        turns.append(
            {
                "turn": int(turn_num),  # type: ignore
                "history_index": history_index,
                "duration_sec": duration_sec,
                "elapsed_sec": elapsed_sec,
                "cumulative_cost": cumulative_cost,
                "current_prompt": current_prompt,
                "log_lines": log_lines,
            }
        )

    return turns


def write_terminal_inputs(history: pd.DataFrame, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    turns = build_terminal_turns(history)

    run_log_path = output_dir / "run.log"
    turns_path = output_dir / "turns.json"

    log_lines = [line for turn in turns for line in turn["log_lines"]]
    run_log_path.write_text(
        "\n".join(log_lines) + ("\n" if log_lines else ""), encoding="utf-8"
    )
    turns_path.write_text(json.dumps(turns, indent=2), encoding="utf-8")

    return run_log_path, turns_path
