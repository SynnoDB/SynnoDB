"""Adversarial tests pinning ``_literalize``'s positional typing contract.

``_literalize`` replaces each anonymous ``?`` in a template with a typed
``CAST(NULL AS <type>)`` so DuckDB can describe the template's output schema
without values. The contract: placeholder specs are declared in TEXTUAL order,
so the i-th ``?`` (reading the SQL left to right, ignoring ``?`` characters
inside string literals) must receive the type of the i-th binding group of
``binding_groups(placeholders)``.

Any traversal-order-dependent assignment breaks that contract: sqlglot's
``transform`` visits LIMIT/OFFSET before WHERE, so a visit-order counter gives
``LIMIT ?`` the WHERE parameter's type and vice versa (the ClickBench Q7
regression: ``LIMIT CAST(NULL AS DATE)`` raised a BinderException and silently
prevented engine registration). Every case here uses heterogeneous types per
position so any misordering changes the output detectably.

The oracle, applied to every case:

* scan the literalized SQL left to right for ``CAST(NULL AS <type>)`` and
  require the type sequence to equal the spec-derived group types exactly
  (group type per the production rule: a group is VARCHAR unless it is a
  single spec with no prefix/suffix, in which case it keeps its own type);
* no un-substituted ``?`` may remain outside string literals;
* the output must parse as DuckDB SQL;
* DuckDB must describe it against a tiny in-memory schema through the real
  ``describe_output`` (the ``SELECT * FROM (<sql>) LIMIT 0`` production path).
"""

from __future__ import annotations

import re
from typing import List, Sequence

import duckdb
import pytest
import sqlglot
from sqlglot import expressions as exp

from synnodb.router.normalize import binding_groups
from synnodb.router.registration import _literalize, describe_output
from synnodb.router.registry import PlaceholderSpec

# One CAST(NULL AS <type>) occurrence; the inner group captures the type,
# including a single parenthesized argument list such as DECIMAL(12, 2).
_CAST_NULL_RE = re.compile(
    r"CAST\s*\(\s*NULL\s+AS\s+([A-Za-z][A-Za-z0-9_ ]*(?:\([^()]*\))?)\s*\)",
    re.IGNORECASE,
)

# A single-quoted SQL string literal, with '' as the escaped quote.
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _canonical(type_str: str) -> str:
    """Canonical DuckDB rendering of a type, so spellings compare equal.

    The implementation may render types through sqlglot (VARCHAR becomes TEXT,
    DECIMAL(12,2) becomes DECIMAL(12, 2)) or splice the spec's own spelling;
    both must count as the same type.
    """
    return (
        exp.DataType.build(type_str.strip(), dialect="duckdb")
        .sql(dialect="duckdb")
        .upper()
    )


def _expected_group_types(specs: Sequence[PlaceholderSpec]) -> List[str]:
    """The type each textual ``?`` must get, one per binding group, in order.

    Mirrors the production rule: a group binds a whole SQL literal of the
    spec's own type only when it is a single spec with no prefix/suffix;
    otherwise the ``?`` stands for a composed string literal, hence VARCHAR.
    """
    out: List[str] = []
    for group in binding_groups(list(specs)):
        whole_literal = len(group) == 1 and not group[0].prefix and not group[0].suffix
        out.append(group[0].type if whole_literal else "VARCHAR")
    return out


def _strip_strings(sql: str) -> str:
    """Blank out string literals so ``?`` scans see only real placeholders."""
    return _STRING_LITERAL_RE.sub("''", sql)


def _cast_sequence(sql: str) -> List[str]:
    """The CAST(NULL AS <type>) types in *sql*, left to right, canonicalized."""
    return [_canonical(t) for t in _CAST_NULL_RE.findall(sql)]


P = PlaceholderSpec

# (case id, template, specs in textual order). Types are interleaved so that
# any positional shuffle changes the CAST sequence detectably.
CASES = [
    (
        "limit_offset",
        "SELECT id, name FROM t WHERE d >= ? ORDER BY id LIMIT ? OFFSET ?",
        [P("p0", "DATE"), P("p1", "INT"), P("p2", "BIGINT")],
    ),
    (
        # The ClickBench Q7 regression shape: sqlglot visits LIMIT before WHERE,
        # and the INTERVAL quantity sits inside arithmetic on the WHERE side.
        "q7_interval_arith",
        "SELECT id FROM t WHERE d >= ? AND d < ? + INTERVAL (?) DAY LIMIT ?",
        [P("p0", "DATE"), P("p1", "DATE"), P("p2", "INT"), P("p3", "INT")],
    ),
    (
        "interval_minus",
        "SELECT id FROM t WHERE ts > ? - INTERVAL (?) DAY AND id = ?",
        [P("p0", "TIMESTAMP"), P("p1", "INT"), P("p2", "BIGINT")],
    ),
    (
        "order_by_param",
        "SELECT id FROM t WHERE name = ? ORDER BY ?",
        [P("p0", "VARCHAR"), P("p1", "INT")],
    ),
    (
        "having_param",
        "SELECT id, COUNT(*) AS c FROM t WHERE d > ? GROUP BY id HAVING COUNT(*) > ? ORDER BY c LIMIT ?",
        [P("p0", "DATE"), P("p1", "BIGINT"), P("p2", "INT")],
    ),
    (
        "between",
        "SELECT id FROM t WHERE id BETWEEN ? AND ? AND d BETWEEN ? AND ?",
        [P("p0", "INT"), P("p1", "BIGINT"), P("p2", "DATE"), P("p3", "TIMESTAMP")],
    ),
    (
        "in_list",
        "SELECT id FROM t WHERE id IN (?, ?, ?) AND name = ?",
        [P("p0", "INT"), P("p1", "BIGINT"), P("p2", "SMALLINT"), P("p3", "VARCHAR")],
    ),
    (
        "scalar_subquery_and_where",
        "SELECT id, (SELECT MAX(u.id) FROM u WHERE u.label = ?) AS m FROM t WHERE t.d > ? LIMIT ?",
        [P("p0", "VARCHAR"), P("p1", "DATE"), P("p2", "INT")],
    ),
    (
        "cte_body_and_outer",
        "WITH recent AS (SELECT id, name FROM t WHERE d >= ?) SELECT r.id FROM recent AS r WHERE r.name = ? LIMIT ?",
        [P("p0", "DATE"), P("p1", "VARCHAR"), P("p2", "INT")],
    ),
    (
        "case_when_then_else",
        "SELECT CASE WHEN id > ? THEN ? ELSE ? END AS v FROM t",
        [P("p0", "BIGINT"), P("p1", "DECIMAL(12,2)"), P("p2", "DOUBLE")],
    ),
    (
        "window_frame",
        "SELECT id, SUM(val) OVER (ORDER BY id ROWS BETWEEN ? PRECEDING AND ? FOLLOWING) AS s FROM t WHERE flag = ?",
        [P("p0", "INT"), P("p1", "BIGINT"), P("p2", "BOOLEAN")],
    ),
    (
        "union_branches",
        "SELECT id FROM t WHERE d = ? UNION ALL SELECT id FROM u WHERE label = ? ORDER BY 1 LIMIT ?",
        [P("p0", "DATE"), P("p1", "VARCHAR"), P("p2", "INT")],
    ),
    (
        "join_condition",
        "SELECT t.id FROM t JOIN u ON t.id = u.id AND u.label = ? WHERE t.d > ? LIMIT ?",
        [P("p0", "VARCHAR"), P("p1", "DATE"), P("p2", "INT")],
    ),
    (
        # Literal '?' characters inside strings, before, between, and after the
        # real placeholders. None of them is a parameter.
        "question_mark_in_strings",
        "SELECT '?' AS lead, name || ' ?' AS q, id FROM t WHERE name = ? AND id > ? AND name <> 'trailing?'",
        [P("p0", "VARCHAR"), P("p1", "BIGINT")],
    ),
    (
        # TPC-H Q13 mirror: two specs packed into ONE string literal share one
        # ``?`` (group 0, typed VARCHAR); a single affixed spec (prefix "id-")
        # is also VARCHAR by the whole-literal rule even though its own type is
        # INT; the trailing plain specs keep their own types.
        "q13_packed_group",
        "SELECT id FROM t WHERE name NOT LIKE ? AND name LIKE ? AND id > ? LIMIT ?",
        [
            P("word1", "VARCHAR", prefix="%", suffix="%", group=0),
            P("word2", "VARCHAR", prefix="", suffix="%", group=0),
            P("idpat", "INT", prefix="id-", suffix=""),
            P("min_id", "BIGINT"),
            P("lim", "INT"),
        ],
    ),
    (
        "heterogeneous_kitchen_sink",
        "SELECT id FROM t WHERE d > ? AND name = ? AND amount < ? AND id IN (?, ?) AND ts < ? ORDER BY ? LIMIT ? OFFSET ?",
        [
            P("p0", "DATE"),
            P("p1", "VARCHAR"),
            P("p2", "DECIMAL(12,2)"),
            P("p3", "INT"),
            P("p4", "BIGINT"),
            P("p5", "TIMESTAMP"),
            P("p6", "INT"),
            P("p7", "SMALLINT"),
            P("p8", "BIGINT"),
        ],
    ),
]

CASE_IDS = [c[0] for c in CASES]
CASE_PARAMS = [(c[1], c[2]) for c in CASES]


@pytest.fixture(scope="module")
def duck():
    """Empty in-memory schema with every column type the templates touch."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (id INTEGER, name VARCHAR, d DATE, amount DECIMAL(12,2), "
        "big BIGINT, ts TIMESTAMP, val DOUBLE, flag BOOLEAN)"
    )
    con.execute("CREATE TABLE u (id INTEGER, label VARCHAR)")
    yield con
    con.close()


@pytest.mark.parametrize(("template", "specs"), CASE_PARAMS, ids=CASE_IDS)
def test_cast_sequence_follows_textual_order(template, specs):
    """The i-th textual ``?`` gets the i-th binding group's type, exactly."""
    out = _literalize(template, specs)
    got = _cast_sequence(out)
    want = [_canonical(t) for t in _expected_group_types(specs)]
    assert got == want, (
        f"typed-NULL sequence does not follow textual placeholder order\n"
        f"  template: {template}\n"
        f"  expected: {want}\n"
        f"  got:      {got}\n"
        f"  output:   {out}"
    )
    assert "?" not in _strip_strings(out), (
        f"un-substituted placeholder left in literalized SQL: {out}"
    )


@pytest.mark.parametrize(("template", "specs"), CASE_PARAMS, ids=CASE_IDS)
def test_literalized_output_parses(template, specs):
    """The literalized SQL is still valid DuckDB syntax."""
    out = _literalize(template, specs)
    tree = sqlglot.parse_one(out, read="duckdb")
    assert tree is not None


@pytest.mark.parametrize(("template", "specs"), CASE_PARAMS, ids=CASE_IDS)
def test_duckdb_describes_literalized_template(duck, template, specs):
    """DuckDB binds the literalized template through the production describe path.

    This is the end-to-end symptom of the Q7 regression: a misplaced type makes
    DuckDB raise a BinderException (e.g. LIMIT CAST(NULL AS DATE)) and engine
    registration silently fails.
    """
    schema = describe_output(duck, template, specs)
    assert len(schema) > 0
    assert all(col.name and col.type for col in schema)


def test_question_marks_inside_strings_are_not_placeholders():
    """String-literal '?' characters survive untouched; only real ``?`` bind."""
    template, specs = next(
        (c[1], c[2]) for c in CASES if c[0] == "question_mark_in_strings"
    )
    out = _literalize(template, specs)
    assert len(_cast_sequence(out)) == 2
    for literal in ("'?'", "' ?'", "'trailing?'"):
        assert literal in out, f"string literal {literal} was altered: {out}"


def test_q13_packed_group_counts_as_one_placeholder():
    """Multiple specs sharing a group id consume a single ``?`` (as VARCHAR)."""
    template, specs = next((c[1], c[2]) for c in CASES if c[0] == "q13_packed_group")
    groups = binding_groups(list(specs))
    assert len(specs) == 5 and len(groups) == 4  # word1+word2 share one group
    out = _literalize(template, specs)
    want = [_canonical(t) for t in ["VARCHAR", "VARCHAR", "BIGINT", "INT"]]
    assert _cast_sequence(out) == want
