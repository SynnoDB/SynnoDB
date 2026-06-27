import json
import logging
import re
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

_error_counter = 0
_initialized_models = set()  # track which model files have been cleared this run


def log_tool_call_error(
    error_type: str,
    error: Exception,
    model: str = "",
    turn: int | None = None,
    raw_tool_calls: list | None = None,
):
    global _error_counter
    _error_counter += 1

    log_dir = Path("tool_call_errors")
    log_dir.mkdir(parents=True, exist_ok=True)

    # sanitize model name for filename
    safe_model = model.replace("/", "_").replace(" ", "_")
    filepath = log_dir / f"{safe_model}.log"

    # overwrite on first error per model per run
    if safe_model not in _initialized_models:
        _initialized_models.add(safe_model)
        mode = "w"
    else:
        mode = "a"

    # format raw tool calls if available
    raw_section = ""
    if raw_tool_calls:
        raw_section = "\n--- Raw Tool Calls ---\n"
        for tc in raw_tool_calls:
            raw_section += f"Tool: {tc.get('name', 'unknown')}\n"
            raw_section += f"Arguments:\n{tc.get('arguments', '')}\n\n"

    entry = (
        f"=== Error #{_error_counter} @ {datetime.now().isoformat()} ===\n"
        f"Type: {error_type}\n"
        f"Turn: {turn or 'unknown'}\n"
        f"Message: {str(error)}\n"
        f"{raw_section}\n"
    )

    with open(filepath, mode) as f:
        f.write(entry)
    logger.info(f"Logged tool call error to {filepath}")
