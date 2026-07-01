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

    def __init__(self, workspace, scale_factor, verdict=PASS):
        self._workspace = workspace
        self._scale_factor = scale_factor
        self._verdict = verdict

    def validate_for_publish(self, query_ids, **_):
        return ValidationReceipt(
            snapshot_id="fake-snapshot",
            build_ids=engine_build_ids(self._workspace),
            validated_queries=tuple(ValidatedQuery(str(q), ()) for q in query_ids),
            coverage_policy="test",
            data_planes=(PLANE_PARQUET,),
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

    got = _derive_expected_tables(sf_dir, ["lineitem"])
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
    assert _derive_expected_tables(tmp_path, ["absent"]) is None


# ── the auto-publish wires both into the manifest ──────────────────────────


@needs_tpch
def test_factory_publish_downgrades_shm_to_parquet_only(tmp_path, monkeypatch):
    """A shm-capable loader is still published parquet-only when the receipt only proves the
    parquet plane - generation does not validate shm, so the shm plane is withheld (not shipped
    unverified). The engine publishes and serves; it just does not advertise the shm hot-load."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import (
        OLAPWorkload,
        OLAPWorkloadProvider,
    )

    data = tmp_path / "data"
    sf_dir = data / "sf0.01"
    sf_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    prov = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=data,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1"],
    )
    for t in prov.dataset_tables:
        con.execute(f"COPY {t} TO '{sf_dir / (t + '.parquet')}' (FORMAT parquet)")
    con.close()

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
        threads=4,
    )

    manifest_path = next(engines.glob("*/manifest.json"))
    man = EngineManifest.read(manifest_path)
    assert man.shm_capable is False  # shm withheld: the receipt only validated parquet
    assert man.threads == 4
    assert {q.query_id for q in man.queries} == {"1"}


def test_factory_publish_refuses_on_failed_validation(tmp_path, monkeypatch):
    """The whole point of the gate: a final validation that does not pass means the engine is not
    published, even though everything else (db binary, parquet, engines dir) is in place."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import (
        OLAPWorkload,
        OLAPWorkloadProvider,
    )

    data = tmp_path / "data"
    sf_dir = data / "sf0.01"
    sf_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    prov = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=data,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1"],
    )
    for t in prov.dataset_tables:
        con.execute(f"COPY {t} TO '{sf_dir / (t + '.parquet')}' (FORMAT parquet)")
    con.close()

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
    )

    assert not engines.exists() or not list(engines.glob("*/manifest.json"))


@needs_tpch
def test_factory_publish_skips_shm_for_a_disk_only_loader(tmp_path, monkeypatch):
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import (
        OLAPWorkload,
        OLAPWorkloadProvider,
    )

    data = tmp_path / "data"
    sf_dir = data / "sf0.01"
    sf_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    prov = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=data,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1"],
    )
    for t in prov.dataset_tables:
        con.execute(f"COPY {t} TO '{sf_dir / (t + '.parquet')}' (FORMAT parquet)")
    con.close()

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
    )

    man = EngineManifest.read(next(engines.glob("*/manifest.json")))
    assert man.shm_capable is False
    assert not man.expected_tables
