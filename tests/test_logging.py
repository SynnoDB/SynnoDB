"""Debug/observability plumbing: the router, worker, and shm paths must emit enough
to chase errors. These tests assert the key lifecycle events are logged.
"""
from __future__ import annotations

import logging

import pyarrow as pa
import pytest

import synnodb
from synnodb.router import RouterMode, RouterPolicy, TemplateRegistry, WorkerEngine


def test_enable_debug_logging_is_idempotent():
    root = logging.getLogger("synnodb")
    original_level, original_handlers = root.level, list(root.handlers)
    try:
        synnodb.enable_debug_logging()
        n = len(root.handlers)
        synnodb.enable_debug_logging()
        assert len(root.handlers) == n  # no duplicate handler
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(original_level)
        root.handlers[:] = original_handlers


def test_router_logs_every_decision(caplog):
    con = synnodb.connect(policy=RouterPolicy(mode=RouterMode.SAMPLED), registry=TemplateRegistry())
    con.duckdb.execute("CREATE TABLE t(a INTEGER)")  # setup via the escape hatch
    with caplog.at_level(logging.DEBUG, logger="synnodb.router"):
        con.execute("SELECT * FROM t")  # no template -> fallback (logged)
    messages = [r.getMessage() for r in caplog.records]
    assert any("fallback" in m for m in messages), messages


def test_worker_logs_spawn_ingest_run(caplog, tmp_path):
    with caplog.at_level(logging.DEBUG, logger="synnodb.router.worker"):
        eng = WorkerEngine("dbg", {"1": "SELECT count(*) AS c FROM t"}, shm_dir=tmp_path / "shm")
        try:
            eng.ingest({"t": pa.table({"a": [1, 2, 3]})})
            eng.run("1", {})
        finally:
            eng.close()
    messages = [r.getMessage() for r in caplog.records]
    assert any("spawning worker" in m for m in messages), messages
    assert any("ingest" in m for m in messages), messages
    assert any("run query_id=1" in m for m in messages), messages


def test_shm_write_is_logged(caplog, tmp_path):
    from synnodb.router.shm_transport import ShmWriter

    with caplog.at_level(logging.DEBUG, logger="synnodb.router.shm"):
        w = ShmWriter(base_dir=tmp_path)
        w.write_table(pa.table({"a": [1, 2, 3, 4]}))
        w.close()
    assert any("wrote shm segment" in r.getMessage() for r in caplog.records)
