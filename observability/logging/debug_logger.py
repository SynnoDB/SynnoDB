import time
from datetime import datetime
from pathlib import Path

_LLM_MAX_CHARS = 4000
_TOOL_MAX_CHARS = 3000
_EVENT_MAX_CHARS = 8000
_SEP = "#" * 80


def _truncate(text: str, cap: int) -> str:
    """Clip text to cap chars, appending a marker that records the original size."""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...(truncated, {len(text)} chars total)"


class DebugLogger:
    """Write a human-readable debug log for one run to <base_dir>/<category>/<storage>/debug.log.

    One chronological file per (category, storage): all stages and queries in the
    run are appended in execution order. Per-query attribution is carried on the
    stage header (see log_stage_start), not in the path, so the timeline stays
    intact. `base_dir` is the run's output dir, so separate runs never overwrite
    each other.

    category: the conversation kind, e.g. "base_impl" or "storage_plan"
    storage:  "ssd" or "in_memory"
    model:    recorded in the file header only, not used in the path
    """

    def __init__(
        self,
        category: str,
        storage: str,
        model: str,
        base_dir: str | Path,
    ):
        log_dir = Path(base_dir) / category / storage
        log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / "debug.log"
        self._path.write_text(
            f"# debug log — {category} | storage: {storage} | model: {model} "
            f"| {datetime.now().isoformat()}\n\n"
        )
        self._stage_start_time: float | None = None

    # ── Stage boundaries ────────────────────────────────────────────────────

    def log_stage_start(
        self,
        stage_nr: int,
        descriptor: str,
        rt_before_s: float | None,
        query_id: str | None = None,
    ) -> None:
        self._stage_start_time = time.monotonic()
        query_suffix = f" | query {query_id}" if query_id is not None else ""
        lines = [
            _SEP,
            f"## Stage {stage_nr}: {descriptor}{query_suffix}",
            f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if rt_before_s is not None:
            lines.append(f"## Runtime before: {rt_before_s * 1000:.0f}ms")
        lines.append(_SEP)
        self._append("\n".join(lines) + "\n\n")

    def log_stage_end(
        self,
        rt_after_s: float | None = None,
        speedup_after: float | None = None,
    ) -> None:
        # The logger owns stage timing: duration is measured from the matching
        # log_stage_start, so the caller does not keep its own stopwatch. Defaults
        # to 0s if no start was recorded (e.g. an end without a start).
        duration_s = (
            time.monotonic() - self._stage_start_time
            if self._stage_start_time is not None
            else 0.0
        )
        self._stage_start_time = None
        mins, secs = divmod(int(duration_s), 60)
        dur_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
        if rt_after_s is not None and speedup_after is not None:
            summary = (
                f"Speedup: {speedup_after:.2f}x ({rt_after_s * 1000:.0f}ms) "
                f"| Duration: {dur_str}"
            )
        else:
            summary = f"Duration: {dur_str}"
        lines = ["", _SEP, f"## Stage END — {summary}", _SEP, ""]
        self._append("\n".join(lines) + "\n")

    # ── Per-turn entries ────────────────────────────────────────────────────

    def log_prompt(self, prompt_idx: int, descriptor: str | None, text: str) -> None:
        descriptor_str = f" - {descriptor}" if descriptor else ""
        self._append(f"[Prompt {prompt_idx}{descriptor_str}]\n{text}\n\n")

    def log_event(self, label: str, text: str = "") -> None:
        """Record a free-form annotation (e.g. a compaction event) so the log tells
        the full story - compactions and reprompts otherwise leave an unexplained
        gap between turns."""
        body = f"\n{_truncate(text, _EVENT_MAX_CHARS)}" if text else ""
        self._append(f"[{datetime.now().strftime('%H:%M:%S')}] {label}{body}\n\n")

    def log_llm_turn(self, turn: int, text: str) -> None:
        if not text.strip():
            return
        self._append(f"[Turn {turn} - LLM]\n{_truncate(text, _LLM_MAX_CHARS)}\n\n")

    def log_tool_result(self, turn: int, tool_name: str, result: str) -> None:
        self._append(
            f"[Turn {turn} - Tool: {tool_name}]\n"
            f"{_truncate(result, _TOOL_MAX_CHARS)}\n\n"
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _append(self, text: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception:
            # Best-effort debug logging: never fail the run due to debug I/O.
            pass
