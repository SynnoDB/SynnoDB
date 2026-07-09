"""Shared-memory zero-copy Arrow transport (Python side of the ``/dev/shm`` data plane).

Bulk data crosses between the Python parent and a C++ engine as Arrow IPC written into a
``/dev/shm`` (tmpfs) segment that both processes ``mmap``. On the read side the Arrow arrays
are **zero-copy views** into the mapping (Arrow IPC is offset-based and position-independent,
so no fixed-address mmap is needed). This module is the canonical Python end of that wire
format, with two consumers:

* The serving engines (``router.process_engine``) take ``SHM_DIR`` and ``_pid_alive`` from
  here. ``ShmHotLoadEngine`` writes its ingest segments into ``/dev/shm`` and the generated
  C++ loader maps them zero-copy via ``ReadArrowTableFromShm`` (``cpp_helpers/shm_arrow_loader.hpp``).
* ``ShmWriter`` / ``read_table`` are the reference producer/consumer that round-trips the C++
  shm helpers (``shm_arrow_{loader,writer}.hpp``) in ``tests/test_cpp_shm.py``, pinning the
  exact byte format the C++ side must read and write.

Ownership & lifecycle (the hard, mandatory part):

* The **Python parent owns** every segment — it creates and ``unlink``s them, because the
  engine can be ``SIGKILL``'d and cannot self-clean.
* Segment names encode the owner pid (``synnodb-<owner_pid>-<seq>.arrow``); ``sweep_orphans``
  removes segments whose owner pid is dead — orphans are a silent RAM leak.
* ``SegmentRef`` is a small, picklable handle (segment name + byte length) — never data.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional

import pyarrow as pa
import pyarrow.ipc as ipc

log = logging.getLogger("synnodb.router.shm")

# tmpfs on Linux; overridable for tests / portability.
SHM_DIR = Path(os.environ.get("SYNNODB_SHM_DIR", "/dev/shm"))
_PREFIX = "synnodb-"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


# --- shared shm-ingest staging primitives (used by the serving ShmHotLoadEngine and the
#     synthesis-time shm_stage; kept here so both write the exact same segment format, budget
#     policy, and crash-cleanup rules the C++ ``ReadArrowTableFromShm`` depends on) ------------
def proc_start_time(pid: int) -> Optional[str]:
    """The owner process's start time (``/proc/<pid>/stat`` field 22, in clock ticks since boot),
    used to tell a recycled PID from the original owner. None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
        # field 2 (comm) is parenthesized and may contain spaces/parens; split after the last ')'.
        post = data[data.rfind(")") + 2 :].split()
        return post[19]  # field 22 overall -> index 19 among the post-comm fields
    except (OSError, IndexError):
        return None


def sweep_ingest_orphans(base: Path, prefix: str) -> int:
    """Remove ``<prefix><pid>-<starttime>-*`` ingest dirs under ``base`` whose owner is gone, so a
    SIGKILL'd process does not leak shm. Reaps when the PID is dead OR its current start time
    differs from the tag (the PID was recycled into an unrelated live process) - PID-alive alone
    would keep a genuine orphan forever after PID reuse. Only touches dirs with ``prefix``."""
    removed = 0
    try:
        children = list(base.glob(f"{prefix}*"))
    except OSError:
        return 0
    for path in children:
        parts = path.name[len(prefix) :].split("-")
        pid_str = parts[0] if parts else ""
        tagged_start = parts[1] if len(parts) > 1 else None
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if _pid_alive(pid) and (
            tagged_start is None or proc_start_time(pid) in (None, tagged_start)
        ):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def check_shm_budget(base: Path, needed_bytes: int) -> tuple[bool, int, int]:
    """Whether ``needed_bytes`` of Arrow segments fit in the tmpfs at ``base`` while keeping a
    reserve free (a mid-write ENOSPC would leave a partial segment). Returns
    ``(fits, free_bytes, reserve_bytes)``; each caller raises its own typed error on a miss."""
    usage = shutil.disk_usage(base)
    reserve = max(64 * 1024 * 1024, usage.total // 20)
    return (needed_bytes + reserve <= usage.free, usage.free, reserve)


def write_arrow_segments(ingest_dir: Path, tables: Mapping[str, pa.Table]) -> int:
    """Write each ``{name: table}`` as an Arrow-IPC ``<name>.arrow`` file under ``ingest_dir`` -
    exactly the format the C++ ``ReadArrowTableFromShm`` maps. Returns the total bytes written; on
    any failure the partial ``ingest_dir`` is removed and the error propagates."""
    total = 0
    try:
        for name, table in tables.items():
            seg = ingest_dir / f"{name}.arrow"
            with pa.OSFile(str(seg), "wb") as sink:
                with ipc.new_file(sink, table.schema) as writer:
                    writer.write_table(table)
            total += seg.stat().st_size
    except BaseException:
        shutil.rmtree(ingest_dir, ignore_errors=True)
        raise
    return total


@dataclass(frozen=True)
class SegmentRef:
    """A picklable handle to a shm segment (carried over the control plane)."""

    name: str  # file name under SHM_DIR
    nbytes: int  # payload length

    @property
    def path(self) -> Path:
        return SHM_DIR / self.name


class ShmWriter:
    """Owns shm segments for one engine epoch; creates, writes, and unlinks them.

    Use as a context manager (or call ``close``) so segments are reclaimed even on
    error. The owner pid is baked into every name for the orphan sweep.
    """

    def __init__(
        self, *, base_dir: Optional[Path] = None, owner_pid: Optional[int] = None
    ) -> None:
        self._dir = Path(base_dir) if base_dir is not None else SHM_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        # The pid baked into names for the orphan sweep. A worker passes the *parent*
        # pid so result segments are reaped if the parent dies (the parent owns them).
        self._owner = owner_pid if owner_pid is not None else os.getpid()
        self._counter = 0
        self._segments: List[Path] = []

    def write_table(self, table: pa.Table) -> SegmentRef:
        """Serialize *table* as an Arrow IPC **file** into a fresh shm segment."""
        self._counter += 1
        name = f"{_PREFIX}{self._owner}-{self._counter:06d}.arrow"
        path = self._dir / name
        # Write the Arrow IPC file format directly into the (RAM-backed) segment.
        with pa.OSFile(str(path), "wb") as sink:
            with ipc.new_file(sink, table.schema) as writer:
                writer.write_table(table)
        nbytes = path.stat().st_size
        self._segments.append(path)
        # 0600: never readable across users.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.debug(
            "wrote shm segment %s (%d rows, %d bytes)", name, table.num_rows, nbytes
        )
        return SegmentRef(name=name, nbytes=nbytes)

    def unlink(self, ref: SegmentRef) -> None:
        path = self._dir / ref.name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._segments = [p for p in self._segments if p != path]

    def close(self) -> None:
        for path in list(self._segments):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._segments.clear()

    def __enter__(self) -> "ShmWriter":
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False


def read_table(ref_or_path, *, base_dir: Optional[Path] = None) -> pa.Table:
    """Map a shm segment and return its Arrow ``Table`` **zero-copy**.

    The returned table's buffers are views into the memory mapping; the mapping is
    kept alive by the Arrow objects, so it is valid for the table's lifetime.
    """
    if isinstance(ref_or_path, SegmentRef):
        path = (Path(base_dir) if base_dir is not None else SHM_DIR) / ref_or_path.name
    else:
        path = Path(ref_or_path)
    source = pa.memory_map(str(path), "r")  # zero-copy mmap
    reader = ipc.open_file(source)
    return reader.read_all()


def sweep_orphans(*, base_dir: Optional[Path] = None) -> int:
    """Unlink segments whose owner pid is no longer alive. Returns the count removed.

    Run at startup (and periodically) so a crashed/`SIGKILL`'d worker's segments do
    not leak RAM. Only touches files matching our ``synnodb-<pid>-`` convention.
    """
    directory = Path(base_dir) if base_dir is not None else SHM_DIR
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob(f"{_PREFIX}*"):
        parts = path.name[len(_PREFIX) :].split("-", 1)
        if not parts or not parts[0].isdigit():
            continue
        if _pid_alive(int(parts[0])):
            continue
        try:
            path.unlink()
            removed += 1
            log.debug(
                "swept orphaned shm segment %s (dead owner pid %s)", path.name, parts[0]
            )
        except FileNotFoundError:
            pass
    if removed:
        log.info("swept %d orphaned shm segment(s) from %s", removed, directory)
    return removed
