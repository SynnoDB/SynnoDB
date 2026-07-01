import atexit
import logging
from typing import Callable, Optional

from synnodb.cpp_runner.hotpatch.hotpatch_proc import HotpatchProc

logger = logging.getLogger(__name__)


class _HotpatchHolder:
    def __init__(self) -> None:
        self._runners: dict[str, HotpatchProc] = {}
        # Per-key build fingerprint the warm runner was launched against. Used to
        # restart a runner whose inputs changed in a way an in-place hotpatch
        # cannot absorb (a libloader.so source change makes its in-RAM Arrow input
        # stale, so the whole engine must restart).
        self._fingerprints: dict[str, Optional[str]] = {}

    def get(
        self,
        key: str,
        factory: Callable[[], HotpatchProc],
        fingerprint: Optional[str] = None,
    ) -> HotpatchProc:
        runner = self._runners.get(key)
        stored = self._fingerprints.get(key)
        if (
            runner is not None
            and fingerprint is not None
            and stored is not None
            and stored != fingerprint
        ):
            # The warm runner was built against a different fingerprint; the
            # hotpatch loop cannot reload this change in place, so retire it and
            # build a fresh engine below.
            logger.info("Restarting warm runner %s: build fingerprint changed", key)
            self.terminate(key)
            runner = None
        if runner is None:
            runner = factory()
            self._runners[key] = runner
            self._fingerprints[key] = fingerprint
        return runner

    def terminate(self, key: str) -> bool:
        self._fingerprints.pop(key, None)
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
