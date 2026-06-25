import logging
import os
import signal
import time

logger = logging.getLogger(__name__)


class ProcTreeTimeoutKiller:
    def __init__(
        self,
        root_pid: int,
        timeout: int,
        *,
        min_descendant_depth: int = 0,
        build_timeout: int = 0,
        build_min_depth: int = 1,
    ):
        self.root_pid = root_pid
        self.timeout = timeout
        self.min_descendant_depth = min_descendant_depth
        # Independent, usually larger, deadline for the build/load stages — any
        # live descendant shallower than the query stage (depth in
        # [build_min_depth, min_descendant_depth)). 0 disables it, preserving the
        # original query-only behaviour. Without this, a livelock inside the
        # build/loader stage never spawns the depth-`min_descendant_depth` query
        # leaf, so the query timer never arms and the hang runs unbounded.
        self.build_timeout = build_timeout
        self.build_min_depth = build_min_depth
        self.start: float | None = None
        self.phase: str | None = None
        self.killed = False

    def _active_stage(self, depth: int) -> tuple[str | None, int]:
        """Map the rightmost-descendant depth to its (stage, deadline_seconds).

        The query stage takes priority; anything shallower (but still a real
        descendant) is treated as the build/load stage when a build_timeout is
        configured. Returns (None, 0) when nothing should be timed.
        """
        if self.timeout > 0 and depth >= self.min_descendant_depth:
            return "query", self.timeout
        if self.build_timeout > 0 and depth >= self.build_min_depth:
            return "build", self.build_timeout
        return None, 0

    def enforce(self) -> None:
        """
        Kill the rightmost eligible descendant exactly once when its stage's
        deadline expires. The query and (longer) build stages are timed
        independently; the timer restarts whenever the active stage changes so
        build time is never charged against the query budget and vice versa.
        """
        if self.killed:
            return

        victim, depth = self._rightmost_descendant(self.root_pid)
        stage, deadline = self._active_stage(depth)

        if stage is None:
            self.start = None
            self.phase = None
            return
        if self.phase != stage or self.start is None:
            self.phase = stage
            self.start = time.monotonic()
            return
        if (time.monotonic() - self.start) < deadline:
            return

        logger.warning(
            f"Timeout: {stage} stage exceeded {deadline}s; killing pid {victim} (depth {depth})"
        )
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
