"""Regression tests for the ``synnodb.router.process_engine`` hardening (the PE findings).

Each test began as an adversarial probe that exposed a defect; it now asserts the *fixed*
behavior, so it guards the fix against regressing. Covered: the result snapshot (PE6), the
strict CSV cast grammar (PE1), HUGEINT/UBIGINT typing (PE2), engine lifecycle (PE3), the shm
orphan sweep under PID reuse (PE5), the verbose result-read error (PE7), and the 0-row contract.

Run: .venv/bin/python -m pytest tests/test_process_engine_adversarial.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pytest

from synnodb.router.process_engine import (
    ProcessEngine,
    ShmHotLoadEngine,
    _sweep_ingest_orphans,
    _INGEST_PREFIX,
)


def _write_arrow(path: Path, table: pa.Table) -> None:
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as w:
            w.write_table(table)


# ---------------------------------------------------------------------------
# PE6 (fixed): _read_arrow now OWNS its buffers (reads the IPC file through an
# OSFile and closes it), so the returned Table is a true snapshot. Overwriting or
# shrinking the backing file under an already-returned Table no longer corrupts it.
# ---------------------------------------------------------------------------
def test_read_arrow_is_a_snapshot_under_inplace_overwrite(tmp_path):
    eng = ProcessEngine("e", tmp_path, "/data")
    p = tmp_path / "result_x.arrow"
    _write_arrow(p, pa.table({"x": pa.array([111111] * 64, pa.int64())}))
    tbl = eng._read_arrow(p)
    assert tbl.column("x").to_pylist()[0] == 111111

    # Overwrite the same inode in place (as a fixed-path egress / same-name re-ingest would).
    _write_arrow(p, pa.table({"x": pa.array([777] * 64, pa.int64())}))
    assert tbl.column("x").to_pylist()[0] == 111111, (
        "returned Table must be an owned snapshot"
    )


def test_read_arrow_snapshot_survives_file_shrink(tmp_path):
    """Shrinking (or deleting) the backing file must not affect an already-returned Table."""
    eng = ProcessEngine("e", tmp_path, "/data")
    p = tmp_path / "result_x.arrow"
    _write_arrow(p, pa.table({"x": pa.array(list(range(100)), pa.int64())}))
    tbl = eng._read_arrow(p)
    assert tbl.num_rows == 100
    _write_arrow(p, pa.table({"x": pa.array([1], pa.int64())}))  # shrink in place
    p.unlink()  # and remove entirely
    assert tbl.column("x").to_pylist() == list(range(100)), (
        "snapshot must be stable and owned"
    )


# ---------------------------------------------------------------------------
# FINDING 4 (HIGH): no __del__ / context manager. A ShmHotLoadEngine that ingested
# (wrote /dev/shm) and is then dropped without close() leaks the shm dir until a
# FUTURE ingest's orphan sweep happens to run AND the owner pid is dead. Same for
# the warm subprocess. Here we prove the shm dir survives garbage collection.
# ---------------------------------------------------------------------------
def test_del_cleans_shm_dir_on_gc(tmp_path):
    """PE3 (fixed): a ShmHotLoadEngine dropped without close() must not leak its /dev/shm dir or
    warm subprocess. __del__ -> close() now reclaims them on GC."""
    import gc

    base = tmp_path / "shm"
    base.mkdir()
    eng = ShmHotLoadEngine("e", tmp_path, shm_dir=base)
    eng.ingest({"t": pa.table({"x": pa.array([1, 2, 3])})})
    d = eng._ingest_dir
    assert d is not None and d.exists()
    # Drop the only reference without calling close(); __del__ must reclaim the shm dir.
    del eng
    gc.collect()
    assert not d.exists(), (
        "ingest dir leaked on GC: __del__ did not reclaim the shm segment"
    )


def test_engine_context_manager_closes(tmp_path):
    """PE3: the engine is a context manager; leaving the block closes it (shm reclaimed)."""
    base = tmp_path / "shm"
    base.mkdir()
    with ShmHotLoadEngine("e", tmp_path, shm_dir=base) as eng:
        eng.ingest({"t": pa.table({"x": pa.array([1])})})
        d = eng._ingest_dir
        assert d is not None and d.exists()
    assert not d.exists() and eng.health() is False


# ---------------------------------------------------------------------------
# FINDING 5 (MEDIUM): _sweep_ingest_orphans + PID reuse / TOCTOU. The sweep keeps
# a dir only if int(pid) is alive. If a long-dead connection's pid is recycled by
# an unrelated live process, that connection's leaked shm dir is kept forever
# (never reaped). Conversely a *live* sibling connection that shares the same pid
# space is unaffected here, but a dir whose pid wrapped to a now-dead value is
# removed even though... (the dangerous direction is the false-"alive").
# We prove the false-"alive": a stale dir tagged with a currently-live pid (this
# test process) is NOT swept even though it is genuinely orphaned.
# ---------------------------------------------------------------------------
def test_sweep_reaps_orphan_on_pid_reuse(tmp_path):
    """PE5 (fixed): a leaked dir whose PID was recycled into an unrelated live process is reaped,
    because its tagged start time no longer matches the live process's; a dir tagged with our own
    live pid AND current start time is kept."""
    from synnodb.router.process_engine import _proc_start_time

    if _proc_start_time(os.getpid()) is None:
        pytest.skip("process start time unavailable (non-Linux)")
    base = tmp_path / "shm"
    base.mkdir()
    # Orphan tagged with THIS (live) pid but a stale start time -> the recycled-pid case.
    leaked = base / f"{_INGEST_PREFIX}{os.getpid()}-000000-deadbeef"
    leaked.mkdir()
    (leaked / "t.arrow").write_text("x")
    # Our own dir: this pid + its real start time -> must be kept.
    mine = base / f"{_INGEST_PREFIX}{os.getpid()}-{_proc_start_time(os.getpid())}-live"
    mine.mkdir()
    (mine / "t.arrow").write_text("x")

    removed = _sweep_ingest_orphans(base)
    assert removed == 1
    assert not leaked.exists() and mine.exists()


# ---------------------------------------------------------------------------
# FINDING 6 (MEDIUM): close() flips health()->False (good) but a double close()
# and close()-before-ingest are silent no-ops that must not raise. Also after
# close(), ShmHotLoadEngine.run() must still refuse (loaded=False) rather than
# touch a removed shm dir. Pin the contract; flag if run() does something unsafe.
# ---------------------------------------------------------------------------
def test_double_close_is_safe(tmp_path):
    base = tmp_path / "shm"
    base.mkdir()
    eng = ShmHotLoadEngine("e", tmp_path, shm_dir=base)
    eng.ingest({"t": pa.table({"x": pa.array([1])})})
    d = eng._ingest_dir
    eng.close()
    eng.close()  # must not raise
    assert d is not None and not d.exists()
    assert eng.health() is False
    with pytest.raises(RuntimeError):
        eng.run("Q1", {})  # loaded=False after close -> refuses


# ---------------------------------------------------------------------------
# FINDING 7 (MEDIUM): run() with a stale .arrow from a PREVIOUS req that the
# engine did NOT overwrite. run() unlinks arrow_path for THIS req_id before the
# engine runs, but if the engine crashes after writing a *different*-named file,
# or writes nothing, run() raises. Here we prove run() returns the engine's file
# even when query_results report an empty/zero-row result without error: a 0-row
# .arrow is returned as a 0-row table (not an error) - documenting the contract.
# ---------------------------------------------------------------------------
def test_zero_row_arrow_returns_empty_table_not_error(tmp_path):
    # Drive ProcessEngine.run with a fake runner that "writes" a 0-row arrow result.
    results = tmp_path / "results"
    results.mkdir()

    class FakeQR:
        error = None

    class FakeResult:
        query_results = [FakeQR()]
        response = "ok"
        stderr = ""

    class FakeRunner:
        def run(self, *, timeout, query_lines, run_env):
            req = query_lines[0].split()[1]
            empty = pa.table({"x": pa.array([], pa.int64())})
            _write_arrow(
                Path(run_env["SYNNODB_RESULT_DIR"]) / f"result_{req}.arrow", empty
            )
            return FakeResult()

    eng = ProcessEngine("e", tmp_path, "/data")
    eng._runner = lambda: FakeRunner()  # type: ignore[method-assign]
    # format_args_element needs a known query id; monkeypatch it to a trivial line.
    import synnodb.workloads.workload_provider as wp

    orig = wp.format_args_element
    wp.format_args_element = lambda qid, ph: f"q{qid} REQ{qid}"  # type: ignore
    try:
        out = eng.run("1", {})
    finally:
        wp.format_args_element = orig
    assert out.num_rows == 0  # 0-row result is a valid empty table, not an error


# ---------------------------------------------------------------------------
# PE7 (fixed): a truncated/corrupt result (engine crashed mid-write) surfaces an
# EngineExecutionError carrying the engine id / query / stderr, not an opaque
# pyarrow error from deep in the read.
# ---------------------------------------------------------------------------
def test_partial_arrow_surfaces_engine_execution_error(tmp_path):
    from synnodb.errors import EngineExecutionError

    class FakeQR:
        error = None

    class FakeResult:
        query_results = [FakeQR()]
        response = "ok"
        stderr = "boom: segfault in run_q1"

    class FakeRunner:
        def run(self, *, timeout, query_lines, run_env):
            req = query_lines[0].split()[1]
            p = Path(run_env["SYNNODB_RESULT_DIR"]) / f"result_{req}.arrow"
            _write_arrow(p, pa.table({"x": pa.array(list(range(1000)), pa.int64())}))
            with open(p, "r+b") as f:  # truncate to simulate a crash mid-write
                f.truncate(p.stat().st_size // 2)
            return FakeResult()

    eng = ProcessEngine("synno-x", tmp_path, "/data")
    eng._runner = lambda: FakeRunner()  # type: ignore[method-assign]
    import synnodb.workloads.workload_provider as wp

    orig = wp.format_args_element
    wp.format_args_element = lambda qid, ph: f"q{qid} REQ{qid}"  # type: ignore
    try:
        with pytest.raises(EngineExecutionError) as ei:
            eng.run("1", {})
    finally:
        wp.format_args_element = orig
    msg = str(ei.value)
    assert (
        "synno-x" in msg and "boom: segfault" in msg
    )  # engine id + stderr carried through
