"""Structural keys and binding for the parameterized-tutorial query shapes: negated
literals (clickbench), variable-arity IN lists (musicbrainz/stack), postfix-cast markers
(stack), and bracketed data literals that only look like placeholders (ceb/IMDB).
"""

from __future__ import annotations

from synnodb.router.normalize import (
    constant_in_arities,
    normalize_sql,
    unify_and_bind,
)
from synnodb.router.registry import EngineBinding, PlaceholderSpec, TemplateRegistry
from synnodb.router.router import QueryRouter
from synnodb.workloads.engine_publish import build_query_templates, derive_template
from synnodb.workloads.query_params import (
    find_placeholders,
    is_placeholder_name,
    substitute,
)

IN_FIXED_T = "select ts, count(*) c from hits where ts in (?, ?, ?) group by ts"
IN_FIXED_I = "select ts, count(*) c from hits where ts in (10, -1, 1) group by ts"


# --- negated literals ---------------------------------------------------------


def test_negated_literal_shares_key():
    assert normalize_sql(IN_FIXED_I) == normalize_sql(IN_FIXED_T)
    assert normalize_sql("select a from t where a = -5") == normalize_sql(
        "select a from t where a = ?"
    )


def test_negated_literal_binds():
    bound = unify_and_bind(IN_FIXED_T, IN_FIXED_I, ["TS1", "TS2", "TS3"])
    assert bound == {"TS1": 10, "TS2": -1, "TS3": 1}


def test_binary_minus_stays_structural():
    # 3 - 1 is exp.Sub: two constants around a minus, never one collapsed constant.
    key = normalize_sql("select a from t where a = 3 - 1")
    assert key == normalize_sql("select a from t where a = ? - ?")
    assert key != normalize_sql("select a from t where a = ?")


def test_negative_in_list_derives():
    # The clickbench Q6 shape: a sampled domain containing a negative id.
    q = "select ts, count(*) c from hits where ts in ([T1], [T2], [T3]) group by ts"
    derived = derive_template(q, [{"T1": "10", "T2": "-1", "T3": "1"}])
    assert derived is not None
    marker, specs = derived
    assert marker.count("?") == 3
    assert [s.name for s in specs] == ["T1", "T2", "T3"]


# --- variable-arity IN lists ----------------------------------------------------


def test_in_list_key_is_arity_free():
    keys = {
        normalize_sql(s)
        for s in (
            "select x from t where name in ?",
            "select x from t where name in (?)",
            "select x from t where name in (?, ?, ?)",
            "select x from t where name in ('a', 'b', 'c')",
            "select x from t where name in (1)",
        )
    }
    assert len(keys) == 1


def test_in_subquery_untouched():
    key = normalize_sql("select x from t where a in (select b from u)")
    assert "IN (SELECT" in key
    assert key != normalize_sql("select x from t where a in ('a', 'b')")


def test_non_constant_member_untouched():
    # A column member keeps the list structural on both sides.
    assert normalize_sql("select x from t where a in (b, 2)") == normalize_sql(
        "select x from t where a in (b, ?)"
    )
    assert (
        unify_and_bind(
            "select x from t where a in ?",
            "select x from t where a in (b, 2)",
            ["L"],
        )
        is None
    )


def test_whole_list_marker_binds_any_arity():
    t = "select x from t where name in ?"
    assert unify_and_bind(
        t, "select x from t where name in ('a', 'b', 'c')", ["L"]
    ) == {"L": ("a", "b", "c")}
    assert unify_and_bind(t, "select x from t where name in ('a')", ["L"]) == {
        "L": ("a",)
    }
    assert unify_and_bind(t, "select x from t where name in (1, -2)", ["L"]) == {
        "L": (1, -2)
    }


def test_not_in_follows():
    t = "select x from t where name not in ?"
    i = "select x from t where name not in ('a', 'b')"
    assert normalize_sql(t) == normalize_sql(i)
    assert unify_and_bind(t, i, ["L"]) == {"L": ("a", "b")}


def test_fixed_arity_list_keeps_positional_matching():
    # `in (?)` is a one-element list, not a whole-list marker: it binds a lone scalar
    # and refuses a longer incoming list.
    assert unify_and_bind(
        "select * from t where x in (?)", "select * from t where x in (7)", ["X"]
    ) == {"X": 7}
    assert (
        unify_and_bind(
            "select * from t where x in (?)",
            "select * from t where x in (1, 2, 3)",
            ["X"],
        )
        is None
    )
    assert (
        unify_and_bind(IN_FIXED_T, IN_FIXED_I.replace("10, ", ""), ["A", "B", "C"])
        is None
    )


def test_in_collapse_keeps_walking_the_lhs():
    # The collapse must not stop literal replacement in the membership's left-hand side.
    assert normalize_sql(
        "select 1 from t where substr(x, 3, 2) in ('a', 'b')"
    ) == normalize_sql("select 1 from t where substr(x, 9, 2) in ('c')")


def test_whole_list_marker_template_validates():
    # The musicbrainz/stack shape: the generator hands back a pre-rendered list, with
    # element spacing varying by source, at whatever arity it drew (including 1).
    q = "select x from t where name in [TAGS] and score >= [MIN]"
    assignments = [
        {"TAGS": "('a', 'b', 'c')", "MIN": "3"},
        {"TAGS": "('d','e')", "MIN": "5"},
        {"TAGS": "('z')", "MIN": "1"},
    ]
    derived = derive_template(q, assignments)
    assert derived is not None
    marker, specs = derived
    assert "in ?" in marker
    assert [s.name for s in specs] == ["TAGS", "MIN"]


# --- postfix-cast markers -------------------------------------------------------


STACK7 = (
    "select count(*) from badge b1, badge b2 where b1.name = '[NAME]' "
    "and b2.date > b1.date + '[DATE]'::interval"
)


def test_postfix_cast_marker_derives_and_binds():
    derived = derive_template(STACK7, [{"NAME": "Epic", "DATE": "7 months"}])
    assert derived is not None
    marker, specs = derived
    assert "CAST(? AS interval)" in marker
    assert [s.name for s in specs] == ["NAME", "DATE"]
    concrete = substitute(STACK7, {"NAME": "Epic", "DATE": "7 months"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    assert unify_and_bind(marker, concrete, ["NAME", "DATE"]) == {
        "NAME": "Epic",
        "DATE": "7 months",
    }


# --- bracketed data literals ------------------------------------------------------


CEB3A = (
    "select count(*) from company_name cn "
    "where cn.country_code in ('[cz]','[sk]') and cn.kind = 'production'"
)


def test_bracketed_data_literal_is_not_a_placeholder():
    # Lowercase bracketed tokens are data (IMDB country codes), not markers.
    assert find_placeholders(CEB3A) == []
    assert not is_placeholder_name("cz")
    assert is_placeholder_name("TAG_LIST")


def test_data_literal_query_ships_as_constant_template():
    templates = build_query_templates({"3a": CEB3A}, {})
    assert [t.query_id for t in templates] == ["3a"]
    assert templates[0].sql_template == CEB3A
    assert templates[0].placeholders == ()


def test_data_literal_and_marker_coexist():
    q = (
        "select count(*) from company_name cn "
        "where cn.country_code in ('[cz]','[sk]') and cn.kind = '[KIND]'"
    )
    derived = derive_template(q, [{"KIND": "production"}])
    assert derived is not None
    marker, specs = derived
    assert "'[cz]'" in marker
    assert [s.name for s in specs] == ["KIND"]


def test_constant_in_arities_signature():
    sig = constant_in_arities("select * from t where a in (1, 2) and b in (3, 4)")
    assert sig == (2, 2)
    # A non-constant member keeps that list out of the signature.
    assert constant_in_arities("select * from t where a in (x, 2)") == ()
    assert constant_in_arities("not sql at all (") is None


def test_positional_bind_refuses_compensating_in_arities():
    tmpl = "select * from t where a in (1, 2) and b in (3, 4)"
    inc = "select * from t where a in (9) and b in (7, 8, 6)"
    assert normalize_sql(tmpl) == normalize_sql(inc)

    binding = EngineBinding(
        template_id="tid",
        engine_id="eid",
        query_id="1",
        template_sql=tmpl,
        normalized_sql=normalize_sql(tmpl),
        placeholders=tuple(PlaceholderSpec(n, "INT") for n in ("A1", "A2", "B1", "B2")),
        tables=frozenset({"t"}),
        schema_fingerprint="fp",
        output_schema=(),
        engine=object(),
    )
    router = QueryRouter(registry=TemplateRegistry())
    assert router._bind_placeholders(binding, inc, None) is None
    same_shape = "select * from t where a in (9, 5) and b in (7, 8)"
    bound = router._bind_placeholders(binding, same_shape, None)
    assert bound == {"A1": 9, "A2": 5, "B1": 7, "B2": 8}


def test_whole_list_in_refuses_null_members():
    tmpl = "select x from t where a in ?"
    assert unify_and_bind(tmpl, "select x from t where a in (1, NULL)", ["L"]) is None
    assert unify_and_bind(tmpl, "select x from t where a in (1, TRUE)", ["L"]) is None
    bound = unify_and_bind(tmpl, "select x from t where a in (1, 2)", ["L"])
    assert bound == {"L": (1, 2)}
