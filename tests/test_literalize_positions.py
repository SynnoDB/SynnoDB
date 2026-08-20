"""Positional unit tests for ``_literalize``'s token-offset splicing.

``_literalize`` assigns each anonymous ``?`` its binding group's type by the
placeholder's TEXTUAL position (specs are declared in textual order), never by
AST traversal order - sqlglot's ``transform`` visits ``LIMIT ?`` before the
WHERE clause, which is exactly the ClickBench Q7 regression: ``LIMIT`` got the
DATE meant for ``EventDate >= ?`` and DuckDB's binder refused the template.

The oracle in every case is sharp and purely textual: scan the literalized SQL
left to right for ``CAST(NULL AS <type>)`` and require the k-th occurrence to
carry exactly the k-th expected type (specs are chosen with heterogeneous
types, so any positional shuffle changes the sequence detectably). Expected
types are written in DuckDB's canonical rendering (VARCHAR -> TEXT,
INTEGER -> INT), which is how the implementation emits them.
"""

from __future__ import annotations

import re
from typing import List

import duckdb

from synnodb.router.registration import _literalize, describe_output
from synnodb.router.registry import PlaceholderSpec

P = PlaceholderSpec

# One CAST(NULL AS <type>) occurrence, capturing the type name.
_CAST_NULL_RE = re.compile(r"CAST\(NULL AS ([A-Za-z][A-Za-z0-9_]*(?:\([^()]*\))?)\)")

# The Q7 template exactly as the ClickBench manifest ships it, trailing
# semicolon included, with its real spec list (textual order).
Q7_TEMPLATE = (
    "SELECT URL, COUNT(*) AS PageViews FROM hits WHERE CounterID = 62 "
    "AND EventDate >= ? AND EventDate < ? + interval ? day "
    "AND DontCountHits = 0 AND IsRefresh = 0 AND URL <> '' "
    "GROUP BY URL ORDER BY PageViews DESC LIMIT ?;"
)
Q7_SPECS = [
    P("DATE_FROM", "DATE"),
    P("DATE_FROM", "DATE"),
    P("WINDOW_DAYS", "INTEGER"),
    P("TOPK", "INTEGER"),
]


def _cast_sequence(sql: str) -> List[str]:
    """The CAST(NULL AS <type>) types in *sql*, left to right."""
    return _CAST_NULL_RE.findall(sql)


def test_q7_template_positional_types():
    # The regression template verbatim: the k-th textual ? must get the k-th
    # spec's type - DATE, DATE, INT, INT - not the AST visit order's rotation.
    out = _literalize(Q7_TEMPLATE, Q7_SPECS)
    assert _cast_sequence(out) == ["DATE", "DATE", "INT", "INT"]
    assert "EventDate >= CAST(NULL AS DATE)" in out
    assert "LIMIT CAST(NULL AS INT)" in out
    assert "?" not in out


def test_q7_template_describes_against_duckdb():
    # The end-to-end symptom: describe_output must bind the literalized Q7
    # instead of raising a BinderException on LIMIT CAST(NULL AS DATE).
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE hits (URL VARCHAR, CounterID INTEGER, EventDate DATE, "
            "DontCountHits SMALLINT, IsRefresh SMALLINT)"
        )
        schema = describe_output(con, Q7_TEMPLATE, Q7_SPECS)
    finally:
        con.close()
    assert [c.name for c in schema] == ["URL", "PageViews"]


def test_limit_offset_positions():
    out = _literalize(
        "SELECT id FROM t WHERE d >= ? ORDER BY id LIMIT ? OFFSET ?",
        [P("d0", "DATE"), P("lim", "INT"), P("off", "BIGINT")],
    )
    assert _cast_sequence(out) == ["DATE", "INT", "BIGINT"]
    assert "LIMIT CAST(NULL AS INT)" in out
    assert "OFFSET CAST(NULL AS BIGINT)" in out


def test_between_positions():
    out = _literalize(
        "SELECT id FROM t WHERE id BETWEEN ? AND ? AND d BETWEEN ? AND ?",
        [P("lo", "INT"), P("hi", "BIGINT"), P("d0", "DATE"), P("d1", "TIMESTAMP")],
    )
    assert _cast_sequence(out) == ["INT", "BIGINT", "DATE", "TIMESTAMP"]


def test_in_list_positions():
    out = _literalize(
        "SELECT id FROM t WHERE id IN (?, ?, ?) AND name = ?",
        [P("a", "INT"), P("b", "BIGINT"), P("c", "SMALLINT"), P("nm", "VARCHAR")],
    )
    assert _cast_sequence(out) == ["INT", "BIGINT", "SMALLINT", "TEXT"]


def test_named_parameters_mix():
    # Anonymous ?s consume binding groups by textual ? index; named parameters
    # resolve by name, with VARCHAR for a name the specs do not declare.
    out = _literalize(
        "SELECT id FROM t WHERE id > ? AND name = $nm AND d >= $d0 "
        "AND tag = $undeclared LIMIT ?",
        [P("min_id", "BIGINT"), P("lim", "INT"), P("nm", "VARCHAR"), P("d0", "DATE")],
    )
    assert _cast_sequence(out) == ["BIGINT", "TEXT", "DATE", "TEXT", "INT"]
    assert "LIMIT CAST(NULL AS INT)" in out
    assert "$" not in out


def test_more_placeholders_than_specs_fall_back_to_varchar():
    out = _literalize(
        "SELECT id FROM t WHERE id > ? AND name = ? LIMIT ?",
        [P("min_id", "INT")],
    )
    assert _cast_sequence(out) == ["INT", "TEXT", "TEXT"]


def test_unparseable_template_returned_unchanged():
    # Tokenizes but does not parse: the post-splice validation gate refuses it.
    bad_parse = "SELECT ? FROM ("
    assert _literalize(bad_parse, [P("p0", "INT")]) == bad_parse
    # Does not even tokenize (unterminated string literal).
    bad_tokenize = "SELECT 'unterminated ?"
    assert _literalize(bad_tokenize, [P("p0", "INT")]) == bad_tokenize
