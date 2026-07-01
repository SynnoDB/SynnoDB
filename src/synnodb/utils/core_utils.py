import os

import psutil


def get_cores_for_current_machine(
    leave_core_0_out: bool = True,
    allow_hyperthreading: bool = True,
    ncores_to_use: int | None = None,
):
    num_physical = psutil.cpu_count(logical=False) or 1  # physical cores
    num_logical = psutil.cpu_count(logical=True) or 1  # logical cores (with HT)

    assert num_logical >= num_physical, (
        "Logical cores should be greater than or equal to physical cores."
    )

    # Get the current process's CPU affinity (which cores it can run on)
    p = psutil.Process(os.getpid())
    core_ids = sorted(p.cpu_affinity())

    # If hyperthreading is disabled, keep only one logical core per physical core.
    # Physical core N maps to logical cores N and N+num_physical (common layout).
    if not allow_hyperthreading and num_logical > num_physical:
        ht_siblings = set(range(num_physical, num_logical))
        core_ids = [c for c in core_ids if c not in ht_siblings]

    # Optionally leave core 0 out to avoid OS interference
    if leave_core_0_out:
        core_ids = [c for c in core_ids if c != 0]

    # Limit the number of cores to use if specified
    if ncores_to_use is not None:
        core_ids = core_ids[:ncores_to_use]

    return len(core_ids), core_ids


def core_ids_to_env(core_ids: list[int] | None) -> str:
    """Build the ``CORE_IDS`` env value the C++ thread pool reads (``init_thread_pool``).

    A non-empty list -> that many worker threads, each pinned to the listed core. ``None``
    or an empty list -> ``"1"``: a single thread, which both keeps the pool on its serial
    fast path and stops ``init_thread_pool`` from falling back to "use every hardware core"
    when no list is provided. Generation (the RunTool) and serving (the router) build the
    env through this one function so the engine sees an identical thread count either way.
    """
    if not core_ids:
        return "1"
    return ",".join(str(c) for c in core_ids)
