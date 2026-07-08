"""Tear down all warm engine runtime state for this process.

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
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def reset_warm_runtime() -> None:
    """Terminate every warm hotpatch process and drop every staged ``/dev/shm`` segment. A no-op
    when nothing is warm, so it is safe to call unconditionally at a resync boundary."""
    from synnodb.cpp_runner.hotpatch.pool import HotpatchPool
    from synnodb.cpp_runner.shm_stage import clear_staged_segments

    logger.info("Resync: tearing down warm hotpatch processes and staged shm segments")
    HotpatchPool.terminate_all()
    clear_staged_segments()
