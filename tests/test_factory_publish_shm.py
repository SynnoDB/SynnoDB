"""The factory auto-publish (``main._publish_generated_engine``) gates on a live re-validation and
ships the planes that validation actually proved. Generation runs over parquet, so a generated
engine publishes the parquet plane and the shm hot-load plane is withheld (downgraded) until it is
validated in its own right - an unvalidated serving plane must never ride on a parquet-only receipt.
"""

from __future__ import annotations

import types

import duckdb
import pytest

from synnodb.main import (
    _derive_expected_tables,
    _loader_is_shm_capable,
    _publish_generated_engine,
)
from synnodb.router.manifest import EngineManifest
from synnodb.utils.utils import ServeFrom
from synnodb.workloads.validation_receipt import (
    PASS,
    PLANE_PARQUET,
    ValidatedQuery,
    ValidationReceipt,
    engine_build_ids,
)

from receipt_helpers import write_fake_engine_db


class _FakeRunTool:
    """A run_tool double for the publish gate: returns a parquet-only pass receipt for *workspace*
    (exactly the shape ``RunTool.validate_for_publish`` produces in generation, which validates the
    parquet plane only), so these tests exercise the stamping/downgrade logic without compiling a
    real engine."""

    def __init__(self, workspace, scale_factor, verdict=PASS, planes=(PLANE_PARQUET,)):
        self._workspace = workspace
        self._scale_factor = scale_factor
        self._verdict = verdict
        self._planes = planes
        self.gate_calls = 0

    def validate_for_publish(self, query_ids, **_):
        self.gate_calls += 1
        return ValidationReceipt(
            snapshot_id="fake-snapshot",
            build_ids=engine_build_ids(self._workspace),
            validated_queries=tuple(ValidatedQuery(str(q), ()) for q in query_ids),
            coverage_policy="test",
            data_planes=tuple(self._planes),
            dataset="tpch",
            validated_scale_factors=(float(self._scale_factor),),
            mode="exhaustive",
            live_run=True,
            verdict=self._verdict,
        )


def _tpch_available() -> bool:
    try:
        con = duckdb.connect()
        con.execute("INSTALL tpch; LOAD tpch;")
        con.close()
        return True
    except Exception:
        return False


needs_tpch = pytest.mark.skipif(
    not _tpch_available(), reason="DuckDB tpch extension unavailable"
)


def _tpch_parquet_provider(tmp_path):
    """A tpch workload provider over a freshly generated sf0.01 parquet subset
    under ``tmp_path/data`` - the standard fixture for publish-path tests.
    Returns ``(provider, data_dir)``."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

    data = tmp_path / "data"
    sf_dir = data / "sf0.01"
    sf_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    prov = OLAPWorkloadProvider(
        benchmark="tpch",
        base_parquet_dir=data,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1"],
    )
    for t in prov.dataset_tables:
        con.execute(f"COPY {t} TO '{sf_dir / (t + '.parquet')}' (FORMAT parquet)")
    con.close()
    return prov, data


# ── the shm-capability probe (reads the compiled loader) ───────────────────


def test_loader_is_shm_capable_reads_the_emitted_loader(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _loader_is_shm_capable(ws) is False  # no parquet_reader.cpp at all
    (ws / "parquet_reader.cpp").write_text(
        'tables->x = ReadParquetTable(path + "x.parquet");'
    )
    assert _loader_is_shm_capable(ws) is False  # disk-only loader (SSD plane)
    (ws / "parquet_reader.cpp").write_text(
        "if (synnodb::shm_ingest_enabled()) { /* shm branch */ } else { /* parquet */ }"
    )
    assert _loader_is_shm_capable(ws) is True  # in-memory loader with the shm branch


# ── expected_tables derived the way the serving compat gate reads them ─────


@needs_tpch
def test_derive_expected_tables_matches_information_schema(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    sf_dir = tmp_path / "sf0.01"
    sf_dir.mkdir()
    con.execute(f"COPY lineitem TO '{sf_dir / 'lineitem.parquet'}' (FORMAT parquet)")

    got = _derive_expected_tables(sf_dir, ["lineitem"], ServeFrom.PARQUET)
    assert set(got) == {"lineitem"}
    names = [c.name for c in got["lineitem"]]
    assert names[0] == "l_orderkey" and "l_shipdate" in names and len(names) == 16

    # The derived types are exactly what check_compatibility reads from a live table built from
    # the same parquet, so an engine never rejects its own build.
    live = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE lower(table_name) = 'lineitem' ORDER BY ordinal_position"
    ).fetchall()
    assert [(c.name, c.type) for c in got["lineitem"]] == [(n, str(t)) for n, t in live]
    con.close()


def test_derive_expected_tables_is_none_when_a_parquet_is_missing(tmp_path):
    # A half-declared schema would let the shm gate serve wrong data, so signal "no shm".
    assert _derive_expected_tables(tmp_path, ["absent"], ServeFrom.PARQUET) is None


def test_derive_expected_tables_native_reads_subset_duckdb(tmp_path):
    """For a DuckDB-native workload the schema is read from ``subset.duckdb`` (no parquet), and it
    matches what the serving compat gate reads from the same tables in a live connection."""
    sf_dir = tmp_path / "fraction1"
    sf_dir.mkdir()
    subset_db = sf_dir / "subset.duckdb"
    con = duckdb.connect(str(subset_db))
    con.execute(
        "CREATE TABLE lineitem AS SELECT 1 AS l_id, (1.5)::DECIMAL(15,2) AS l_amt"
    )
    con.close()

    got = _derive_expected_tables(sf_dir, ["lineitem"], ServeFrom.DUCKDB)
    assert set(got) == {"lineitem"}
    assert [(c.name, c.type) for c in got["lineitem"]] == [
        ("l_id", "INTEGER"),
        ("l_amt", "DECIMAL(15,2)"),
    ]
    # missing subset.duckdb -> None (withhold the shm plane rather than half-declare a schema)
    assert (
        _derive_expected_tables(tmp_path / "nope", ["lineitem"], ServeFrom.DUCKDB)
        is None
    )


# ── the auto-publish wires both into the manifest ──────────────────────────


@needs_tpch
def test_factory_publish_downgrades_shm_to_parquet_only(tmp_path, monkeypatch):
    """A shm-capable loader is still published parquet-only when the receipt only proves the
    parquet plane - generation does not validate shm, so the shm plane is withheld (not shipped
    unverified). The engine publishes and serves; it just does not advertise the shm hot-load."""
    prov, data = _tpch_parquet_provider(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(ws / "db")  # engine-producing-run guard, with a real build-id
    (ws / "parquet_reader.cpp").write_text(
        "if (synnodb::shm_ingest_enabled()) {} else {}"
    )

    engines = tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(engines))

    _publish_generated_engine(
        ws,
        prov,
        ["1"],
        data,
        types.SimpleNamespace(benchmark_sf=0.01),
        "run-x",
        run_tool=_FakeRunTool(ws, 0.01),
        publishes_engine=True,
        threads=4,
    )

    manifest_path = next(engines.glob("*/manifest.json"))
    man = EngineManifest.read(manifest_path)
    assert man.shm_capable is False  # shm withheld: the receipt only validated parquet
    assert man.threads == 4
    assert {q.query_id for q in man.queries} == {"1"}


@needs_tpch
def test_factory_publish_refuses_on_failed_validation(tmp_path, monkeypatch):
    """The whole point of the gate: a final validation that does not pass means the engine is not
    published, even though everything else (db binary, parquet, engines dir) is in place."""
    prov, data = _tpch_parquet_provider(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(ws / "db")
    (ws / "parquet_reader.cpp").write_text(
        "if (synnodb::shm_ingest_enabled()) {} else {}"
    )

    engines = tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(engines))

    _publish_generated_engine(
        ws,
        prov,
        ["1"],
        data,
        types.SimpleNamespace(benchmark_sf=0.01),
        "run-x",
        run_tool=_FakeRunTool(ws, 0.01, verdict="fail"),
        publishes_engine=True,
    )

    assert not engines.exists() or not list(engines.glob("*/manifest.json"))


def test_factory_publish_runs_gate_and_refuses_without_binary(tmp_path, monkeypatch):
    """The real gate's forced live compile always leaves a ``db`` binary, even on a fully
    cache-replayed run. A gate that minted a pass receipt WITHOUT producing one (this fake)
    is therefore an inconsistent state: the gate must still run (see
    ``_publish_generated_engine``), and the receipt identity check then refuses the publish
    (no build-id on disk) instead of shipping an unidentifiable engine."""
    data = tmp_path / "data"
    (data / "sf0.01").mkdir(parents=True)  # subset dir only; nothing reads parquet
    ws = tmp_path / "ws"
    ws.mkdir()  # no db binary despite a pass receipt

    engines = tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(engines))

    run_tool = _FakeRunTool(ws, 0.01)
    _publish_generated_engine(
        ws,
        None,
        ["1"],
        data,
        types.SimpleNamespace(benchmark_sf=0.01),
        "run-x",
        run_tool=run_tool,
        publishes_engine=True,
    )

    assert run_tool.gate_calls == 1  # the gate ran (and would restore snapshots)
    assert not engines.exists() or not list(engines.glob("*/manifest.json"))


def test_factory_publish_skips_gate_for_non_engine_plans(tmp_path):
    """A plan that declares no engine (e.g. createStoragePlan) never runs the gate."""
    run_tool = _FakeRunTool(tmp_path, 0.01)
    _publish_generated_engine(
        tmp_path,
        None,
        ["1"],
        tmp_path,
        types.SimpleNamespace(benchmark_sf=0.01),
        "run-x",
        run_tool=run_tool,
        publishes_engine=False,
    )
    assert run_tool.gate_calls == 0


@needs_tpch
def test_factory_publish_skips_shm_for_a_disk_only_loader(tmp_path, monkeypatch):
    prov, data = _tpch_parquet_provider(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(ws / "db")
    # A persistent/SSD loader has no shm branch -> must not be advertised as shm-capable.
    (ws / "parquet_reader.cpp").write_text('tables->x_path = path + "x.parquet";')

    engines = tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(engines))

    _publish_generated_engine(
        ws,
        prov,
        ["1"],
        data,
        types.SimpleNamespace(benchmark_sf=0.01),
        "run-x",
        run_tool=_FakeRunTool(ws, 0.01),
        publishes_engine=True,
    )

    man = EngineManifest.read(next(engines.glob("*/manifest.json")))
    assert man.shm_capable is False
    assert not man.expected_tables


@needs_tpch
def test_factory_publish_native_ships_shm_only(tmp_path, monkeypatch):
    """A DuckDB-native workload validates over shm, so it publishes as a pure shm engine: shm
    plane on, no parquet plane, expected_tables declared, and source_db pointing at subset.duckdb -
    never a parquet manifest pointing at a directory that holds none (the F5 regression)."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.byo_workload import register_workload_from_duckdb
    from synnodb.workloads.validation_receipt import PLANE_SHM
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider
    from synnodb.workloads.workload_spec import get_workload_spec

    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    con.close()

    managed = tmp_path / "managed"
    con = duckdb.connect(str(src), read_only=True)
    try:
        register_workload_from_duckdb(
            name="native_pub",
            con=con,
            queries_json={"1": "SELECT count(*) FROM lineitem"},
            managed_root=managed,
            downscale_fractions=(0.1,),
            serve_from="duckdb",
            source_db_path=str(src),
            source_is_static=True,
        )
    finally:
        con.close()

    prov = OLAPWorkloadProvider(
        benchmark="native_pub",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(ws / "db")
    (ws / "parquet_reader.cpp").write_text(
        "if (synnodb::shm_ingest_enabled()) { /* shm */ } else { /* parquet */ }"
    )
    engines = tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(engines))

    _publish_generated_engine(
        ws,
        prov,
        ["1"],
        managed,
        get_workload_spec("native_pub"),
        "run-native",
        run_tool=_FakeRunTool(ws, 1.0, planes=(PLANE_SHM,)),
        publishes_engine=True,
    )

    man = EngineManifest.read(next(engines.glob("*/manifest.json")))
    assert man.shm_capable is True
    assert set(man.expected_tables) == set(prov.dataset_tables)
    assert man.parquet_dir is None  # a native engine ships no parquet plane
    assert man.source_db == str(managed / "fraction1" / "subset.duckdb")
