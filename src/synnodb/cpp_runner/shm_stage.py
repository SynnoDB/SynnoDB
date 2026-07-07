"""Stage a DuckDB-native tier into ``/dev/shm`` for the synthesis engine.

The generated in-memory loader reads its tables from ``<SYNNODB_SHM_INGEST>/<table>.arrow`` when
that env var is set, and from parquet otherwise (one binary, runtime choice - see
``prepare_workspace_olap._gen_table_reads``). At serving time the router's ``ShmHotLoadEngine``
stages those segments; during synthesis we do the same for a DuckDB-native tier so the candidate
engine ingests the tier's ``tier.duckdb`` with no parquet on disk.

The segment format is exactly what the C++ ``ReadArrowTableFromShm`` maps: one Arrow IPC file per
table. The ingest directory is deterministic per (process, tier), so repeated batches for the same
tier reuse the same segments (matching the warm hotpatch-pool process, which loads once).
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Ingest dirs staged by this process, cleaned up at exit.
_STAGED: set[Path] = set()
_ATEXIT_REGISTERED = False
_PREFIX = "synno-synth-"


def _shm_base() -> Path:
    from synnodb.router.shm_transport import SHM_DIR

    return SHM_DIR


def _sweep_orphans(base: Path) -> None:
    """Remove ``synno-synth-<pid>-*`` dirs whose owning process is gone (crash cleanup)."""
    from synnodb.router.shm_transport import _pid_alive

    try:
        children = list(base.glob(f"{_PREFIX}*"))
    except OSError:
        return
    for child in children:
        parts = child.name[len(_PREFIX) :].split("-", 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        if pid != os.getpid() and not _pid_alive(pid):
            shutil.rmtree(child, ignore_errors=True)


def _cleanup() -> None:
    for d in list(_STAGED):
        shutil.rmtree(d, ignore_errors=True)
    _STAGED.clear()


def stage_tier_duckdb_to_shm(tier_db_path: Path | str) -> Path:
    """Materialize ``tier.duckdb``'s tables as ``/dev/shm`` Arrow segments and return the ingest
    directory (the value for ``SYNNODB_SHM_INGEST``).

    Idempotent per (process, tier): a completed staging is reused as-is without even opening the
    database, so calling this before every batch is cheap and keeps a warm engine's loaded
    snapshot valid. Refuses up front if the snapshot will not fit in shared memory (a mid-write
    ENOSPC would leave a partial segment).
    """
    import pyarrow as pa
    import pyarrow.ipc as ipc

    import duckdb

    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup)
        _ATEXIT_REGISTERED = True

    tier_db = Path(tier_db_path)
    base = _shm_base()
    base.mkdir(parents=True, exist_ok=True)
    _sweep_orphans(base)

    fingerprint = hashlib.sha256(str(tier_db.resolve()).encode()).hexdigest()[:12]
    ingest_dir = base / f"{_PREFIX}{os.getpid()}-{fingerprint}"
    marker = ingest_dir / ".complete"
    if marker.exists():
        return ingest_dir

    # Fresh (or previously-partial) staging: rebuild from scratch.
    shutil.rmtree(ingest_dir, ignore_errors=True)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(tier_db), read_only=True)
    try:
        tables = [r[0] for r in con.execute("PRAGMA show_tables").fetchall()]
        arrow_tables = {
            t: con.execute(f'SELECT * FROM "{t}"').to_arrow_table() for t in tables
        }
    finally:
        con.close()

    needed = sum(int(t.nbytes) for t in arrow_tables.values())
    usage = shutil.disk_usage(base)
    reserve = max(64 * 1024 * 1024, usage.total // 20)
    if needed + reserve > usage.free:
        shutil.rmtree(ingest_dir, ignore_errors=True)
        raise RuntimeError(
            f"not enough shared memory to stage DuckDB-native tier into {base} "
            f"(needed {needed / 1048576:.1f} MiB, free {usage.free / 1048576:.1f} MiB); "
            "register the workload with serve_from='parquet' or free /dev/shm."
        )

    total = 0
    for name, table in arrow_tables.items():
        seg = ingest_dir / f"{name}.arrow"
        with pa.OSFile(str(seg), "wb") as sink:
            with ipc.new_file(sink, table.schema) as writer:
                writer.write_table(table)
        total += seg.stat().st_size
    marker.touch()
    _STAGED.add(ingest_dir)
    logger.info(
        "staged DuckDB-native tier %s -> %s (%d tables, %.1f MiB)",
        tier_db.parent.name,
        ingest_dir,
        len(arrow_tables),
        total / 1048576,
    )
    return ingest_dir
