import logging
import os
import socket
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def _tmux_fmt(fmt: str, target: str | None = None) -> str:
    cmd = ["tmux", "display-message", "-p"]
    if target:
        cmd += ["-t", target]
    cmd.append(fmt)
    return subprocess.check_output(cmd, text=True).strip()


def is_tmux_focused() -> bool:
    """
    True if this process is in tmux AND its pane is the active pane in the active window.
    False if not in tmux, or pane/window not active, or tmux not reachable.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False  # not running inside tmux

    try:
        # Both are documented format vars.
        pane_active = _tmux_fmt("#{pane_active}", target=pane) == "1"
        window_active = _tmux_fmt("#{window_active}", target=pane) == "1"
        return pane_active and window_active
    except Exception:
        return False


# load dotenv
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

ZULIP_ADDR = os.getenv("ZULIP_ADDR")
ZULIP_EMAIL = os.getenv("ZULIP_EMAIL")
ZULIP_API_KEY = os.getenv("ZULIP_API_KEY")
ZULIP_TO_USER = os.getenv("ZULIP_TO_USER")
ZULIP_TO_CHNL = os.getenv("ZULIP_TO_CHNL")
ZULIP_TO_TOPIC = os.getenv("ZULIP_TO_TOPIC")


class ZulipBot:
    def __init__(self, email, api_key):
        self.email = email
        self.api_key = api_key
        self.url = ZULIP_ADDR

    def send_to_user(self, to, msg):
        assert self.url is not None, "ZULIP_ADDR is not set"
        r = requests.post(
            self.url,
            auth=(self.email, self.api_key),
            data={"type": "private", "to": to, "content": msg},
        )
        assert r.json()["result"] == "success"

    def send_to_stream(self, to, topic, msg):
        assert self.url is not None, "ZULIP_ADDR is not set"
        r = requests.post(
            self.url,
            auth=(self.email, self.api_key),
            data={"type": "stream", "to": to, "topic": topic, "content": msg},
        )
        assert r.json()["result"] == "success"


SEND_NOTIFICATIONS = False


def send_notification(msg, check_tmux=False, prepend_with_hostname: bool = True):
    if not SEND_NOTIFICATIONS:
        return
    assert ZULIP_EMAIL is not None, "ZULIP_EMAIL is not set"
    assert ZULIP_API_KEY is not None, "ZULIP_API_KEY is not set"
    assert ZULIP_TO_USER is not None or ZULIP_TO_CHNL is not None, (
        "Either ZULIP_TO_USER or ZULIP_TO_CHNL must be set"
    )

    if check_tmux and is_tmux_focused():
        logger.info("No notification, tmux pane is focused")
        return
    bot = ZulipBot(ZULIP_EMAIL, ZULIP_API_KEY)

    if prepend_with_hostname:
        hostname = socket.gethostname()
        msg = f"**[{hostname}]** {msg}"

    if ZULIP_TO_USER is not None:
        bot.send_to_user(ZULIP_TO_USER, msg=msg)
    if ZULIP_TO_CHNL is not None:
        bot.send_to_stream(ZULIP_TO_CHNL, topic=ZULIP_TO_TOPIC, msg=msg)
