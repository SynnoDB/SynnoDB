"""The bind-time exactness guard: a query whose output types the engine cannot reproduce exactly
is refused at bind (DuckDB serves it) with a verbose reason, instead of binding and failing later
inside egress. The vocabulary is wide - every integer width, decimal128/256, BOOLEAN, DOUBLE,
VARCHAR, DATE, TIMESTAMP - so the guard is a deny-list of the genuinely unreachable (nested, blob,
interval, time, uuid, ...).
"""
from __future__ import annotations

import duckdb
import pytest

from synnodb.duckdb_compat.connection import SynnoConnection
from synnodb.errors import SynnoUnsupportedQuery
from synnodb.router.registration import _unsupported_output_reasons, make_binding
from synnodb.router.registry import ColumnSpec


class _Eng:
    engine_id = "e"


def _conn():
    return SynnoConnection(duckdb.connect(":memory:"), None)


def test_supported_output_types_bind():
    conn = _conn()
    try:
        conn.duckdb.execute(
            "CREATE TABLE t(a INTEGER, big BIGINT, h HUGEINT, b DECIMAL(10,2), wide DECIMAL(38,4), "
            "s VARCHAR, d DATE, ts TIMESTAMP, flag BOOLEAN, dbl DOUBLE)"
        )
        binding = make_binding(
            conn, template_sql="SELECT a, big, h, b, wide, s, d, ts, flag, dbl FROM t",
            engine=_Eng(), query_id="1",
        )
        assert binding is not None and len(binding.output_schema) == 10
    finally:
        conn.close()


def test_nested_output_is_refused():
    conn = _conn()
    try:
        conn.duckdb.execute("CREATE TABLE t(a INTEGER)")
        with pytest.raises(SynnoUnsupportedQuery) as ei:
            make_binding(conn, template_sql="SELECT [a, a] AS arr FROM t", engine=_Eng(), query_id="7")
        assert "nested" in str(ei.value).lower() and "arr" in str(ei.value)
    finally:
        conn.close()


def test_interval_output_is_refused():
    conn = _conn()
    try:
        conn.duckdb.execute("CREATE TABLE t(a INTEGER)")
        with pytest.raises(SynnoUnsupportedQuery):
            make_binding(conn, template_sql="SELECT INTERVAL 1 DAY AS iv FROM t", engine=_Eng(), query_id="8")
    finally:
        conn.close()


def test_reason_helper_handles_time_vs_timestamp_and_decimals():
    # The substring trap: naive TIMESTAMP must NOT be refused just because it starts with TIME.
    assert _unsupported_output_reasons([ColumnSpec("x", "TIMESTAMP")]) == []
    assert _unsupported_output_reasons([ColumnSpec("x", "DECIMAL(38,2)")]) == []
    assert _unsupported_output_reasons([ColumnSpec("x", "HUGEINT")]) == []
    # Genuinely unreachable / not exactly reproducible:
    assert _unsupported_output_reasons([ColumnSpec("x", "TIME")])
    assert _unsupported_output_reasons([ColumnSpec("x", "INTEGER[]")])
    assert _unsupported_output_reasons([ColumnSpec("x", "STRUCT(a INTEGER)")])
    assert _unsupported_output_reasons([ColumnSpec("x", "BLOB")])
    assert _unsupported_output_reasons([ColumnSpec("x", "MAP(INTEGER, VARCHAR)")])


def test_reason_helper_closes_grammar_holes():
    # Array spelled with a size, a time-zone-bearing timestamp, and JSON were all wrongly ALLOWED
    # by a leading-token-only deny-list; the guard must catch DuckDB's full type grammar.
    assert _unsupported_output_reasons([ColumnSpec("x", "INTEGER[3]")])             # fixed-size array
    assert _unsupported_output_reasons([ColumnSpec("x", "TIMESTAMP WITH TIME ZONE")])
    assert _unsupported_output_reasons([ColumnSpec("x", "TIMESTAMPTZ")])
    assert _unsupported_output_reasons([ColumnSpec("x", "JSON")])
    # ...but the naive, exactly-reproducible forms remain allowed.
    assert _unsupported_output_reasons([ColumnSpec("x", "TIMESTAMP")]) == []
    assert _unsupported_output_reasons([ColumnSpec("x", "VARCHAR")]) == []
