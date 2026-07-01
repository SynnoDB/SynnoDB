"""Structural placeholder binding for the router (normalize.unify_and_bind).

These are the "try to break it" tests: a real user's *inline* query (literals written
out, e.g. ``... interval 90 day``) must bind to the engine's placeholders correctly even
though the template also contains constants — and must NEVER mis-bind, so the bespoke
engine is only used for a genuinely-matching query. Covers TPC-H Q1 (a placeholder amid
constants) and Q6 (repeated [DATE]/[DISCOUNT]), plus a large adversarial matrix.
"""

from __future__ import annotations

import pytest

from synnodb.router.normalize import has_param_markers, unify_and_bind


def _norm(d):
    """Compare values via str so Decimal('0.05') == 0.05 etc. (the engine stringifies)."""
    return None if d is None else {k: str(v) for k, v in d.items()}


# real TPC-H shapes ------------------------------------------------------------
Q1_T = (
    "select sum(l_extendedprice*(1-l_discount)) a from lineitem "
    "where l_shipdate <= date '1998-12-01' - interval (?) day"
)
Q1_I = (
    "select sum(l_extendedprice*(1-l_discount)) a from lineitem "
    "where l_shipdate <= date '1998-12-01' - interval 90 day"
)
Q6_T = (
    "select sum(l_extendedprice*l_discount) r from lineitem where l_shipdate >= ? "
    "and l_shipdate < ? + interval '1' year and l_discount between ? - 0.01 and ? + 0.01 "
    "and l_quantity < ?"
)
Q6_I = (
    "select sum(l_extendedprice*l_discount) r from lineitem where l_shipdate >= date '1994-01-01' "
    "and l_shipdate < date '1994-01-01' + interval '1' year "
    "and l_discount between 0.05 - 0.01 and 0.05 + 0.01 and l_quantity < 24"
)
Q6_NAMES = ["DATE", "DATE", "DISCOUNT", "DISCOUNT", "QUANTITY"]
Q6_NAMED_T = (
    "select sum(l_extendedprice*l_discount) r from lineitem where l_shipdate >= $DATE "
    "and l_shipdate < $DATE + interval '1' year and l_discount between $DISC - 0.01 and $DISC + 0.01 "
    "and l_quantity < $QTY"
)


@pytest.mark.parametrize(
    "label,template,incoming,names,expect",
    [
        # --- the bug that started this: DELTA must bind to 90, not the constants ---
        ("q1 paren", Q1_T, Q1_I, ["DELTA"], {"DELTA": 90}),
        ("q1 no-paren", Q1_T.replace("(?)", "?"), Q1_I, ["DELTA"], {"DELTA": 90}),
        (
            "q1 wrong const date",
            Q1_T,
            Q1_I.replace("1998-12-01", "1997-01-01"),
            ["DELTA"],
            None,
        ),
        # --- Q6 repeated placeholders, ? form and named form ---
        (
            "q6 ?",
            Q6_T,
            Q6_I,
            Q6_NAMES,
            {"DATE": "1994-01-01", "DISCOUNT": 0.05, "QUANTITY": 24},
        ),
        (
            "q6 named",
            Q6_NAMED_T,
            Q6_I,
            [],
            {"DATE": "1994-01-01", "DISC": 0.05, "QTY": 24},
        ),
        (
            "q6 repeated DATE differs",
            Q6_T,
            Q6_I.replace("< date '1994-01-01'", "< date '1995-06-06'"),
            Q6_NAMES,
            None,
        ),
        (
            "q6 named repeated differs",
            Q6_NAMED_T,
            Q6_I.replace("< date '1994-01-01'", "< date '1995-06-06'"),
            [],
            None,
        ),
        ("q6 const 0.02", Q6_T, Q6_I.replace("- 0.01", "- 0.02"), Q6_NAMES, None),
        ("q6 const 2 year", Q6_T, Q6_I.replace("'1' year", "'2' year"), Q6_NAMES, None),
        # --- ordering: placeholders bound by source order, not AST order ---
        (
            "limit param swap-safe",
            "select * from t where a=? limit ?",
            "select * from t where a=5 limit 20",
            ["A", "L"],
            {"A": 5, "L": 20},
        ),
        (
            "offset+limit",
            "select * from t where a=? limit ? offset ?",
            "select * from t where a=5 limit 20 offset 100",
            ["A", "L", "O"],
            {"A": 5, "L": 20, "O": 100},
        ),
        # --- values: signs, strings, escapes ---
        (
            "negative int",
            "select * from t where x>?",
            "select * from t where x>-5",
            ["X"],
            {"X": -5},
        ),
        (
            "negative dec",
            "select * from t where x>?",
            "select * from t where x>-3.5",
            ["X"],
            {"X": -3.5},
        ),
        (
            "string escape",
            "select * from t where s=?",
            "select * from t where s='O''Brien'",
            ["S"],
            {"S": "O'Brien"},
        ),
        (
            "empty string",
            "select * from t where s=?",
            "select * from t where s=''",
            ["S"],
            {"S": ""},
        ),
        (
            "qmark inside string",
            "select * from t where s='a?b' and x=?",
            "select * from t where s='a?b' and x=7",
            ["X"],
            {"X": 7},
        ),
        # --- constants are type-sensitive: 1 != '1' ---
        (
            "type at const",
            "select * from t where a=1 and b=?",
            "select * from t where a='1' and b=2",
            ["B"],
            None,
        ),
        (
            "float vs int const",
            "select * from t where a=1.0 and b=?",
            "select * from t where a=1 and b=2",
            ["B"],
            None,
        ),
        (
            "bool const ok",
            "select * from t where flag=true and b=?",
            "select * from t where flag=true and b=2",
            ["B"],
            {"B": 2},
        ),
        (
            "bool const differs",
            "select * from t where flag=true and b=?",
            "select * from t where flag=false and b=2",
            ["B"],
            None,
        ),
        # --- structural safety: only a genuine match binds ---
        (
            "diff table",
            "select * from a where x=?",
            "select * from b where x=1",
            ["X"],
            None,
        ),
        (
            "diff column",
            "select * from t where a=?",
            "select * from t where z=1",
            ["A"],
            None,
        ),
        (
            "diff operator",
            "select * from t where a<=?",
            "select * from t where a>=5",
            ["A"],
            None,
        ),
        (
            "diff agg",
            "select sum(a) from t where a=?",
            "select avg(a) from t where a=5",
            ["A"],
            None,
        ),
        (
            "diff alias",
            "select a as x from t where a=?",
            "select a as y from t where a=5",
            ["A"],
            None,
        ),
        (
            "reordered predicates",
            "select * from t where a=? and b=?",
            "select * from t where b=2 and a=1",
            ["A", "B"],
            None,
        ),
        (
            "operand side swap",
            "select * from t where a=?",
            "select * from t where 5=a",
            ["A"],
            None,
        ),
        (
            "extra predicate",
            "select * from t where a=?",
            "select * from t where a=5 and c=1",
            ["A"],
            None,
        ),
        (
            "missing predicate",
            "select * from t where a=? and b=?",
            "select * from t where a=5",
            ["A", "B"],
            None,
        ),
        (
            "order desc differs",
            "select a from t where b=? order by a",
            "select a from t where b=7 order by a desc",
            ["B"],
            None,
        ),
        (
            "limit value differs",
            "select * from t where a=? limit 10",
            "select * from t where a=5 limit 20",
            ["A"],
            None,
        ),
        # --- arity ---
        (
            "too few names",
            "select * from t where a=? and b=?",
            "select * from t where a=1 and b=2",
            ["A"],
            None,
        ),
        (
            "too many names",
            "select * from t where a=?",
            "select * from t where a=1",
            ["A", "B"],
            None,
        ),
        # --- placeholders in interesting positions ---
        (
            "func arg",
            "select * from t where substr(s,1,?)='x'",
            "select * from t where substr(s,1,3)='x'",
            ["N"],
            {"N": 3},
        ),
        (
            "like pattern",
            "select * from t where s like ?",
            "select * from t where s like 'A%'",
            ["P"],
            {"P": "A%"},
        ),
        (
            "between distinct",
            "select * from t where a between ? and ?",
            "select * from t where a between 1 and 10",
            ["LO", "HI"],
            {"LO": 1, "HI": 10},
        ),
        (
            "subquery param",
            "select * from t where a in (select x from u where y=?)",
            "select * from t where a in (select x from u where y=9)",
            ["Y"],
            {"Y": 9},
        ),
        (
            "case param",
            "select sum(case when a>? then 1 else 0 end) from t",
            "select sum(case when a>5 then 1 else 0 end) from t",
            ["A"],
            {"A": 5},
        ),
        # --- in-list cannot bind a multi-element list to a scalar placeholder ---
        (
            "in list scalar",
            "select * from t where x in (?)",
            "select * from t where x in (1,2,3)",
            ["X"],
            None,
        ),
        # --- whitespace / case are normalized away by parsing ---
        (
            "whitespace+case",
            "select * from t where a = ?",
            "SELECT  *  FROM t WHERE a=7",
            ["A"],
            {"A": 7},
        ),
    ],
)
def test_unify(label, template, incoming, names, expect):
    assert _norm(unify_and_bind(template, incoming, names)) == _norm(expect), label


def test_router_inline_binding_end_to_end():
    """The whole router path on a `?` template with a constant AND a repeated placeholder:
    an inline query routes with the right params; a near-miss (repeated placeholder with
    two different values) falls back without calling the engine."""
    import pyarrow as pa

    import synnodb
    from synnodb.router import (
        LocalCallableEngine,
        PlaceholderSpec,
        RouterMode,
        RouterPolicy,
        TemplateRegistry,
        register_engine,
    )

    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(d DATE, x INTEGER)")
    con.duckdb.execute(
        "INSERT INTO t VALUES (DATE '2020-03-01', 5), (DATE '2020-09-01', 50)"
    )

    seen: dict = {}

    def fn(ph):
        seen.clear()
        seen.update(ph)
        return pa.table(
            {"n": pa.array([1], pa.int64())}
        )  # the true count for the query below

    # template: repeated $D (start date, used twice) + a constant interval + X
    T = "select count(*) as n from t where d >= ? and d < ? + interval '1' year and x < ?"
    register_engine(
        con,
        template_sql=T,
        engine=LocalCallableEngine("e", {"1": fn}),
        query_id="1",
        placeholders=[
            PlaceholderSpec("D", "DATE"),
            PlaceholderSpec("D", "DATE"),
            PlaceholderSpec("X", "INTEGER"),
        ],
    )

    # inline query: repeated date is consistent -> routes, engine gets {D, X}
    good = (
        "select count(*) as n from t where d >= date '2020-01-01' "
        "and d < date '2020-01-01' + interval '1' year and x < 10"
    )
    dec = con.router.route(good, None, con)
    assert dec.routed is True
    assert dec.trace.results_match is True
    assert seen == {"D": "2020-01-01", "X": 10}

    # near-miss: the two dates differ -> cannot be one placeholder -> fall back, no engine call
    seen.clear()
    bad = (
        "select count(*) as n from t where d >= date '2020-01-01' "
        "and d < date '2021-07-07' + interval '1' year and x < 10"
    )
    dec2 = con.router.route(bad, None, con)
    assert dec2.routed is False
    assert seen == {}  # engine was never called for a non-matching query


def test_has_param_markers():
    assert has_param_markers("select * from t where a = ?")
    assert has_param_markers("select * from t where a = $DATE")
    assert not has_param_markers("select * from t where a = 2")  # concrete example
    assert not has_param_markers("select count(*) from t")
