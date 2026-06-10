import os
import subprocess
from pathlib import Path


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
    try:
        subprocess.run(["sync"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError("sync failed before dropping OS page caches") from e

    if os.geteuid() == 0:
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
            return
        except OSError as e:
            raise RuntimeError("Could not drop OS page caches") from e

    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            "Could not drop OS page caches. Disk-based benchmarking requires "
            "root or passwordless sudo for "
            "'echo 3 > /proc/sys/vm/drop_caches'."
        ) from e
