import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Dropping OS page caches only sharpens cold-cache benchmark timings; it is never
# required for correctness. When it cannot be done - no root, no passwordless sudo, or
# the write fails - we warn once and continue rather than prompting for a password or
# aborting the run. Warn-once keeps a per-query benchmark loop from flooding the logs.
_warned_cannot_drop = False


def _warn_cannot_drop_caches_once(reason: str) -> None:
    global _warned_cannot_drop
    if _warned_cannot_drop:
        return
    _warned_cannot_drop = True
    logger.warning(
        "Could not drop OS page caches (%s); continuing with caches intact. "
        "Disk-based benchmark timings may reflect warm OS caches. To drop caches, run "
        "as root or grant passwordless sudo for 'echo 3 > /proc/sys/vm/drop_caches'.",
        reason,
    )


def is_memory_backed(path: Path) -> bool:
    mount_types = {"tmpfs", "ramfs"}
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False

    resolved_path = path.resolve()
    best_mount = Path("/")
    best_type = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = Path(parts[1].replace("\\040", " ")).resolve()
        try:
            resolved_path.relative_to(mount_point)
        except ValueError:
            continue
        if len(mount_point.parts) >= len(best_mount.parts):
            best_mount = mount_point
            best_type = parts[2]
    return best_type in mount_types


def drop_os_caches() -> None:
    """Best-effort drop of the OS page cache before a cold-cache measurement.

    Never prompts for a password and never aborts: when the caches cannot be dropped
    (no root, no passwordless sudo, or the write fails) it warns once and returns so
    the benchmark continues - only its cold-cache accuracy is affected.
    """
    try:
        subprocess.run(["sync"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        _warn_cannot_drop_caches_once(f"sync failed: {exc}")
        return

    if os.geteuid() == 0:
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
        except OSError as exc:
            _warn_cannot_drop_caches_once(f"root write failed: {exc}")
        return

    # Non-root: attempt a passwordless drop. ``-n`` makes sudo fail immediately instead
    # of prompting for a password when the right is not granted, so a benchmark never
    # blocks waiting on interactive input.
    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        _warn_cannot_drop_caches_once(f"passwordless sudo unavailable: {exc}")
