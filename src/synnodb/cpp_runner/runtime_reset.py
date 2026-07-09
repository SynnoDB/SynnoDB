"""Tear down all warm engine runtime state for this process, and guard that teardown against an
in-flight run.

The synthesis path keeps engines *warm* across runs: ``HotpatchPool`` caches a running ``db``
process per key, and ``shm_stage`` keeps each subset's ``/dev/shm`` Arrow segment resident so a
later run reuses it. Both assume the data behind a given key does not change under them - the C++
loader ingests its input exactly once per process (the loader stage is ``RunPolicy::OnChange`` on
``libloader.so``) and never re-reads ``SYNNODB_SHM_INGEST`` afterwards.

That assumption holds *within* a synthesis run but breaks at a **resync** - when the source data
behind the managed subsets is swapped out (re-snapshotted and the subsets rebuilt). A warm process
left over from before the swap would keep serving the previous snapshot's rows (stale results) and
keep its now-orphaned ``/dev/shm`` segment occupying RAM until interpreter exit. So a resync retires
the whole warm runtime here; the next run spawns fresh processes that load the new data.

The retirement must not race a run: terminating a warm process mid-query would corrupt that run, and
even between queries a data swap would let different batches of one run see different data. A run
therefore marks the warm runtime in use for its whole duration (:func:`warm_runtime_in_use`), and a
resync attempted while a run is in flight fails fast with :class:`ResyncInFlightError` rather than
tearing procs down under it. In the normal single-threaded flow (resync happens between runs, never
during one) the guard never fires; it only surfaces genuine concurrent misuse.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# Serializes teardown against run-scope entry, and protects ``_active_runs``.
_lock = threading.Lock()
_active_runs = (
    0  # in-flight runs holding the warm runtime (a depth count, so run scopes may nest)
)


class ResyncInFlightError(RuntimeError):
    """A resync was attempted while a run was still using the warm runtime."""


@contextmanager
def warm_runtime_in_use() -> Iterator[None]:
    """Mark the warm runtime in use for the duration of a run, so a concurrent resync fails fast
    instead of terminating a warm process mid-query."""
    global _active_runs
    with _lock:
        _active_runs += 1
    try:
        yield
    finally:
        with _lock:
            _active_runs -= 1


def reset_warm_runtime() -> None:
    """Terminate every warm hotpatch process and drop every staged ``/dev/shm`` segment. A no-op
    when nothing is warm, so it is safe to call unconditionally at a resync boundary. Raises
    :class:`ResyncInFlightError` if a run is in flight - a warm process must never be torn down
    under a running query (see :func:`warm_runtime_in_use`)."""
    from synnodb.cpp_runner.hotpatch.pool import HotpatchPool
    from synnodb.cpp_runner.shm_stage import clear_staged_segments

    # Hold the lock across the whole teardown so the check and the teardown are atomic: no run can
    # start between "no run in flight" and the procs actually going away.
    with _lock:
        if _active_runs > 0:
            raise ResyncInFlightError(
                f"resync attempted while {_active_runs} run(s) are in flight - resync between runs, "
                "not during one"
            )
        logger.info(
            "Resync: tearing down warm hotpatch processes and staged shm segments"
        )
        HotpatchPool.terminate_all()
        clear_staged_segments()
