"""Data loading / preload: ``ProcessEngine.load_data`` loads the engine's data up front (via an
empty query batch) and ``SynnoConnection.synno_ingest_data`` drives it across every discovered
engine, so the first real query is served warm instead of paying the one-time loader/builder cost.

Uses fake runners / in-process engines so no compiled binary is needed.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

import synnodb
from synnodb.duckdb_compat import discovery
from synnodb.errors import EngineExecutionError
from synnodb.router import (
    LocalCallableEngine,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
)
from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.router.process_engine import ProcessEngine, ShmHotLoadEngine
from synnodb.router.registry import PlaceholderSpec


class FakeRunner:
    """Stand-in for HotpatchProc: records the batches it was asked to run and reports liveness."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.calls: list = []

    def run(self, *, timeout, query_lines, run_env):
        self.calls.append(list(query_lines))

        class _Result:
            response = "exit_code: 0 signal: 0"
            stderr = ""
            query_results: list = []

        return _Result()

    def is_running(self) -> bool:
        return self.alive


# ---- engine-level: ProcessEngine.load_data --------------------------------
def test_load_data_sends_empty_batch_and_is_idempotent(tmp_path):
    eng = ProcessEngine("e", tmp_path, "/data")
    runner = FakeRunner()
    eng._runner = lambda: runner  # type: ignore[method-assign]

    eng.load_data()
    assert runner.calls == [[]]  # exactly one batch, and it carries no queries
    assert eng._loaded_data is True

    eng.load_data()  # idempotent: the data is already resident, so no second round-trip
    assert runner.calls == [[]]


def test_load_data_raises_when_child_dies(tmp_path):
    # A loader/builder crash leaves the child dead (is_running() False) and writes no result file;
    # load_data detects that via liveness and surfaces the engine's diagnostics.
    eng = ProcessEngine("e", tmp_path, "/data")
    eng._runner = lambda: FakeRunner(alive=False)  # type: ignore[method-assign]
    with pytest.raises(EngineExecutionError):
        eng.load_data()
    assert eng._loaded_data is False  # a failed load may be retried


def test_close_resets_loaded_data(tmp_path):
    eng = ProcessEngine("e", tmp_path, "/data")
    eng._runner = lambda: FakeRunner()  # type: ignore[method-assign]
    eng.load_data()
    assert eng._loaded_data is True
    eng.close()
    assert eng._loaded_data is False


def test_shm_load_data_before_ingest_raises(tmp_path):
    # The hot-load plane has nothing to load until its data has been staged into /dev/shm.
    eng = ShmHotLoadEngine("e", tmp_path)
    with pytest.raises(RuntimeError, match="before ingest"):
        eng.load_data()


def test_local_callable_load_data_is_noop():
    LocalCallableEngine(
        "e", {}
    ).load_data()  # in-process: no cold start, must not raise


# ---- connection-level: synno_ingest_data ----------------------------------
class CountingEngine(LocalCallableEngine):
    """An in-process engine that records how many times its data was loaded."""

    def __init__(self, *args, **kwargs) -> None:
        self.loads = 0
        self.raise_on_load = kwargs.pop("raise_on_load", False)
        super().__init__(*args, **kwargs)

    def load_data(self) -> None:
        self.loads += 1
        if self.raise_on_load:
            raise RuntimeError("boom")


T1 = "SELECT count(*) AS c FROM t WHERE a >= ?"
T2 = "SELECT count(*) AS c FROM t WHERE a > ?"  # structurally distinct -> a second binding


def _fn(ph):
    return pa.table({"c": pa.array([0], pa.int64())})


def _write_two_query_manifest(engines_dir, engine_id="eng-test"):
    d = engines_dir / engine_id
    d.mkdir(parents=True)
    EngineManifest(
        engine_id=engine_id,
        queries=(
            QueryTemplate("1", T1, (PlaceholderSpec("p0", "INTEGER"),)),
            QueryTemplate("2", T2, (PlaceholderSpec("p0", "INTEGER"),)),
        ),
        parquet_dir="/unused",
        expected_tables={},
    ).write(d)
    return d


def _con(engines_dir):
    con = synnodb.connect(
        engines=str(engines_dir),
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER)")
    con.duckdb.execute("INSERT INTO t SELECT * FROM range(1, 6)")
    return con


def _patch_shared_engine(monkeypatch, created, *, raise_on_load=False):
    def fake_build(manifest, engine_dir, **_):
        eng = CountingEngine(
            manifest.engine_id,
            {"1": _fn, "2": _fn},
            raise_on_load=raise_on_load,
        )
        created.append(eng)
        return eng

    monkeypatch.setattr(discovery, "_build_engine", fake_build)


def test_synno_ingest_data_loads_each_engine_once(tmp_path, monkeypatch):
    # Two templates are backed by ONE engine process; the load must run once, not once per template.
    created: list = []
    _patch_shared_engine(monkeypatch, created)
    engines = tmp_path / "engines"
    _write_two_query_manifest(engines)
    con = _con(engines)
    con.refresh_engines()
    assert con.router_stats()["registry"]["templates"] == 2
    assert len(created) == 1  # one shared engine backs both templates

    loaded = con.synno_ingest_data()
    assert loaded == 1  # distinct engines loaded
    assert created[0].loads == 1  # loaded exactly once despite two bindings


def test_synno_ingest_data_skips_failing_engine(tmp_path, monkeypatch):
    # One bad engine must not break the call: it is logged and skipped, never raised.
    created: list = []
    _patch_shared_engine(monkeypatch, created, raise_on_load=True)
    engines = tmp_path / "engines"
    _write_two_query_manifest(engines)
    con = _con(engines)
    con.refresh_engines()

    loaded = con.synno_ingest_data()  # must not raise
    assert loaded == 0  # the failing engine is not counted
    assert created[0].loads == 1  # it was attempted


def test_synno_ingest_data_no_engines_returns_zero(tmp_path):
    engines = tmp_path / "engines"
    engines.mkdir()
    con = _con(engines)
    con.refresh_engines()
    assert con.synno_ingest_data() == 0
