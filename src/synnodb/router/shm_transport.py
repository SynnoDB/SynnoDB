"""Shared-memory zero-copy Arrow transport — the Phase-3 data plane (Python side).

The router and the C++ engine worker exchange bulk data as Arrow IPC laid out in a
``/dev/shm`` (tmpfs) segment that both processes ``mmap``. On the read side the
Arrow arrays are **zero-copy views** into the mapping (Arrow IPC is offset-based and
position-independent, so no fixed-address mmap is needed — unlike the pointer-rich
``misc/misc/shm_test.cpp`` prototype). This module is the Python end; the C++ worker
implements the symmetric side (``ReadArrowTableFromShm`` / an Arrow-IPC shm writer).

Ownership & lifecycle (the hard, mandatory part):

* The **Python parent owns** every segment — it creates and ``unlink``s them, because
  the engine can be ``SIGKILL``'d and cannot self-clean.
* Segment names encode the owner pid: ``synnodb-<owner_pid>-<uid>.arrow``. A startup
  ``sweep_orphans`` removes segments whose owner pid is dead — orphans are a silent
  RAM leak.
* ``SegmentRef`` is the small, picklable handle passed to the worker over the control
  channel (env/pipe); it never carries data, only the segment name and byte length.
"""
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

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


@dataclass(frozen=True)
class SegmentRef:
    """A picklable handle to a shm segment (carried over the control plane)."""

    name: str            # file name under SHM_DIR
    nbytes: int          # payload length

    @property
    def path(self) -> Path:
        return SHM_DIR / self.name


class ShmWriter:
    """Owns shm segments for one engine epoch; creates, writes, and unlinks them.

    Use as a context manager (or call ``close``) so segments are reclaimed even on
    error. The owner pid is baked into every name for the orphan sweep.
    """

    def __init__(self, *, base_dir: Optional[Path] = None, owner_pid: Optional[int] = None) -> None:
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
        log.debug("wrote shm segment %s (%d rows, %d bytes)", name, table.num_rows, nbytes)
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
    source = pa.memory_map(str(path), "r")          # zero-copy mmap
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
        parts = path.name[len(_PREFIX):].split("-", 1)
        if not parts or not parts[0].isdigit():
            continue
        if _pid_alive(int(parts[0])):
            continue
        try:
            path.unlink()
            removed += 1
            log.debug("swept orphaned shm segment %s (dead owner pid %s)", path.name, parts[0])
        except FileNotFoundError:
            pass
    if removed:
        log.info("swept %d orphaned shm segment(s) from %s", removed, directory)
    return removed
