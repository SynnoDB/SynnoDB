"""End-to-end for optimizing an existing DuckDB database: ``optimize_database`` publishes
``synno-<db>``, then queries route over both planes - the zero-copy Arrow shm hot-load (the
loaded database's own in-memory data) and the self-contained parquet snapshot (the
synthesized database, no live DuckDB of your own). Cross-checked against DuckDB.

Skipped unless the recompiled q1q6byo engine and DuckDB's tpch extension are available.
"""

from __future__ import annotations

import glob
import tempfile
from pathlib import Path

import duckdb
import pytest

import synnodb
from synnodb import optimize_database
from synnodb.router import RouterMode, RouterPolicy
from synnodb.router.adapt import results_equal
from synnodb.router.manifest import EngineManifest
from synnodb.workloads.query_params import substitute
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider
from synnodb.utils.utils import DBStorage

Q1Q6BYO = Path("/home/teckmann/SynnoDB/q1q6byo")


def _exact_arrow_engine() -> bool:
    """The local q1q6byo fixture, recompiled with the shm-ingest loader AND the exact Arrow
    egress (column_egress). Both markers live in the plugins, so a stale binary skips."""
    loader = Q1Q6BYO / "build" / "libloader.so"
    query = Q1Q6BYO / "build" / "libquery.so"
    try:
        # exists() raises PermissionError (instead of returning False) when a
        # parent directory is unreadable, e.g. another user's home.
        if not (Q1Q6BYO / "db").exists() or not loader.exists() or not query.exists():
            return False
        return (
            b"SYNNODB_SHM_INGEST" in loader.read_bytes()
            and b"SYNNODB_RESULT_DIR" in query.read_bytes()
        )
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _exact_arrow_engine(),
    reason="requires q1q6byo recompiled with shm ingest + exact Arrow egress (column_egress)",
)


@pytest.fixture(scope="module")
def tpch_db(tmp_path_factory):
    """A tiny on-disk TPC-H database (sf=0.01). Skips if the tpch extension is unavailable."""
    dbfile = tmp_path_factory.mktemp("tpchdb") / "tpch.db"
    con = duckdb.connect(str(dbfile))
    try:
        con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01)")
    except Exception as exc:  # pragma: no cover - environment without the extension
        con.close()
        pytest.skip(f"DuckDB tpch extension unavailable: {exc}")
    con.close()
    return dbfile


def _queries(dbfile):
    prov = OLAPWorkloadProvider(
        benchmark="tpch",
        base_parquet_dir=dbfile.parent,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1", "6"],
    )
    q1 = substitute(prov.sql_dict["Q1"], {"DELTA": "90"})
    q6 = substitute(
        prov.sql_dict["Q6"],
        {"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"},
    )
    return q1, q6


def _reference(dbfile, sql):
    ref = duckdb.connect(str(dbfile))
    try:
        return ref.execute(sql).to_arrow_table()
    finally:
        ref.close()


def _policy():
    return RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0)


def test_optimize_publishes_synno_named_engine(tpch_db):
    with tempfile.TemporaryDirectory() as tmp:
        dest = optimize_database(
            tpch_db,
            ["1", "6"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(Path(tmp) / "engines"),
            data_plane="auto",
        )
        assert dest.name == "synno-tpch"
        man = EngineManifest.read(dest / "manifest.json")
        assert man.shm_capable is True
        assert man.parquet_dir == "data"
        assert (dest / "data" / "lineitem.parquet").exists()
        assert {q.query_id for q in man.queries} == {"1", "6"}
        assert set(man.expected_tables) == {"lineitem"}


def test_data_plane_variants(tpch_db):
    with tempfile.TemporaryDirectory() as tmp:
        shm = optimize_database(
            tpch_db,
            ["1"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(Path(tmp) / "shm"),
            data_plane="shm",
        )
        m_shm = EngineManifest.read(shm / "manifest.json")
        assert m_shm.shm_capable is True and m_shm.parquet_dir is None
        assert not (shm / "data").exists()

        pq = optimize_database(
            tpch_db,
            ["1"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(Path(tmp) / "pq"),
            data_plane="parquet",
        )
        m_pq = EngineManifest.read(pq / "manifest.json")
        assert m_pq.shm_capable is False and m_pq.parquet_dir == "data"
        assert (pq / "data" / "lineitem.parquet").exists()


def test_shm_hot_load_plane(tpch_db):
    q1, q6 = _queries(tpch_db)
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        optimize_database(
            tpch_db,
            ["1", "6"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(engines),
            data_plane="auto",
        )
        # Plain on-disk connection: the engine hot-loads its tables as Arrow over shm.
        con = synnodb.connect(str(tpch_db), engines=str(engines), policy=_policy())
        try:
            con.refresh_engines()
            assert con.router_stats()["registry"]["templates"] == 2
            for sql in (q1, q6):
                assert con.why(sql)["decision"] == "would-route"
                got = con.execute(sql).to_arrow_table()
                assert results_equal(got, _reference(tpch_db, sql), ordered=True)
            # The engine reproduces DuckDB bit-for-bit (decimals built from int128 via
            # column_egress), so both queries route and the exact cross-check finds 0 mismatches.
            session = con.router_stats()["session"]
            assert session["routed"] == 2 and session["fell_back"] == 0
            assert session["cross_check_mismatch"] == 0
        finally:
            con.close()


def test_parquet_synthesized_plane(tpch_db):
    q1, q6 = _queries(tpch_db)
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        optimize_database(
            tpch_db,
            ["1", "6"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(engines),
            data_plane="auto",
        )
        # No database of our own: mount the engine's bundled snapshot and query it.
        con = synnodb.connect(
            ":memory:", engines=str(engines), mount=True, policy=_policy()
        )
        try:
            con.refresh_engines()
            assert con.router_stats()["registry"]["templates"] == 2
            assert con.duckdb.execute("SELECT count(*) FROM lineitem").fetchone()[0] > 0
            for sql in (q1, q6):
                assert con.why(sql)["decision"] == "would-route"
                got = con.execute(sql).to_arrow_table()
                assert results_equal(got, _reference(tpch_db, sql), ordered=True)
            session = con.router_stats()["session"]
            assert (
                session["routed"] == 2 and session["cross_check_mismatch"] == 0
            )  # bit-exact
        finally:
            con.close()


def test_near_miss_falls_back(tpch_db):
    q1, _ = _queries(tpch_db)
    near_miss = q1.replace(
        "1998-12-01", "1997-01-01"
    )  # a constant the engine was not built for
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        optimize_database(
            tpch_db,
            ["1"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(engines),
            data_plane="auto",
        )
        con = synnodb.connect(str(tpch_db), engines=str(engines), policy=_policy())
        try:
            con.refresh_engines()
            assert con.why(q1)["decision"] == "would-route"
            assert con.why(near_miss)["decision"] == "would-fall-back"
            assert (
                con.execute(near_miss).to_arrow_table().num_rows >= 1
            )  # still correct via DuckDB
        finally:
            con.close()


def test_shm_segments_cleaned_up_on_close(tpch_db):
    q1, _ = _queries(tpch_db)
    before = set(glob.glob("/dev/shm/synno-ingest-*"))
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        optimize_database(
            tpch_db,
            ["1"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(engines),
            data_plane="shm",
        )
        con = synnodb.connect(str(tpch_db), engines=str(engines), policy=_policy())
        con.refresh_engines()
        con.execute(q1).fetchall()
        assert set(glob.glob("/dev/shm/synno-ingest-*")) - before, (
            "an ingest dir exists while open"
        )
        con.close()
        assert not (set(glob.glob("/dev/shm/synno-ingest-*")) - before), (
            "ingest dir leaked after close"
        )


def test_shm_only_engine_needs_live_data(tpch_db):
    """A shm-only engine (no bundled snapshot) cannot serve a connection that has no data of its
    own - mounting finds nothing to mount, so it stays unregistered until data is loaded."""
    q1, _ = _queries(tpch_db)
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        optimize_database(
            tpch_db,
            ["1"],
            engine_workspace=Q1Q6BYO,
            benchmark="tpch",
            engines_dir=str(engines),
            data_plane="shm",
        )
        empty = synnodb.connect(
            ":memory:", engines=str(engines), mount=True, policy=_policy()
        )
        try:
            empty.refresh_engines()
            assert (
                empty.router_stats()["registry"]["templates"] == 0
            )  # nothing to serve
            assert empty.why(q1)["decision"] == "would-fall-back"
        finally:
            empty.close()
        # With the data loaded, the same engine hot-loads and routes.
        loaded = synnodb.connect(str(tpch_db), engines=str(engines), policy=_policy())
        try:
            loaded.refresh_engines()
            assert loaded.router_stats()["registry"]["templates"] == 1
            assert loaded.why(q1)["decision"] == "would-route"
        finally:
            loaded.close()
