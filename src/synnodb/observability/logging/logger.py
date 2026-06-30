import logging
import re
import sys
from pathlib import Path

PLAIN = 25
logging.addLevelName(PLAIN, "PLAIN")


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",  # cyan
        logging.INFO: "\033[32m",  # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",  # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        record.name = f"{color}{record.name}{self.RESET}"
        return super().format(record)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class SuppressMessageFilter(logging.Filter):
    def __init__(self, name: str, patterns: list[str]) -> None:
        super().__init__(name)
        self._patterns = [re.compile(pattern) for pattern in patterns]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(pattern.search(msg) for pattern in self._patterns)


def setup_logging(
    level: int = logging.INFO,
    logfile: Path | None = None,
    console_level: int | None = None,
) -> None:
    # ``level`` is the root/file verbosity (kept high so the logfile captures
    # everything). ``console_level`` independently throttles what reaches the
    # terminal; default it to the quieter of INFO and ``level`` so a DEBUG file
    # log doesn't flood the console. Pass console_level=DEBUG for verbose mode.
    if console_level is None:
        console_level = max(level, logging.INFO)
    handlers = []

    # Console handler (colored). Route to stdout (not the default stderr) so that
    # log lines and ordinary ``print`` output share one stream — otherwise tools
    # like Jupyter/VSCode render stderr logs and stdout prints as two separate
    # output blocks for the same cell.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        ColorFormatter(
            "%(asctime)s %(levelname)s:%(name)s:%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    console_handler.addFilter(lambda r: r.levelno != PLAIN)
    handlers.append(console_handler)

    # "Print-like" logs (custom level only)
    plain_console_handler = logging.StreamHandler(sys.stdout)
    plain_console_handler.setLevel(PLAIN)
    plain_console_handler.setFormatter(PlainFormatter())
    plain_console_handler.addFilter(lambda r: r.levelno == PLAIN)
    handlers.append(plain_console_handler)

    # File handler (plain text)

    if logfile:
        # Structured file logs (except PLAIN)
        file_handler = logging.FileHandler(logfile.as_posix(), encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s:%(name)s:%(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(lambda r: r.levelno != PLAIN)
        handlers.append(file_handler)

        # Plain file logs (ONLY PLAIN)
        plain_file_handler = logging.FileHandler(logfile.as_posix(), encoding="utf-8")
        plain_file_handler.setFormatter(PlainFormatter())
        plain_file_handler.addFilter(lambda r: r.levelno == PLAIN)
        handlers.append(plain_file_handler)

    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )

    suppress = SuppressMessageFilter(
        "openai.agents",
        [
            "Tracing is disabled. Not creating span",
            "Resetting current trace",
            "Processing output item type=message",
            "Processing output item type=reasoning",
            "Processing output item type=function_call",
            "Processing output item type=shell_call",
            "skip: deferring compaction for response",
            "Creating span",
            "Exported [0-9]+ items",
            r"Running agent [\s\S]+ \(turn [0-9]+\)",
            "LLM responded",
            "Calling LLM",
            "Queueing shell_call call_",
            "Persisting turn items",
            "Queuing apply_patch_call",
            "Processing output item",
            "No conversation_id available for request",
            "Received model response",
            "Tracing is diabled. Not creating trace",
            "Setting current trace:",
        ],
    )
    logging.getLogger("openai.agents").addFilter(suppress)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)

    if level == logging.DEBUG:
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("gql.transport.httpx").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("openai.agents").setLevel(logging.DEBUG)
        logging.getLogger("weave").setLevel(logging.WARNING)
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        logging.getLogger("anthropic").setLevel(logging.INFO)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
