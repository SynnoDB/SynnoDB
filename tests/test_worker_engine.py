"""Out-of-process WorkerEngine over the shm data plane: ingest, run, crash isolation,
and end-to-end routing through the QueryRouter.

This is the production engine shape (warm subprocess + shared memory). The C++ worker
is a drop-in for the reference Python worker behind the same ``BespokeEngine``
interface, so a green run here means the routing architecture is sound for the real
engine too.
"""
from __future__ import annotations

import pyarrow as pa
import pytest

import synnodb
from synnodb.router import (
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    WorkerEngine,
    WorkerEngineError,
    register_engine,
)

WORKER_SQL = "SELECT count(*) AS c FROM t WHERE a >= $p0"
USER_TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= 2"
DATA = pa.table({"a": pa.array([1, 2, 3, 4, 5], pa.int64())})


@pytest.fixture
def worker(tmp_path):
    eng = WorkerEngine("we", {"1": WORKER_SQL}, shm_dir=tmp_path / "shm")
    eng.ingest({"t": DATA})
    yield eng
    eng.close()


# --------------------------------------------------------------------------- #
# Direct engine behavior
# --------------------------------------------------------------------------- #
def test_ingest_and_run(worker):
    assert worker.health()
    assert worker.run("1", {"p0": 3}).to_pylist() == [{"c": 3}]
    assert worker.run("1", {"p0": 1}).to_pylist() == [{"c": 5}]


def test_unknown_query_raises(worker):
    with pytest.raises(KeyError):
        worker.run("99", {"p0": 1})


def test_crash_isolation(worker):
    worker._proc.kill()
    worker._proc.wait()
    assert worker.health() is False
    with pytest.raises(WorkerEngineError):
        worker.run("1", {"p0": 2})  # raises cleanly; the parent process survives


def test_close_is_idempotent_and_cleans_up(tmp_path):
    eng = WorkerEngine("we", {"1": WORKER_SQL}, shm_dir=tmp_path / "shm")
    eng.ingest({"t": DATA})
    eng.close()
    eng.close()  # no error
    assert eng.health() is False


# --------------------------------------------------------------------------- #
# End-to-end through the router
# --------------------------------------------------------------------------- #
def _connection(worker, tmp_path, *, cross_check_rate=1.0):
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=cross_check_rate),
        registry=TemplateRegistry(),
    )
    con.execute("CREATE TABLE t(a BIGINT)")
    con.execute("INSERT INTO t SELECT * FROM range(1, 6)")
    register_engine(con, template_sql=USER_TEMPLATE, engine=worker, placeholders=[PlaceholderSpec("p0", "INTEGER")])
    return con


def test_worker_engine_routes_and_matches_duckdb(worker, tmp_path):
    con = _connection(worker, tmp_path)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    dec = con.router.route(sql, None, con)
    assert dec.routed is True and dec.trace.results_match is True
    assert con.execute(sql).fetchall() == con.duckdb.execute(sql).fetchall()


def test_router_falls_back_when_worker_dies(worker, tmp_path):
    con = _connection(worker, tmp_path, cross_check_rate=0.0)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    assert con.router.route(sql, None, con).routed is True   # works while alive

    worker._proc.kill()
    worker._proc.wait()
    # Dead worker: engine_ready guard fails -> clean fallback, correct answer from DuckDB.
    dec = con.router.route(sql, None, con)
    assert dec.routed is False
    assert "unhealthy" in dec.trace.reason
    assert con.execute(sql).fetchall() == con.duckdb.execute(sql).fetchall()
