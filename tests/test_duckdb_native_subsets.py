"""DuckDB-native subsets: the subset is a ``subset.duckdb`` instead of parquet.

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


def _grow_lineitem(con, start: int = 1000, stop: int = 3000) -> None:
    """Append rows to ``lineitem`` (matching ``_make_source``'s columns) so a source's fingerprint
    moves - used to exercise the rebuild path across the reuse tests."""
    con.execute(
        "INSERT INTO lineitem SELECT i AS l_id, (i % 200) AS l_orderkey, "
        f"(i % 7)::DECIMAL(10,2) AS l_amt FROM range({start}, {stop}) t(i)"
    )


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / "src.duckdb"
    _make_source(path)
    return path


def _register(name, managed_root, source_db, *, serve_from):
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    # Opened read-only and closed below: a SynnoDB-owned static source, so it is read in place
    # (no snapshot) and the DuckDB benchmark subset is a zero-copy symlink to it.
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
            source_db_path=str(source_db),
            source_is_static=True,
        )
    finally:
        con.close()


def _prepare(managed_root, name):
    """Trigger the lazy downscaling the same way a synthesis run's start does: build the provider
    and call ``prepare``, materializing the fractional subsets from the frozen source."""
    prov = OLAPWorkloadProvider(
        benchmark=name,
        base_parquet_dir=managed_root,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    prov.prepare()
    return prov


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
    # Sync materializes only the benchmark subset (a zero-copy symlink to the source); the
    # downscaled fractional subset is lazy and not on disk yet.
    downscaled = managed / "fraction0.2" / "subset.duckdb"
    full = managed / "fraction1" / "subset.duckdb"
    assert full.is_symlink() and full.resolve() == source_db.resolve()
    assert not downscaled.exists()
    # schema is derived from the benchmark subset, which exists right after sync
    assert "CREATE TABLE lineitem" in spec.schema()

    # prepare() downscales the fractional subset on demand: a real subset.duckdb, no parquet.
    _prepare(managed, "nat_reg")
    assert downscaled.exists() and not downscaled.is_symlink()
    assert not list(managed.rglob("*.parquet"))


def test_benchmark_subset_is_frozen_snapshot(tmp_path, source_db):
    """A caller-supplied live connection (which they may keep writing to) is frozen into a
    point-in-time snapshot SynnoDB owns. The ``fraction1`` benchmark subset resolves to that
    snapshot, not the caller's live file, so later read-only opens never collide with the
    still-open read-write connection and never see the caller's subsequent writes."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    managed = tmp_path / "managed"
    # A read-write connection to the source stays open across registration and the writes below,
    # exactly as a notebook keeps ``conn = duckdb.connect(path)`` open and hands it to SynnoDB.
    live_rw = duckdb.connect(str(source_db))
    try:
        register_workload_from_duckdb(
            name="nat_snap",
            con=live_rw,
            queries_json=_QUERIES,
            managed_root=managed,
            downscale_fractions=(0.2,),
            whole_table_threshold=10,
            serve_from=ServeFrom.DUCKDB,
            source_db_path=str(source_db),
            source_is_static=False,
        )
        full = managed / "fraction1" / "subset.duckdb"
        # The benchmark subset symlinks the frozen snapshot we own, never the caller's live file.
        assert full.is_symlink()
        assert full.resolve() != source_db.resolve()
        assert full.resolve() == (managed / ".source_snapshot.duckdb").resolve()

        # The caller keeps writing to their live database in parallel...
        _grow_lineitem(live_rw, 1000, 2000)
        # ...but the frozen benchmark subset still reflects the snapshot instant (1000 rows), and
        # opening it read-only does not collide with the live read-write connection.
        con = duckdb.connect(str(full), read_only=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM lineitem").fetchone()[0] == 1000
        finally:
            con.close()
    finally:
        live_rw.close()


def test_stale_subsets_rebuilt_when_source_changes(tmp_path):
    """Re-registering after the source data changes must rebuild the subsets, not reuse the old
    ones: the fingerprint tracks the change and the fraction1 copy reflects the new rows."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"

    def reg():
        con = duckdb.connect(str(src))
        try:
            return register_workload_from_duckdb(
                name="stale_test",
                con=con,
                queries_json=_QUERIES,
                managed_root=managed,
                downscale_fractions=(0.2,),
                whole_table_threshold=10,
                serve_from=ServeFrom.DUCKDB,
                source_db_path=str(src),
                source_is_static=False,
            )
        finally:
            con.close()

    v1 = reg().dataset_version
    grow = duckdb.connect(str(src))
    _grow_lineitem(grow)
    grow.close()
    v2 = reg().dataset_version

    assert v1 != v2  # the fingerprint tracks the source change
    full = managed / "fraction1" / "subset.duckdb"
    con = duckdb.connect(str(full), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM lineitem").fetchone()[0] == 3000
    finally:
        con.close()


def test_unchanged_live_source_reused_by_default(tmp_path):
    """By default a *live* source reuses its on-disk materialization when the fingerprint is
    unchanged - no re-snapshot, no re-downscale - yet still rebuilds once the source changes. A
    sentinel dropped inside a subset dir survives a reuse (the dir is left untouched) but not a
    rebuild (``_clear_managed_subsets`` rmtrees every ``fraction*`` dir first)."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"

    def reg():
        con = duckdb.connect(str(src))
        try:
            return register_workload_from_duckdb(
                name="reuse_test",
                con=con,
                queries_json=_QUERIES,
                managed_root=managed,
                downscale_fractions=(0.2,),
                whole_table_threshold=10,
                serve_from=ServeFrom.DUCKDB,
                source_db_path=str(src),
                source_is_static=False,
            )
        finally:
            con.close()

    v1 = reg().dataset_version
    # Build the fractional subset lazily, then drop a sentinel inside its dir.
    _prepare(managed, "reuse_test")
    sentinel = managed / "fraction0.2" / "_sentinel"
    sentinel.write_text("keep")

    # Re-registering an unchanged live source reuses verbatim: no re-snapshot, no fractional clear,
    # same fingerprint - the sentinel survives.
    v2 = reg().dataset_version
    assert v2 == v1
    assert sentinel.exists()

    # Changing the source moves the fingerprint and forces a re-snapshot that clears the stale
    # fractional subsets (sentinel is gone); prepare would rebuild them from the new snapshot.
    grow = duckdb.connect(str(src))
    _grow_lineitem(grow)
    grow.close()
    v3 = reg().dataset_version
    assert v3 != v1
    assert not sentinel.exists()


def test_always_resample_rebuilds_live_source(tmp_path):
    """``always_resample`` re-snapshots every run even when the source is unchanged: it freezes a
    new snapshot and clears the fractional subsets (rebuilt lazily), so a sentinel in a subset dir
    does not survive - the escape hatch for a source edited in place without moving its
    fingerprint."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"

    def reg():
        con = duckdb.connect(str(src))
        try:
            return register_workload_from_duckdb(
                name="no_reuse_test",
                con=con,
                queries_json=_QUERIES,
                managed_root=managed,
                downscale_fractions=(0.2,),
                whole_table_threshold=10,
                serve_from=ServeFrom.DUCKDB,
                source_db_path=str(src),
                source_is_static=False,
                always_resample=True,
            )
        finally:
            con.close()

    reg()
    _prepare(managed, "no_reuse_test")
    sentinel = managed / "fraction0.2" / "_sentinel"
    sentinel.write_text("keep")
    reg()
    # re-snapshot clears the fractional subsets despite the unchanged source; prepare rebuilds lazily
    assert not sentinel.exists()


def test_sync_snapshots_only_no_fractional_downscale(tmp_path):
    """Sync freezes a snapshot and materializes only the full benchmark subset; the fractional
    rungs are not downscaled until a run's ``prepare`` asks for them. The snapshot is retained (not
    deleted) because the lazy downscaler reads from it."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"
    con = duckdb.connect(str(src))
    try:
        register_workload_from_duckdb(
            name="snap_only",
            con=con,
            queries_json=_QUERIES,
            managed_root=managed,
            downscale_fractions=(0.2,),
            whole_table_threshold=10,
            serve_from=ServeFrom.DUCKDB,
            source_db_path=str(src),
            source_is_static=False,
        )
    finally:
        con.close()

    assert (managed / "fraction1" / "subset.duckdb").is_symlink()
    assert (managed / ".source_snapshot.duckdb").exists()
    assert not (managed / "fraction0.2").exists()


def test_reingest_after_synthesis_does_not_downscale(tmp_path):
    """A re-ingest that rebuilds (the source changed) never runs the downscaler: sync re-snapshots
    and clears the stale fractional subsets, leaving them absent until the next run's ``prepare``
    rebuilds them."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"

    def reg():
        con = duckdb.connect(str(src))
        try:
            register_workload_from_duckdb(
                name="reingest",
                con=con,
                queries_json=_QUERIES,
                managed_root=managed,
                downscale_fractions=(0.2,),
                whole_table_threshold=10,
                serve_from=ServeFrom.DUCKDB,
                source_db_path=str(src),
                source_is_static=False,
            )
        finally:
            con.close()

    reg()
    _prepare(managed, "reingest")  # a run downscales the fractional subset
    assert (managed / "fraction0.2" / "subset.duckdb").exists()

    # Change the source so the fingerprint moves and the re-ingest rebuilds: it clears the stale
    # fractional subset but does NOT re-downscale (that stays lazy until the next run's prepare).
    grow = duckdb.connect(str(src))
    _grow_lineitem(grow)
    grow.close()

    reg()  # re-ingest: re-snapshots and clears the fractional subset, but does NOT downscale
    assert not (managed / "fraction0.2").exists()


def test_prepare_is_idempotent_and_rebuilds_incomplete_subset(tmp_path, source_db):
    """``prepare`` skips fractions already present (idempotent, cheap to call every run) and
    rebuilds one whose artifact is missing (an interrupted / partial build)."""
    managed = tmp_path / "managed"
    _register("prep_idem", managed, source_db, serve_from=ServeFrom.DUCKDB)
    subset = managed / "fraction0.2" / "subset.duckdb"

    _prepare(managed, "prep_idem")
    assert subset.exists()
    mtime = subset.stat().st_mtime_ns

    _prepare(managed, "prep_idem")  # already present -> not rebuilt
    assert subset.stat().st_mtime_ns == mtime

    subset.unlink()  # simulate an incomplete subset dir (the artifact is gone)
    _prepare(managed, "prep_idem")  # rebuilt on demand
    assert subset.exists()


def test_prepare_noop_when_no_duckdb_source():
    """A workload whose subsets are already on disk (built-ins, plain BYO-parquet) carries no
    ``DuckDBSubsetSource``, so ``prepare`` returns immediately and materializes nothing."""
    from types import SimpleNamespace

    fake = SimpleNamespace(spec=SimpleNamespace(duckdb_source=None))
    assert OLAPWorkloadProvider.prepare(fake) is None


def test_parquet_schema_available_after_sync(tmp_path, source_db):
    """Parquet-mode schema is derived from the benchmark subset (``fraction1``), which exists right
    after sync even though the fractional subsets are lazy."""
    managed = tmp_path / "managed"
    spec = _register("parq_schema", managed, source_db, serve_from=ServeFrom.PARQUET)
    assert not (managed / "fraction0.2").exists()
    assert "CREATE TABLE lineitem" in spec.schema()


def test_native_spec_subset_files_and_ram_check(tmp_path, source_db):
    """A native workload's subset is a single ``subset.duckdb``; RAM measurement must read that,
    not stat ``<table>.parquet`` files that were never written (F14 regression)."""
    from synnodb.ram_check import RamCheck

    managed = tmp_path / "managed"
    spec = _register("nat_ram", managed, source_db, serve_from=ServeFrom.DUCKDB)
    sf_dir = managed / "fraction1"
    assert spec.subset_files(sf_dir) == [sf_dir / "subset.duckdb"]
    rc = RamCheck.measure(sf_dir.name, spec.subset_files(sf_dir))
    assert rc.dataset_bytes > 0


def test_native_batch_extra_env_has_no_shm_ingest(tmp_path, source_db):
    """The pid-scoped SYNNODB_SHM_INGEST must never land in ``batch.extra_env`` (which is hashed
    into the validate-cache key); staging injects it into the run env lazily instead, so the cache
    still replays across processes. ``_duckdb_subset_db`` resolves the subset for that staging."""
    from synnodb.tools.run import _duckdb_subset_db
    from synnodb.tools.run_tool_mode import RunToolMode

    managed = tmp_path / "managed"
    _register("nat_env", managed, source_db, serve_from=ServeFrom.DUCKDB)
    prov = OLAPWorkloadProvider(
        benchmark="nat_env",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    batches = prov.produce_workload(
        RunToolMode.FAST_CHECK, query_ids=["1"], num_threads=1, core_ids=None
    )
    assert batches
    for b in batches:
        assert "SYNNODB_SHM_INGEST" not in (b.extra_env or {})
        subset = _duckdb_subset_db(b)
        assert subset is not None and subset.name == "subset.duckdb"


def test_ssd_batches_have_distinct_storage_dirs(tmp_path, source_db):
    """Each scale factor's batch must carry its own STORAGE_DIR; a single shared env dict left
    every batch pointing at the last scale factor's storage dir (F9 regression)."""
    from synnodb.tools.run_tool_mode import RunToolMode

    managed = tmp_path / "managed"
    _register("ssd_env", managed, source_db, serve_from=ServeFrom.PARQUET)
    prov = OLAPWorkloadProvider(
        benchmark="ssd_env",
        base_parquet_dir=managed,
        db_storage=DBStorage.SSD,
        bespoke_ssd_storage_dir=tmp_path / "ssd",
        query_ids=["1"],
    )
    batches = prov.produce_workload(
        RunToolMode.EXHAUSTIVE, query_ids=["1"], num_threads=1, core_ids=None
    )
    assert len(batches) >= 2
    for b in batches:
        sf = b.exec_settings.scale_factor
        assert b.extra_env["STORAGE_DIR"].rstrip("/").endswith(f"sf{sf}")


def test_duckdb_registration_normalizes_param_keys(tmp_path):
    """A templated query keyed ``q1`` (not the bare ``1``) registers cleanly: its params key is
    normalized the same way the query id is, so the two still match."""
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    src = tmp_path / "src.duckdb"
    _make_source(src)
    managed = tmp_path / "managed"
    queries = {
        "q1": {
            "sql": "SELECT * FROM lineitem WHERE l_orderkey < [MAXKEY]",
            "params": {"MAXKEY": {"type": "int", "min": 1, "max": 50}},
        },
    }
    con = duckdb.connect(str(src))
    try:
        spec = register_workload_from_duckdb(
            name="param_norm",
            con=con,
            queries_json=queries,
            managed_root=managed,
            downscale_fractions=(0.2,),
            whole_table_threshold=10,
            serve_from=ServeFrom.PARQUET,
        )
    finally:
        con.close()
    assert "1" in spec.all_query_ids


def test_native_subset_duckdb_joins_non_vacuous(tmp_path, source_db):
    managed = tmp_path / "managed"
    _register("nat_join", managed, source_db, serve_from=ServeFrom.DUCKDB)
    _prepare(managed, "nat_join")
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
    prov.prepare()
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
    # The oracle reads the fractional subset directly, not through produce_workload - materialize
    # the lazy subsets first (both the native and the parquet fallback root).
    _prepare(managed_native, "ora_native")
    _prepare(managed_parquet, "ora_parquet")

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


def test_run_env_injects_shm_ingest_for_duckdb_subset():
    """Both execution paths in ``run_worker`` - the validated path and the benchmark/no-validator
    path - build their run env through ``_run_env_with_optional_shm_ingest``. For a DuckDB batch it
    must stage the subset and add ``SYNNODB_SHM_INGEST`` on top of the merged base env; for a
    non-DuckDB batch it must return the base env untouched. The benchmark path regressed here: it
    never staged the subset, so the loader fell back to a non-existent ``<table>.parquet`` and the
    run failed instead of executing."""
    import shutil

    from synnodb.tools.run import _run_env_with_optional_shm_ingest

    tmp = Path(tempfile.mkdtemp())
    subset_db = tmp / "subset.duckdb"
    con = duckdb.connect(str(subset_db))
    con.execute("CREATE TABLE lineitem AS SELECT i AS l_id FROM range(10) t(i)")
    con.close()

    # base env carries CORE_IDS (the merged general env) - the benchmark path used to drop it.
    base_env = {"CORE_IDS": "0,1"}

    # non-DuckDB batch (subset_db is None): env passes through untouched, nothing staged.
    assert _run_env_with_optional_shm_ingest(base_env, None) is base_env

    ingest = None
    try:
        run_env = _run_env_with_optional_shm_ingest(base_env, subset_db)
        ingest = Path(run_env["SYNNODB_SHM_INGEST"])
        assert (
            run_env["CORE_IDS"] == "0,1"
        )  # merged base env preserved (core pinning survives)
        assert "SYNNODB_SHM_INGEST" not in base_env  # caller's dict is not mutated
        assert (ingest / "lineitem.arrow").exists()  # loader has a real segment to map
    finally:
        if ingest is not None:
            shutil.rmtree(ingest, ignore_errors=True)


def test_shm_staging_restages_when_subset_content_changes():
    """A subset rebuilt in place (new content at the same path) must re-stage into a fresh ingest
    dir, not serve the first staging's stale segments - the ingest key includes the file content."""
    import shutil

    import pyarrow as pa
    import pyarrow.ipc as ipc

    from synnodb.cpp_runner.shm_stage import stage_subset_duckdb_to_shm

    tmp = Path(tempfile.mkdtemp())
    subset_db = tmp / "subset.duckdb"
    con = duckdb.connect(str(subset_db))
    con.execute("CREATE TABLE t AS SELECT i AS x FROM range(10) r(i)")
    con.close()
    first = stage_subset_duckdb_to_shm(subset_db)
    second = None
    try:
        # rebuild the subset in place with different content
        subset_db.unlink()
        con = duckdb.connect(str(subset_db))
        con.execute("CREATE TABLE t AS SELECT i AS x FROM range(50) r(i)")
        con.close()
        second = stage_subset_duckdb_to_shm(subset_db)
        assert second != first  # content changed -> new ingest dir, not stale reuse
        with pa.memory_map(str(second / "t.arrow"), "r") as src:
            assert ipc.open_file(src).read_all().num_rows == 50
    finally:
        shutil.rmtree(first, ignore_errors=True)
        if second is not None:
            shutil.rmtree(second, ignore_errors=True)


# --------------------------------------------------------------------- DuckDB thread resync
def test_set_thread_config_resyncs_live_connection_without_reload(tmp_path, source_db):
    """The oracle connection's PRAGMA threads must track whatever thread count it is asked to run
    at next, without tearing down and re-materializing the (potentially large) in-memory tables -
    see ``set_thread_config``."""
    from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
        DuckDBConnectionManager,
    )

    managed = tmp_path / "managed"
    _register("thr_direct", managed, source_db, serve_from=ServeFrom.PARQUET)
    _prepare(managed, "thr_direct")

    mgr = DuckDBConnectionManager(
        pre_load_duckdb_tables=False,
        dataset_tables=_TABLES,
        parquet_path=managed,
        benchmark=None,
        db_storage=DBStorage.IN_MEMORY,
        sf=1.0,
        pin_worker=True,
        pin_core=3,
        num_threads=1,
        run_duckdb_on_parquet=False,
        serve_from=ServeFrom.PARQUET,
        drop_os_caches_before_sql=False,
    )
    try:
        _, table, _ = mgr.duckdb_sql_arrow("SELECT count(*) AS n FROM lineitem")
        row_count = table.to_pydict()["n"][0]
        assert row_count == 1000
        con_before = mgr.con
        assert (
            con_before.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        )

        # Switch to multi-threaded: same connection object, no table reload, PRAGMA updated.
        mgr.set_thread_config(num_threads=4, pin_worker=False, pin_core=None)
        assert mgr.con is con_before
        assert mgr.num_threads == 4
        assert mgr.pin_worker is False
        _, table2, _ = mgr.duckdb_sql_arrow("SELECT count(*) AS n FROM lineitem")
        assert table2.to_pydict()["n"][0] == row_count  # data untouched, not reloaded
        assert (
            mgr.con.execute("SELECT current_setting('threads')").fetchone()[0] == 4
        )

        # Switch back to single-threaded/pinned.
        mgr.set_thread_config(num_threads=1, pin_worker=True, pin_core=3)
        assert mgr.con is con_before
        assert (
            mgr.con.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        )

        # A no-op resync (same thread count) must not blow away the connection either.
        mgr.set_thread_config(num_threads=1, pin_worker=True, pin_core=3)
        assert mgr.con is con_before
    finally:
        mgr.clear_mem_footprint()


def test_set_thread_config_rejects_inconsistent_pin_combos(tmp_path, source_db):
    from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
        DuckDBConnectionManager,
    )

    managed = tmp_path / "managed"
    _register("thr_bad", managed, source_db, serve_from=ServeFrom.PARQUET)
    _prepare(managed, "thr_bad")

    mgr = DuckDBConnectionManager(
        pre_load_duckdb_tables=False,
        dataset_tables=_TABLES,
        parquet_path=managed,
        benchmark=None,
        db_storage=DBStorage.IN_MEMORY,
        sf=1.0,
        pin_worker=True,
        pin_core=3,
        num_threads=1,
        run_duckdb_on_parquet=False,
        serve_from=ServeFrom.PARQUET,
        drop_os_caches_before_sql=False,
    )
    try:
        with pytest.raises(AssertionError):
            mgr.set_thread_config(num_threads=4, pin_worker=True, pin_core=3)
        with pytest.raises(AssertionError):
            mgr.set_thread_config(num_threads=1, pin_worker=True, pin_core=None)
    finally:
        mgr.clear_mem_footprint()


def test_olap_system_factory_resyncs_duckdb_thread_count_on_reuse(tmp_path, source_db):
    """Reproduces the run-tool invariant this fix restores: within a single run,
    ``OLAPSystemFactory`` must hand back a DuckDB connection running at whatever thread count the
    run tool currently asks for - not whatever it happened to be built with first (e.g. serial
    generation, then a later multi-threaded validation gate)."""
    from synnodb.tools.run_tool_mode import RunToolMode
    from synnodb.workloads.system_factory_olap import OLAPSystemFactory
    from synnodb.workloads.workload_provider import GeneralSystemConfig

    managed = tmp_path / "managed"
    _register("thr_factory", managed, source_db, serve_from=ServeFrom.PARQUET)
    prov = OLAPWorkloadProvider(
        benchmark="thr_factory",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    batches = prov.produce_workload(
        RunToolMode.EXHAUSTIVE, query_ids=["1"], num_threads=1, core_ids=None
    )
    # The benchmark SF batch is always last (see produce_workload) - its fraction1 subset was
    # materialized directly by _register's sync, no downscaling needed.
    exec_settings = batches[-1].exec_settings

    factory = OLAPSystemFactory()
    gsc_serial = GeneralSystemConfig(memory_limit_mb=None, num_threads=1, core_ids=None)
    gsc_parallel = GeneralSystemConfig(
        memory_limit_mb=None, num_threads=4, core_ids=[0, 1, 2, 3]
    )

    mgr_serial = factory.get_system(
        System.DUCKDB,
        benchmark=prov.benchmark,
        exec_settings=exec_settings,
        general_system_config=gsc_serial,
    )
    mgr_serial.duckdb_sql_arrow("SELECT count(*) AS n FROM lineitem")
    assert mgr_serial.num_threads == 1
    assert mgr_serial.pin_worker is True

    mgr_parallel = factory.get_system(
        System.DUCKDB,
        benchmark=prov.benchmark,
        exec_settings=exec_settings,
        general_system_config=gsc_parallel,
    )
    # Same cached connection (con_key is dataset-only), resynced in place - not rebuilt.
    assert mgr_parallel is mgr_serial
    assert mgr_parallel.num_threads == 4
    assert mgr_parallel.pin_worker is False
    _, table, _ = mgr_parallel.duckdb_sql_arrow("SELECT count(*) AS n FROM lineitem")
    assert table.to_pydict()["n"][0] == 1000
    assert (
        mgr_parallel.con.execute("SELECT current_setting('threads')").fetchone()[0]
        == 4
    )
