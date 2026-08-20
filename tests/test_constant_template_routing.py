"""Routing for constant templates (no placeholders), e.g. CEB's catalog queries.

A constant template's engine result is baked in, and the structural key abstracts
constants away, so a same-shaped query with different constants matches the key too.
The arity guard must therefore route only the template's own statement (same_statement:
formatting, comments, and keyword case are irrelevant; every literal and identifier is
significant). Serving a non-identical statement would be a wrong result, so probes run
against an engine that returns a poisoned row - a breach fails on the values, not just
on the bookkeeping.

Run: .venv/bin/python -m pytest tests/test_constant_template_routing.py -q
"""

from __future__ import annotations

import pyarrow as pa
import pytest

import synnodb
from synnodb.router import (
    LocalCallableEngine,
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)
from synnodb.router.guards import GuardContext, placeholder_arity_guard
from synnodb.router.normalize import same_statement
from synnodb.router.registry import EngineBinding

TEMPLATE = (
    "SELECT l_returnflag, count(*) AS n FROM lineitem "
    "WHERE l_quantity < 24 AND l_shipdate <= DATE '1998-09-02' "
    "AND l_comment LIKE '%special%' GROUP BY l_returnflag ORDER BY l_returnflag"
)

POISON = pa.table(
    {"l_returnflag": pa.array(["POISON"]), "n": pa.array([-1], pa.int64())}
)


@pytest.fixture
def con():
    # cross_check_rate=0 so a false match would actually serve the poisoned row.
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=0.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute(
        "CREATE TABLE lineitem(l_returnflag VARCHAR, l_quantity INT, "
        "l_shipdate DATE, l_comment VARCHAR)"
    )
    register_engine(
        con,
        template_sql=TEMPLATE,
        engine=LocalCallableEngine("eng-const", {"1": lambda ph: POISON}),
        placeholders=[],
    )
    yield con
    con.close()


def _routed(con, sql, parameters=None) -> bool:
    con.execute(sql, parameters).fetchall()
    return (con._last or {}).get("served_by") == "engine"


@pytest.mark.parametrize(
    "variant",
    [
        TEMPLATE,
        TEMPLATE.replace(" WHERE ", "\n  WHERE\n "),
        TEMPLATE + "\n-- nightly replay",
        TEMPLATE.replace("SELECT", "select").replace("WHERE", "where"),
    ],
    ids=["identical", "whitespace", "comment", "keyword-case"],
)
def test_exact_statement_routes(con, variant):
    assert _routed(con, variant)


@pytest.mark.parametrize(
    "mutated",
    [
        TEMPLATE.replace("< 24", "< 25"),
        TEMPLATE.replace("1998-09-02", "1998-09-01"),
        TEMPLATE.replace("%special%", "%express%"),
        TEMPLATE.replace("%special%", "%SPECIAL%"),
        TEMPLATE.replace("< 24", "< 24.0"),
        TEMPLATE.replace(" AS n ", " AS N "),
    ],
    ids=["int", "date", "string", "string-case", "numeric-spelling", "alias-case"],
)
def test_changed_literal_falls_back(con, mutated):
    assert not _routed(con, mutated)
    rows = con.execute(mutated).fetchall()
    assert ("POISON", -1) not in rows


def test_empty_explicit_params_still_require_exact_statement(con):
    # `execute(sql, [])` used to pass the guard on count alone (0 == 0) even when the
    # inline literals differed from the template's.
    assert _routed(con, TEMPLATE, [])
    assert not _routed(con, TEMPLATE.replace("< 24", "< 99"), [])


def test_binding_without_template_sql_never_routes():
    binding = EngineBinding(
        template_id="legacy::1",
        normalized_sql="irrelevant",
        query_id="1",
        engine_id="legacy",
        placeholders=(),
        output_schema=(),
        tables=frozenset(),
        schema_fingerprint="",
        template_sql=None,
    )
    ctx = GuardContext(sql=TEMPLATE, binding=binding, conn=None, registry=None)
    ok, detail = placeholder_arity_guard(ctx)
    assert not ok and "no template_sql" in detail


def test_parameterized_templates_unaffected():
    # A one-placeholder engine still routes positionally on any literal value.
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=0.0),
        registry=TemplateRegistry(),
    )
    try:
        con.duckdb.execute("CREATE TABLE t(a INTEGER)")
        register_engine(
            con,
            template_sql="SELECT count(*) AS c FROM t WHERE a >= 2",
            engine=LocalCallableEngine(
                "eng-p", {"1": lambda ph: pa.table({"c": pa.array([0], pa.int64())})}
            ),
            placeholders=[PlaceholderSpec("p0", "INTEGER")],
        )
        assert _routed(con, "SELECT count(*) AS c FROM t WHERE a >= 7")
    finally:
        con.close()


def test_json_key_vs_path_never_conflated():
    # sqlglot canonicalizes a bare JSON member key into a JSONPath at parse time
    # (`'a.b'` becomes `'$.a.b'`), but DuckDB executes the two differently: the key form
    # reads the top-level member literally named "a.b". A constant template registered
    # for one form must never serve the other; the raw string-literal tokens keep them
    # apart. Exercised through the full pipeline with a poisoned engine.
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=0.0),
        registry=TemplateRegistry(),
    )
    try:
        con.duckdb.execute("CREATE TABLE j(doc JSON)")
        con.duckdb.execute("""INSERT INTO j VALUES ('{"a": {"b": 5}, "a.b": 99}')""")
        template = "SELECT doc ->> '$.a.b' AS v FROM j"
        register_engine(
            con,
            template_sql=template,
            engine=LocalCallableEngine(
                "eng-json", {"1": lambda ph: pa.table({"v": pa.array(["POISON"])})}
            ),
            placeholders=[],
        )
        assert _routed(con, template)
        # The function surface with the same path string is the same statement.
        assert _routed(con, "SELECT json_extract_string(doc, '$.a.b') AS v FROM j")
        # The bare-key forms read the top-level member literally named "a.b" - a
        # different statement, so they must fall back to DuckDB.
        for attack in (
            "SELECT doc ->> 'a.b' AS v FROM j",
            "SELECT json_extract_string(doc, 'a.b') AS v FROM j",
        ):
            assert not _routed(con, attack), attack
            assert con.execute(attack).fetchall() == [("99",)]
    finally:
        con.close()


def test_search_path_change_disables_routing():
    # An unqualified table name means whatever the session search_path says it means.
    # The engine's baked result is only valid for the resolution it was registered
    # under, so flipping search_path must flip the schema/session fingerprint and fall
    # back - even for the byte-identical statement against a same-shaped shadow table
    # that already existed at registration.
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=0.0),
        registry=TemplateRegistry(),
    )
    try:
        for table in ("lineitem", "alt.lineitem"):
            if table.startswith("alt."):
                con.duckdb.execute("CREATE SCHEMA alt")
            con.duckdb.execute(
                f"CREATE TABLE {table}(l_returnflag VARCHAR, l_quantity INT, "
                "l_shipdate DATE, l_comment VARCHAR)"
            )
        register_engine(
            con,
            template_sql=TEMPLATE,
            engine=LocalCallableEngine("eng-sp", {"1": lambda ph: POISON}),
            placeholders=[],
        )
        assert _routed(con, TEMPLATE)
        con.duckdb.execute("SET search_path='alt'")
        assert not _routed(con, TEMPLATE)
        con.duckdb.execute("RESET search_path")
        assert _routed(con, TEMPLATE)
    finally:
        con.close()


def test_same_statement():
    assert same_statement("select  1 -- x\n", "SELECT 1")
    assert not same_statement("SELECT 1", "SELECT 2")
    assert not same_statement("not sql (", "not sql (")
    assert not same_statement(
        "SELECT doc ->> '$.a.b' FROM j", "SELECT doc ->> 'a.b' FROM j"
    )
