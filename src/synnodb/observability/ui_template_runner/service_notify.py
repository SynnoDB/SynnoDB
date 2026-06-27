"""Zulip alerting helper for the UI template runner services.

Auto-enables notifications when ZULIP_EMAIL + ZULIP_API_KEY are set in the
environment (or .env). Provides a throttled `notify_worker_error()` to call
from worker exception handlers and `notify_service_crash()` for top-level
service crashes.
"""

import json as _json
import logging
import os
import socket
import sys
import threading
import time
import traceback as _tb
from pathlib import Path

from dotenv import load_dotenv

from synnodb.observability.logging import notify

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

# Auto-enable if Zulip env vars are present.
_ENABLED = bool(os.environ.get("ZULIP_EMAIL")) and bool(os.environ.get("ZULIP_API_KEY"))
if _ENABLED:
    notify.SEND_NOTIFICATIONS = True
    logger.info("Zulip worker-error alerts enabled.")
else:
    logger.info(
        "Zulip env vars not set – worker-error alerts disabled. "
        "Set ZULIP_EMAIL and ZULIP_API_KEY in .env to enable."
    )

# Throttle: suppress duplicate alerts (same service+key) within this window.
_THROTTLE_S = 300.0
_recent_lock = threading.Lock()
_recent: dict[tuple[str, str], float] = {}


def _should_send(service: str, dedup_key: str) -> bool:
    now = time.time()
    key = (service, dedup_key)
    with _recent_lock:
        last = _recent.get(key)
        if last is not None and (now - last) < _THROTTLE_S:
            return False
        _recent[key] = now
        # Best-effort cleanup of stale entries.
        for k, ts in list(_recent.items()):
            if now - ts > _THROTTLE_S * 4:
                _recent.pop(k, None)
    return True


def notify_worker_error(service: str, context: str, exc: BaseException) -> None:
    """Send a throttled Zulip alert about a worker crash.

    service: short service name, e.g. "bespoke", "umbra", "clickhouse", "frontend".
    context: short request context, e.g. "Q12 (placeholders={...})".
    exc:     the exception that was caught.
    """
    if not _ENABLED:
        return
    dedup_key = f"{context}|{type(exc).__name__}|{str(exc)[:120]}"
    if not _should_send(service, dedup_key):
        return
    hostname = socket.gethostname()
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    msg = (
        f":warning: *{service}* worker error on host `{hostname}`\n"
        f"Context: {context}\n"
        f"```quote\n{type(exc).__name__}: {exc}\n```\n"
        f"```python\n{tb_text[-1500:]}\n```"
    )
    try:
        notify.send_notification(msg, check_tmux=False)
    except Exception:
        logger.exception("Failed to send Zulip notification")


def notify_5xx_response(
    service: str, request_path: str, status: int, body: object
) -> None:
    """Alert hook called from `_send_json` whenever a service emits a 5xx.

    If an exception is currently being handled (sys.exc_info), its traceback
    is attached so handler-level crashes still produce a rich alert.
    """
    if not _ENABLED:
        return
    if isinstance(body, dict):
        err_msg = body.get("error")
        err_msg = (
            err_msg if isinstance(err_msg, str) else _json.dumps(body, default=str)
        )
    else:
        err_msg = str(body)

    exc_info = sys.exc_info()
    if exc_info[0] is not None and exc_info[1] is not None:
        exc = exc_info[1]
        dedup_key = f"{request_path}|{type(exc).__name__}|{str(exc)[:120]}"
        if not _should_send(service, dedup_key):
            return
        tb_text = "".join(_tb.format_exception(*exc_info))
        body_block = (
            f"```quote\n{type(exc).__name__}: {exc}\n```\n"
            f"```python\n{tb_text[-1500:]}\n```"
        )
    else:
        dedup_key = f"{request_path}|HTTP{status}|{err_msg[:120]}"
        if not _should_send(service, dedup_key):
            return
        body_block = f"```quote\n{err_msg[:1500]}\n```"

    hostname = socket.gethostname()
    msg = (
        f":warning: *{service}* HTTP {status} on host `{hostname}`\n"
        f"Path: `{request_path}`\n"
        f"{body_block}"
    )
    try:
        notify.send_notification(msg, check_tmux=False)
    except Exception:
        logger.exception("Failed to send Zulip notification")


def notify_service_crash(service: str, exc: BaseException) -> None:
    """Send a Zulip alert about a top-level service crash (init or serve loop)."""
    if not _ENABLED:
        return
    hostname = socket.gethostname()
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    msg = (
        f":rotating_light: *{service}* service CRASHED on host `{hostname}`\n"
        f"```quote\n{type(exc).__name__}: {exc}\n```\n"
        f"```python\n{tb_text[-2000:]}\n```"
    )
    try:
        notify.send_notification(msg, check_tmux=False)
    except Exception:
        logger.exception("Failed to send Zulip notification")
