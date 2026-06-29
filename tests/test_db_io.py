"""Loading a DuckDB file into memory and exporting tables to parquet (the data plumbing
behind ``optimize_database`` and the bundled parquet snapshot)."""
from __future__ import annotations

import duckdb
import pytest

import synnodb
from synnodb.duckdb_compat.db_io import (
    export_tables_to_parquet,
    list_tables,
    load_database_into_memory,
)


def _make_db(path) -> None:
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE a(x INTEGER); INSERT INTO a VALUES (1),(2),(3)")
    c.execute("CREATE TABLE b(y VARCHAR); INSERT INTO b VALUES ('p'),('q')")
    c.close()


def test_load_database_into_memory(tmp_path):
    dbfile = tmp_path / "src.db"
    _make_db(dbfile)
    mem = duckdb.connect(":memory:")
    tables = load_database_into_memory(mem, dbfile)
    assert set(tables) == {"a", "b"}
    assert mem.execute("SELECT count(*) FROM a").fetchone()[0] == 3
    assert mem.execute("SELECT count(*) FROM b").fetchone()[0] == 2
    # the source database is detached again (only the copied data remains in memory)
    attached = mem.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE catalog_name = '_synno_src'"
    ).fetchone()[0]
    assert attached == 0
    assert set(list_tables(mem)) == {"a", "b"}


def test_load_missing_file_raises(tmp_path):
    mem = duckdb.connect(":memory:")
    with pytest.raises(FileNotFoundError):
        load_database_into_memory(mem, tmp_path / "nope.db")


def test_export_tables_to_parquet(tmp_path):
    mem = duckdb.connect(":memory:")
    mem.execute("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1),(2)")
    out = export_tables_to_parquet(mem, ["t"], tmp_path / "snap")
    assert (out / "t.parquet").exists()
    assert mem.execute(f"SELECT count(*) FROM read_parquet('{out}/t.parquet')").fetchone()[0] == 2


def test_connect_file_is_a_plain_duckdb_passthrough(tmp_path):
    # connect(file) opens the file on disk, exactly like duckdb.connect - the engine
    # hot-loads its data as Arrow over shm regardless, so no in-memory flag is needed.
    dbfile = tmp_path / "src.db"
    _make_db(dbfile)
    con = synnodb.connect(str(dbfile))
    try:
        assert con.execute("SELECT sum(x) FROM a").fetchone()[0] == 6
        assert con.execute("SELECT count(*) FROM b").fetchone()[0] == 2
    finally:
        con.close()


def test_load_into_memory_helper_on_a_fresh_connection(tmp_path):
    # The explicit RAM-resident-copy path stays available as a helper on the inner connection.
    dbfile = tmp_path / "src.db"
    _make_db(dbfile)
    con = synnodb.connect(":memory:")
    try:
        load_database_into_memory(con.duckdb, dbfile)
        assert con.execute("SELECT sum(x) FROM a").fetchone()[0] == 6
    finally:
        con.close()
