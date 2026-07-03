"""Host-RAM preflight for workloads that load their dataset fully into memory.

Kept free of pipeline imports so both the public API (``SynnoDB.check_ram_for_sf``)
and the workload providers (``WorkloadProvider.preflight_ram_check``) can use it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["IN_MEMORY_RAM_FACTOR", "RamCheck", "InsufficientRamError"]

# An in-memory engine needs well more RAM than the dataset's on-disk parquet size:
# parquet is compressed and column-encoded, and the engine adds indexes and per-query
# intermediates on top. 3x the parquet size is the required headroom.
IN_MEMORY_RAM_FACTOR = 3.0


class InsufficientRamError(RuntimeError):
    """The host's available RAM cannot hold the workload's dataset in memory.

    Raised by the pipeline's preflight before any generation work starts, so an
    in-memory run fails fast with the measured numbers instead of crashing OOM
    mid-run. Catch it to fall back to ``db_storage="ssd"`` or a smaller scale
    factor."""


@dataclass(frozen=True)
class RamCheck:
    """Result of a host-RAM preflight (``SynnoDB.check_ram_for_sf`` or the
    pipeline's automatic gate) - truthy iff the host's available RAM covers
    ``IN_MEMORY_RAM_FACTOR`` x the dataset's on-disk parquet size."""

    sf: float
    dataset_bytes: int  # summed on-disk parquet size of the SF's tables
    available_bytes: int  # host RAM available at check time

    @classmethod
    def measure(cls, sf: float, sf_dir: Path, tables: Sequence[str]) -> "RamCheck":
        """Measure a dataset directory against the host's currently available RAM.

        Sums the on-disk size of ``<table>.parquet`` for each table under
        ``sf_dir`` and reads the host's available RAM. Raises
        ``FileNotFoundError`` if any table's parquet file is absent."""
        import psutil

        dataset_bytes = 0
        missing: list[str] = []
        for table in tables:
            try:
                dataset_bytes += (sf_dir / f"{table}.parquet").stat().st_size
            except FileNotFoundError:
                missing.append(table)
        if missing:
            raise FileNotFoundError(
                f"Dataset at {sf_dir} is missing the parquet files for tables "
                f"{missing}."
            )
        return cls(
            sf=sf,
            dataset_bytes=dataset_bytes,
            available_bytes=psutil.virtual_memory().available,
        )

    @property
    def required_bytes(self) -> int:
        """In-memory bytes needed: ``IN_MEMORY_RAM_FACTOR`` x the on-disk dataset."""
        return math.ceil(self.dataset_bytes * IN_MEMORY_RAM_FACTOR)

    @property
    def sufficient(self) -> bool:
        return self.available_bytes >= self.required_bytes

    def __bool__(self) -> bool:
        return self.sufficient

    def __str__(self) -> str:
        gib = 1024**3
        verdict = "sufficient" if self.sufficient else "insufficient"
        return (
            f"RAM {verdict} for sf{self.sf:g}: dataset {self.dataset_bytes / gib:.2f} GiB "
            f"on disk, {self.required_bytes / gib:.2f} GiB required in memory "
            f"({IN_MEMORY_RAM_FACTOR:g}x), {self.available_bytes / gib:.2f} GiB available"
        )
