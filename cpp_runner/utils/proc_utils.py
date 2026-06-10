import logging
import os
import signal
import time

logger = logging.getLogger(__name__)


class ProcTreeTimeoutKiller:
    def __init__(self, root_pid: int, timeout: int, *, min_descendant_depth: int = 0):
        self.root_pid = root_pid
        self.timeout = timeout
        self.min_descendant_depth = min_descendant_depth
        self.start: float | None = None
        self.killed = False

    def expired(self) -> bool:
        if self.timeout <= 0:
            return False
        if self.start is None:
            return False
        return (time.monotonic() - self.start) >= self.timeout

    def enforce(self) -> None:
        """
        Kill the rightmost eligible descendant exactly once when timeout expires.
        """
        if self.killed:
            return

        victim, depth = self._rightmost_descendant(self.root_pid)
        if depth < self.min_descendant_depth:
            self.start = None
            return
        if self.start is None:
            self.start = time.monotonic()
            return
        if not self.expired():
            return

        logger.warning(f"Timeout, killing {victim}")
        self._kill(victim)

        self.killed = True

    def _children(self, pid: int) -> list[int]:
        path = f"/proc/{pid}/task/{pid}/children"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read().strip()
        except OSError:
            return []

        if not data:
            return []

        out: list[int] = []
        for part in data.split():
            try:
                out.append(int(part))
            except ValueError:
                pass
        return out

    def _rightmost_descendant(self, pid: int) -> tuple[int, int]:
        cur = pid
        depth = 0
        while True:
            kids = self._children(cur)
            if not kids:
                return cur, depth
            cur = kids[-1]  # "most right"
            depth += 1

    def _kill(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
