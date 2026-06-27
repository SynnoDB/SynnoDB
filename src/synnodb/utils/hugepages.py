import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
_SYS_NODE_PATH = Path("/sys/devices/system/node")


def has_passwordless_sudo(logger=None):
    """
    Returns True if sudo can run without prompting for a password.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


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
    """
    Set hugepages using sudo without a password prompt.

    Requires a sudoers rule like:
      username ALL=(root) NOPASSWD: /usr/bin/tee /sys/devices/system/node/node*/hugepages/*/nr_hugepages
    """

    if not has_passwordless_sudo(logger):
        logger.warning("Passwordless sudo is not available. Not setting hugepages.")
        return False

    path = (
        f"/sys/devices/system/node/node{node}/hugepages/"
        f"hugepages-{page_kb}kB/nr_hugepages"
    )

    subprocess.run(
        ["sudo", "/usr/bin/tee", path],
        input=f"{count}\n",
        text=True,
        stdout=subprocess.DEVNULL,  # suppress tee echo
        stderr=subprocess.PIPE,
        check=True,
    )
