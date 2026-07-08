"""Stage a DuckDB-native subset into ``/dev/shm`` for the synthesis engine.

The generated in-memory loader reads its tables from ``<SYNNODB_SHM_INGEST>/<table>.arrow`` when
that env var is set, and from parquet otherwise (one binary, runtime choice - see
``prepare_workspace_olap._gen_table_reads``). At serving time the router's ``ShmHotLoadEngine``
stages those segments; during synthesis we do the same for a DuckDB-native subset so the candidate
engine ingests the subset's ``subset.duckdb`` with no parquet on disk.

The segment format, budget policy, and crash-cleanup rules are the ones the router's
``ShmHotLoadEngine`` uses - shared verbatim from :mod:`synnodb.router.shm_transport` so both
writers agree with the C++ ``ReadArrowTableFromShm``. The ingest directory is keyed on the subset
file's *content* (path + mtime + size), so a subset rebuilt in place is re-staged rather than
served stale, while an unchanged subset reuses its segments without reopening the database.
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


def _content_fingerprint(subset_db: Path) -> str:
    """A short digest of the subset's identity *and* content (resolved path, mtime, size), so a
    subset regenerated at the same path yields a different ingest dir instead of reusing stale
    segments. Falls back to the path alone if the file cannot be stat-ed."""
    key = str(subset_db.resolve())
    try:
        st = subset_db.stat()  # follows a benchmark-subset symlink to the real file
        key = f"{key}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        pass
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _cleanup() -> None:
    for d in list(_STAGED):
        shutil.rmtree(d, ignore_errors=True)
    _STAGED.clear()


def stage_subset_duckdb_to_shm(subset_db_path: Path | str) -> Path:
    """Materialize ``subset.duckdb``'s tables as ``/dev/shm`` Arrow segments and return the ingest
    directory (the value for ``SYNNODB_SHM_INGEST``).

    Idempotent per (process, subset content): a completed staging of the same file content is
    reused as-is without even opening the database, so calling this before every batch is cheap and
    keeps a warm engine's loaded snapshot valid. A subset rebuilt in place (new content) re-stages.
    Refuses up front if the snapshot will not fit in shared memory (a mid-write ENOSPC would leave a
    partial segment).
    """
    import duckdb

    from synnodb.duckdb_compat.db_io import list_tables
    from synnodb.router.shm_transport import (
        SHM_DIR,
        check_shm_budget,
        proc_start_time,
        sweep_ingest_orphans,
        write_arrow_segments,
    )

    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup)
        _ATEXIT_REGISTERED = True

    subset_db = Path(subset_db_path)
    base = SHM_DIR
    base.mkdir(parents=True, exist_ok=True)

    # Tag the dir with pid + process start time so the shared sweep can reap a crashed run's
    # segments even after the PID is recycled (start time distinguishes the incarnations).
    pid = os.getpid()
    start = proc_start_time(pid) or "0"
    fingerprint = _content_fingerprint(subset_db)
    ingest_dir = base / f"{_PREFIX}{pid}-{start}-{fingerprint}"
    marker = ingest_dir / ".complete"
    if marker.exists():
        return ingest_dir

    sweep_ingest_orphans(base, _PREFIX)

    # Fresh (or previously-partial) staging: rebuild from scratch.
    shutil.rmtree(ingest_dir, ignore_errors=True)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(subset_db), read_only=True)
    try:
        arrow_tables = {
            t: con.execute(f"SELECT * FROM {_quote(t)}").to_arrow_table()
            for t in list_tables(con)
        }
    finally:
        con.close()

    needed = sum(int(t.nbytes) for t in arrow_tables.values())
    fits, free, _reserve = check_shm_budget(base, needed)
    if not fits:
        shutil.rmtree(ingest_dir, ignore_errors=True)
        raise RuntimeError(
            f"not enough shared memory to stage DuckDB-native subset into {base} "
            f"(needed {needed / 1048576:.1f} MiB, free {free / 1048576:.1f} MiB); "
            "register the workload with serve_from='parquet' or free /dev/shm."
        )

    total = write_arrow_segments(ingest_dir, arrow_tables)
    marker.touch()
    _STAGED.add(ingest_dir)
    logger.info(
        "staged DuckDB-native subset %s -> %s (%d tables, %.1f MiB)",
        subset_db.parent.name,
        ingest_dir,
        len(arrow_tables),
        total / 1048576,
    )
    return ingest_dir


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'
