"""DuckDB-native subsets (Step 2): the subset is a ``subset.duckdb`` instead of parquet.

Covers the subset.duckdb sink, DuckDB-native registration + the ``DataSource.DUCKDB`` wiring
through the provider, the shm staging that feeds the synthesis engine, and - the load-bearing
cross-check (design §9) - that the DuckDB oracle returns identical rows whether the subset is a
``subset.duckdb`` (native) or parquet (fallback).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from synnodb.utils.utils import DataSource, DBStorage, ServeFrom
from synnodb.workloads.system_factory_olap import System
from synnodb.workloads.workload_provider_olap import (
    OLAPWorkloadProvider,
    allowed_data_sources,
)


_QUERIES = {
    "1": "SELECT * FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey",
    "2": "SELECT * FROM orders o JOIN customer c ON o.o_custkey = c.c_custkey",
    "3": "SELECT * FROM customer c JOIN nation n ON c.c_nationkey = n.n_nationkey",
}
_TABLES = ["customer", "lineitem", "nation", "orders"]


def _make_source(path: Path) -> None:
    """A small star with a DECIMAL column (so the native<->parquet cross-check exercises exact
    decimal fidelity, not just integers)."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE nation AS SELECT i AS n_nationkey FROM range(5) t(i)")
    con.execute(
        "CREATE TABLE customer AS "
        "SELECT i AS c_custkey, (i % 5) AS c_nationkey, (i * 1.5)::DECIMAL(15,2) AS c_bal "
        "FROM range(50) t(i)"
    )
    con.execute(
        "CREATE TABLE orders AS "
        "SELECT i AS o_orderkey, (i % 50) AS o_custkey FROM range(200) t(i)"
    )
    con.execute(
        "CREATE TABLE lineitem AS "
        "SELECT i AS l_id, (i % 200) AS l_orderkey, (i % 7)::DECIMAL(10,2) AS l_amt "
        "FROM range(1000) t(i)"
    )
    con.close()


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / "src.duckdb"
    _make_source(path)
    return path


def _register(name, managed_root, source_db, *, serve_from):
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    is_duckdb = ServeFrom.coerce(serve_from) == ServeFrom.DUCKDB
    con = duckdb.connect(str(source_db), read_only=True)
    try:
        return register_workload_from_duckdb(
            name=name,
            con=con,
            queries_json=_QUERIES,
            managed_root=managed_root,
            downscale_fractions=(0.2,),
            whole_table_threshold=10,
            serve_from=serve_from,
            source_db_path=str(source_db) if is_duckdb else None,
        )
    finally:
        con.close()


# --------------------------------------------------------------------- allowed data sources
def test_duckdb_source_allowed_in_memory_only():
    assert DataSource.DUCKDB in allowed_data_sources(System.DUCKDB, DBStorage.IN_MEMORY)
    assert DataSource.DUCKDB in allowed_data_sources(
        System.BESPOKE, DBStorage.IN_MEMORY
    )
    assert DataSource.DUCKDB not in allowed_data_sources(System.DUCKDB, DBStorage.SSD)
    assert DataSource.DUCKDB not in allowed_data_sources(System.BESPOKE, DBStorage.SSD)


# --------------------------------------------------------------------- native registration
def test_native_registration_materializes_subset_duckdb(tmp_path, source_db):
    managed = tmp_path / "managed"
    spec = _register("nat_reg", managed, source_db, serve_from=ServeFrom.DUCKDB)

    assert spec.serve_from == ServeFrom.DUCKDB
    assert spec.fast_check_sfs == (0.2,)
    assert spec.benchmark_sf == 1.0
    # downscaled subset is a real subset.duckdb; the benchmark subset is a zero-copy symlink to source
    downscaled = managed / "fraction0.2" / "subset.duckdb"
    full = managed / "fraction1" / "subset.duckdb"
    assert downscaled.exists() and not downscaled.is_symlink()
    assert full.is_symlink() and full.resolve() == source_db.resolve()
    # no parquet was written
    assert not list(managed.rglob("*.parquet"))
    # schema derived from the subset.duckdb
    assert "CREATE TABLE lineitem" in spec.schema()


def test_native_subset_duckdb_joins_non_vacuous(tmp_path, source_db):
    managed = tmp_path / "managed"
    _register("nat_join", managed, source_db, serve_from=ServeFrom.DUCKDB)
    subset_db = managed / "fraction0.2" / "subset.duckdb"
    con = duckdb.connect(str(subset_db), read_only=True)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM lineitem l "
            "JOIN orders o ON l.l_orderkey = o.o_orderkey "
            "JOIN customer c ON o.o_custkey = c.c_custkey"
        ).fetchone()[0]
        li = con.execute("SELECT COUNT(*) FROM lineitem").fetchone()[0]
    finally:
        con.close()
    assert 0 < li < 1000  # downscaled
    assert n > 0  # non-vacuous


# --------------------------------------------------------------------- provider wiring
def test_provider_native_emits_duckdb_data_source(tmp_path, source_db):
    managed = tmp_path / "managed"
    _register("nat_prov", managed, source_db, serve_from=ServeFrom.DUCKDB)
    prov = OLAPWorkloadProvider(
        benchmark="nat_prov",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    on_disk = dict(prov._datasets_on_disk())
    assert "fraction0.2" in on_disk and "fraction1" in on_disk

    from synnodb.tools.run_tool_mode import RunToolMode

    batches = prov.produce_workload(
        RunToolMode.FAST_CHECK, query_ids=["1"], num_threads=1, core_ids=None
    )
    assert batches
    for batch in batches:
        assert batch.exec_settings.data_source == DataSource.DUCKDB
        subset_dir = Path(batch.exec_settings.parquet_dir)
        assert (subset_dir / "subset.duckdb").exists()


def test_provider_native_rejects_ssd(tmp_path, source_db):
    managed = tmp_path / "managed"
    _register("nat_ssd", managed, source_db, serve_from=ServeFrom.DUCKDB)
    from synnodb.tools.run_tool_mode import RunToolMode

    prov = OLAPWorkloadProvider(
        benchmark="nat_ssd",
        base_parquet_dir=managed,
        db_storage=DBStorage.SSD,
        bespoke_ssd_storage_dir=tmp_path / "ssd",
        query_ids=["1"],
    )
    with pytest.raises(ValueError, match="in-memory only"):
        prov.produce_workload(
            RunToolMode.FAST_CHECK, query_ids=["1"], num_threads=1, core_ids=None
        )


# --------------------------------------------------------------------- oracle cross-check
def _oracle_result(base, sf, serve_from, sql):
    from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
        DuckDBConnectionManager,
    )

    mgr = DuckDBConnectionManager(
        pre_load_duckdb_tables=False,
        dataset_tables=_TABLES,
        parquet_path=base,
        benchmark=None,
        db_storage=DBStorage.IN_MEMORY,
        sf=sf,
        pin_worker=False,
        pin_core=None,
        num_threads=1,
        run_duckdb_on_parquet=False,
        serve_from=serve_from,
        drop_os_caches_before_sql=False,
    )
    try:
        _, table, _ = mgr.duckdb_sql_arrow(sql)
        return table.to_pydict()
    finally:
        mgr.clear_mem_footprint()


def test_oracle_native_matches_parquet(tmp_path, source_db):
    """Design §9: the DuckDB oracle must return identical rows whether the subset is a
    ``subset.duckdb`` (native) or parquet (fallback) - the fallback keeps the native path honest."""
    managed_native = tmp_path / "native"
    managed_parquet = tmp_path / "parquet"
    _register("ora_native", managed_native, source_db, serve_from=ServeFrom.DUCKDB)
    _register("ora_parquet", managed_parquet, source_db, serve_from=ServeFrom.PARQUET)

    sql = (
        "SELECT count(*) AS n, sum(l.l_amt) AS amt FROM lineitem l "
        "JOIN orders o ON l.l_orderkey = o.o_orderkey "
        "JOIN customer c ON o.o_custkey = c.c_custkey"
    )
    for sf in (0.2, 1.0):
        native = _oracle_result(managed_native, sf, ServeFrom.DUCKDB, sql)
        parquet = _oracle_result(managed_parquet, sf, ServeFrom.PARQUET, sql)
        assert native == parquet, f"native != parquet oracle at sf={sf}"


# --------------------------------------------------------------------- shm staging
def test_shm_staging_produces_loader_segments():
    import pyarrow as pa
    import pyarrow.ipc as ipc

    from synnodb.cpp_runner.shm_stage import stage_subset_duckdb_to_shm

    tmp = Path(tempfile.mkdtemp())
    subset_db = tmp / "subset.duckdb"
    con = duckdb.connect(str(subset_db))
    con.execute(
        "CREATE TABLE lineitem AS "
        "SELECT i AS l_id, (i % 7)::DECIMAL(10,2) AS amt FROM range(100) t(i)"
    )
    con.execute("CREATE TABLE orders AS SELECT i AS o_id FROM range(20) t(i)")
    con.close()

    ingest = stage_subset_duckdb_to_shm(subset_db)
    try:
        # one Arrow IPC file per table, exactly what ReadArrowTableFromShm maps
        for table, rows in (("lineitem", 100), ("orders", 20)):
            seg = ingest / f"{table}.arrow"
            assert seg.exists()
            with pa.memory_map(str(seg), "r") as src:
                tbl = ipc.open_file(src).read_all()
            assert tbl.num_rows == rows
        # decimal fidelity preserved through the Arrow segment
        with pa.memory_map(str(ingest / "lineitem.arrow"), "r") as src:
            li = ipc.open_file(src).read_all()
        assert str(li.schema.field("amt").type) == "decimal128(10, 2)"
        # idempotent: same dir, marker present, no rebuild needed
        assert stage_subset_duckdb_to_shm(subset_db) == ingest
        assert (ingest / ".complete").exists()
    finally:
        import shutil

        shutil.rmtree(ingest, ignore_errors=True)
