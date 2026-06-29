"""The strict write block: only read-only queries pass through ``con.execute``; everything
else raises ``WriteNotSupportedError``. Data setup goes through the ``.duckdb`` escape hatch.
"""
from __future__ import annotations

import pytest

import synnodb
from synnodb import WriteNotSupportedError
from synnodb.router import RouterPolicy


def _con(**policy):
    return synnodb.connect(policy=RouterPolicy(**policy) if policy else None)


BLOCKED = [
    "CREATE TABLE t(a int)",
    "CREATE OR REPLACE TABLE t(a int)",
    "CREATE VIEW v AS SELECT 1",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN b int",
    "COPY t FROM 'x.csv'",
    "SET threads = 2",
    "INSTALL httpfs",
    "ATTACH 'x.db' AS y",
    "BEGIN TRANSACTION",
]

ALLOWED = [
    "SELECT 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "EXPLAIN SELECT 1",
    "(SELECT 1)",  # parenthesized select still recognized as read
]


@pytest.mark.parametrize("sql", BLOCKED)
def test_writes_are_blocked(sql):
    con = _con()
    with pytest.raises(WriteNotSupportedError):
        con.execute(sql)


@pytest.mark.parametrize("sql", ALLOWED)
def test_reads_pass_through(sql):
    con = _con()
    con.execute(sql)  # must not raise


def test_executemany_is_blocked():
    con = _con()
    con.duckdb.execute("CREATE TABLE t(a int)")
    with pytest.raises(WriteNotSupportedError):
        con.executemany("INSERT INTO t VALUES (?)", [[1], [2]])


def test_escape_hatch_allows_writes():
    con = _con()
    con.duckdb.execute("CREATE TABLE t(a int)")
    con.duckdb.execute("INSERT INTO t VALUES (1),(2),(3)")
    assert con.execute("SELECT count(*) FROM t").fetchall() == [(3,)]


def test_read_only_introspection_passes():
    con = _con()
    con.duckdb.execute("CREATE TABLE t(a int, b varchar)")
    con.execute("DESCRIBE t")            # read-only introspection
    con.execute("PRAGMA table_info('t')")


def test_block_writes_false_passes_through():
    con = _con(block_writes=False)
    con.execute("CREATE TABLE t(a int)")  # now runs on DuckDB
    con.execute("INSERT INTO t VALUES (7)")
    assert con.execute("SELECT a FROM t").fetchall() == [(7,)]


def test_block_writes_env(monkeypatch):
    monkeypatch.setenv("SYNNODB_BLOCK_WRITES", "off")
    assert RouterPolicy.from_env().block_writes is False
    monkeypatch.setenv("SYNNODB_BLOCK_WRITES", "on")
    assert RouterPolicy.from_env().block_writes is True


def test_blocked_write_increments_counter():
    con = _con()
    for sql in ("INSERT INTO t VALUES (1)", "CREATE TABLE t(a int)"):
        with pytest.raises(WriteNotSupportedError):
            con.execute(sql)
    assert con.router_stats()["session"]["blocked_writes"] == 2


def test_block_message_names_statement():
    con = _con()
    with pytest.raises(WriteNotSupportedError, match="INSERT"):
        con.execute("INSERT INTO t VALUES (1)")


# CTE-led DML and multi-statement strings must not slip past the leading-keyword check.
HIDDEN_WRITES = [
    "WITH x AS (SELECT 1) DELETE FROM t",
    "WITH x AS (SELECT 1 AS v) INSERT INTO t SELECT v FROM x",
    "SELECT 1; DROP TABLE t",
    "SELECT 1; INSERT INTO t VALUES (9)",
]

HIDDEN_READS = [
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT 1; SELECT 2",
    "SELECT ';' AS semicolon_in_string",
    "SELECT 1;",
]


@pytest.mark.parametrize("sql", HIDDEN_WRITES)
def test_cte_and_multistatement_writes_blocked(sql):
    con = _con()
    con.duckdb.execute("CREATE TABLE t(a int)")
    with pytest.raises(WriteNotSupportedError):
        con.execute(sql)


@pytest.mark.parametrize("sql", HIDDEN_READS)
def test_cte_and_multistatement_reads_pass(sql):
    con = _con()
    con.duckdb.execute("CREATE TABLE t(a int)")
    con.execute(sql)  # must not raise
