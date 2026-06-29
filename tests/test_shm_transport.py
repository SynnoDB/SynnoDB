"""Shared-memory Arrow transport: round-trip, zero-copy, lifecycle, orphan sweep.

Tests use a ``base_dir`` under ``tmp_path`` (not ``/dev/shm``) for portability; the
zero-copy property holds for any ``mmap``-able file, so the assertions are valid.
"""
from __future__ import annotations

import os

import pyarrow as pa

from synnodb.router.shm_transport import SegmentRef, ShmWriter, read_table, sweep_orphans


def test_roundtrip_equal(tmp_path):
    table = pa.table({"a": list(range(1000)), "b": [f"s{i}" for i in range(1000)]})
    with ShmWriter(base_dir=tmp_path) as w:
        ref = w.write_table(table)
        assert ref.nbytes > 0
        out = read_table(ref, base_dir=tmp_path)
    assert out.equals(table)


def test_read_is_zero_copy(tmp_path):
    # ~8 MB of int64; a zero-copy mmap read must not allocate it on Arrow's pool.
    n = 1_000_000
    table = pa.table({"x": pa.array(range(n), pa.int64())})
    data_bytes = n * 8
    with ShmWriter(base_dir=tmp_path) as w:
        ref = w.write_table(table)
        before = pa.total_allocated_bytes()
        out = read_table(ref, base_dir=tmp_path)
        delta = pa.total_allocated_bytes() - before
        assert out.num_rows == n
        # Zero-copy: pool growth is a tiny fraction of the payload (it would be ~8 MB
        # if the buffers were copied into the pool).
        assert delta < data_bytes // 10, f"read allocated {delta} bytes (not zero-copy)"


def test_close_unlinks_all_segments(tmp_path):
    w = ShmWriter(base_dir=tmp_path)
    ref1 = w.write_table(pa.table({"a": [1]}))
    ref2 = w.write_table(pa.table({"a": [2]}))
    assert (tmp_path / ref1.name).exists() and (tmp_path / ref2.name).exists()
    w.close()
    assert not (tmp_path / ref1.name).exists()
    assert not (tmp_path / ref2.name).exists()


def test_unlink_specific_segment(tmp_path):
    w = ShmWriter(base_dir=tmp_path)
    ref = w.write_table(pa.table({"a": [1, 2, 3]}))
    assert (tmp_path / ref.name).exists()
    w.unlink(ref)
    assert not (tmp_path / ref.name).exists()
    w.close()


def test_sweep_removes_dead_owner_keeps_live(tmp_path):
    dead_pid = 999_999  # not a live process
    dead = tmp_path / f"synnodb-{dead_pid}-000001.arrow"
    dead.write_bytes(b"stale")
    live = tmp_path / f"synnodb-{os.getpid()}-000001.arrow"
    live.write_bytes(b"mine")
    unrelated = tmp_path / "not-ours.arrow"
    unrelated.write_bytes(b"keep")

    removed = sweep_orphans(base_dir=tmp_path)

    assert removed == 1
    assert not dead.exists()       # dead owner's segment reaped
    assert live.exists()           # live owner's segment kept
    assert unrelated.exists()      # files outside our convention untouched


def test_segmentref_is_picklable():
    import pickle

    ref = SegmentRef(name="synnodb-1-000001.arrow", nbytes=42)
    assert pickle.loads(pickle.dumps(ref)) == ref
