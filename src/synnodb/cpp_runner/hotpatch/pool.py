import atexit
import logging
from typing import Callable

from synnodb.cpp_runner.hotpatch.hotpatch_proc import HotpatchProc

logger = logging.getLogger(__name__)


class _HotpatchHolder:
    def __init__(self) -> None:
        self._runners: dict[str, HotpatchProc] = {}

    def get(self, key: str, factory: Callable[[], HotpatchProc]) -> HotpatchProc:
        runner = self._runners.get(key)
        if runner is None:
            runner = factory()
            self._runners[key] = runner
        return runner

    def terminate(self, key: str) -> bool:
        runner = self._runners.pop(key, None)
        if runner is None:
            return False
        runner.terminate()
        return True

    def terminate_all(self) -> None:
        for key in list(self._runners.keys()):
            self.terminate(key)


def _terminate_all_at_exit() -> None:
    for key in list(HotpatchPool._runners.keys()):
        try:
            HotpatchPool.terminate(key)
        except Exception:
            logger.exception(
                "Failed to terminate cached runner during interpreter exit: %s", key
            )


HotpatchPool = _HotpatchHolder()
atexit.register(_terminate_all_at_exit)
