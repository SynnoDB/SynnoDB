import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
_SYS_NODE_PATH = Path("/sys/devices/system/node")

# Hugepages only improve engine performance; they are never required for correctness.
# Warn at most once when they cannot be set so a multi-node host does not repeat the
# message for every NUMA node.
_warned_no_hugepages = False


def get_num_numa_nodes() -> int:
    """Return the number of NUMA nodes visible in sysfs."""
    if not _SYS_NODE_PATH.exists():
        return 1
    return max(
        1,
        sum(
            1
            for path in _SYS_NODE_PATH.iterdir()
            if path.is_dir()
            and path.name.startswith("node")
            and path.name[4:].isdigit()
        ),
    )


def set_hugepages(node=0, page_kb=2048, count=0):
    """Set hugepages via passwordless sudo, best-effort.

    Requires a sudoers rule like:
      username ALL=(root) NOPASSWD: /usr/bin/tee /sys/devices/system/node/node*/hugepages/*/nr_hugepages

    ``sudo -n`` never prompts for a password: when the right is not granted the call
    fails immediately, so we warn once and continue on default pages rather than
    blocking startup on interactive input or aborting it. Attempting the exact ``tee``
    command (instead of first probing general sudo access) is also what makes a
    command-scoped NOPASSWD rule like the one above work. Returns ``True`` only if the
    pages were actually set.
    """
    global _warned_no_hugepages
    path = (
        f"/sys/devices/system/node/node{node}/hugepages/"
        f"hugepages-{page_kb}kB/nr_hugepages"
    )
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/bin/tee", path],
            input=f"{count}\n",
            text=True,
            stdout=subprocess.DEVNULL,  # suppress tee echo
            stderr=subprocess.PIPE,
            check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if not _warned_no_hugepages:
            _warned_no_hugepages = True
            logger.warning(
                "Could not set hugepages (passwordless sudo unavailable or the command "
                "failed); continuing on default pages. To enable, grant passwordless "
                "sudo for '/usr/bin/tee %s'. Details: %s",
                path,
                exc,
            )
        return False
