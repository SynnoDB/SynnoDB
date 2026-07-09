import logging
import os

import psutil

logger = logging.getLogger(__name__)


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


def resolve_target_cores(threads: int | None) -> tuple[int, list[int]]:
    """Resolve the DuckDB-style ``threads`` config to concrete worker cores.

    None (unset) -> 1: a single-threaded engine, the default.
    0            -> every usable core on this machine (auto-detect).
    N >= 1       -> up to N cores (clamped to the machine's usable cores).
    """
    if threads is None:
        ncores_to_use: int | None = 1
    elif threads == 0:
        ncores_to_use = None  # all usable cores
    elif threads >= 1:
        ncores_to_use = threads
    else:
        raise ValueError(
            f"threads must be 0 (all cores), None (default 1), or >= 1, got {threads}"
        )

    count, core_ids = get_cores_for_current_machine(ncores_to_use=ncores_to_use)
    if ncores_to_use is not None and count < ncores_to_use:
        logger.warning(
            "threads=%d requested but only %d usable cores are available; using %d.",
            ncores_to_use,
            count,
            count,
        )
    return count, core_ids


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    for divisor in range(2, int(n**0.5) + 1):
        if n % divisor == 0:
            return False
    return True


def largest_prime_le(n: int) -> int | None:
    """Largest prime ``p`` with ``2 < p <= n``, or ``None`` if none exists (``n < 3``)."""
    for candidate in range(n, 2, -1):
        if _is_prime(candidate):
            return candidate
    return None


def clamp_threads_to_available(requested_threads: int, max_available: int) -> int:
    """Clamp a user-selected thread count to the machine's usable cores.

    Selecting more threads than there are cores oversubscribes the machine: multiple
    workers contend for the same core, so wall-clock time degrades instead of improving.
    When the user picks such a count (e.g. via the TPC-H benchmark's ``--num_threads``),
    we catch it, warn, and cap the count at ``max_available``.
    """
    if requested_threads > max_available:
        logger.warning(
            "num_threads=%d oversubscribes the machine (%d usable cores available); "
            "clamping to %d.",
            requested_threads,
            max_available,
            max_available,
        )
        return max_available
    return requested_threads


def validation_thread_counts(target_threads: int, max_available: int) -> list[int]:
    """Thread counts to exercise a query at during multi-threaded validation.

    Beyond the serving parallelism (``target_threads``) we also probe 1, 8, and the
    largest prime ``> 2`` and ``<= target_threads``. Prime / non-power-of-two widths
    make the work split unevenly, surfacing races that a tidy power-of-two split hides,
    and 8 is a common serving width worth covering regardless of the target.

    These are internal probe widths, so any that would exceed ``max_available`` are
    silently capped to it (we pin one worker per core, so a wider count buys nothing) -
    the oversubscription *warning* belongs at the user's ``threads`` selection, not
    here. The result is deduped and sorted ascending.
    """
    raw = {1, 8, target_threads}
    prime = largest_prime_le(target_threads)
    if prime is not None:
        raw.add(prime)
    return sorted({min(count, max_available) for count in raw})


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
