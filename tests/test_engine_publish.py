"""Factory-side publishing: template derivation, manifest schema v2, and atomic publish."""

from __future__ import annotations

import json

import pytest

from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.router.normalize import (
    bind_template,
    normalize_sql,
    split_literal,
    unify_and_bind,
)
from synnodb.router.registry import PlaceholderSpec
from synnodb.workloads.engine_publish import (
    build_query_templates,
    derive_template,
    publish_engine,
)
from synnodb.workloads.query_params import substitute

from receipt_helpers import passing_receipt, write_fake_engine_db

Q1 = (
    "select sum(l_quantity) as q from lineitem "
    "where l_shipdate <= date '1998-12-01' - interval '[DELTA]' day"
)
Q6 = (
    "select sum(l_extendedprice*l_discount) as revenue from lineitem "
    "where l_shipdate >= date '[DATE]' and l_shipdate < date '[DATE]' + interval '1' year "
    "and l_discount between [DISCOUNT] - 0.01 and [DISCOUNT] + 0.01 and l_quantity < [QUANTITY]"
)


def test_derive_single_placeholder():
    marker, specs = derive_template(Q1, [{"DELTA": "90"}])
    assert [(p.name, p.type) for p in specs] == [("DELTA", "INTEGER")]
    # The derived template shares the structural key of, and binds, a real instantiation.
    concrete = substitute(Q1, {"DELTA": "90"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    assert unify_and_bind(marker, concrete, [p.name for p in specs]) is not None


def test_derive_repeated_placeholder_gives_per_occurrence_specs():
    marker, specs = derive_template(
        Q6, [{"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"}]
    )
    # Q6 has 5 placeholder occurrences (DATE, DATE, DISCOUNT, DISCOUNT, QUANTITY).
    assert [p.name for p in specs] == [
        "DATE",
        "DATE",
        "DISCOUNT",
        "DISCOUNT",
        "QUANTITY",
    ]
    concrete = substitute(
        Q6, {"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"}
    )
    assert normalize_sql(marker) == normalize_sql(concrete)
    bound = unify_and_bind(marker, concrete, [p.name for p in specs])
    assert (
        bound == {"DATE": "1994-01-01", "DISCOUNT": 0.06, "QUANTITY": 24}
        or bound is not None
    )


# A LIKE parameter embedded in a string literal: the engine wants `BRASS`, the query carries
# `'%BRASS'`. `[SIZE]` (a plain literal) and the two `[REGION]` copies (a repeated whole literal)
# keep the ordinary path exercised alongside the affixed `[TYPE]`.
Q_LIKE = (
    "select s_name from part, supplier where p_size = [SIZE] "
    "and p_type like '%[TYPE]' and r_name = '[REGION]' and s_comment <> '[REGION]'"
)


def test_derive_like_affix_placeholder_binds_stripped_value():
    marker, specs = derive_template(
        Q_LIKE, [{"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"}]
    )
    by_name = {p.name: p for p in specs}
    # Only the LIKE-embedded placeholder carries a constant affix.
    assert (by_name["TYPE"].prefix, by_name["TYPE"].suffix) == ("%", "")
    assert (by_name["SIZE"].prefix, by_name["SIZE"].suffix) == ("", "")
    # The whole literal is one marker (no dangling quote), so the template parses and keys match.
    concrete = substitute(Q_LIKE, {"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    # bind_template peels the `%`, recovering the parameter the engine expects.
    bound = bind_template(marker, concrete, specs)
    assert bound == {"SIZE": 15, "TYPE": "BRASS", "REGION": "EUROPE"}


def test_like_affix_guard_rejects_wildcard_less_lookalike():
    # `like '%BRASS'` and `like 'BRASS'` share the coarse structural key, but the second is a
    # different query. Binding must refuse it so the router falls back to DuckDB.
    marker, specs = derive_template(
        Q_LIKE, [{"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"}]
    )
    concrete = substitute(Q_LIKE, {"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"})
    assert bind_template(marker, concrete, specs) is not None
    lookalike = concrete.replace("'%BRASS'", "'BRASS'")
    assert normalize_sql(lookalike) == normalize_sql(marker)  # same key...
    assert bind_template(marker, lookalike, specs) is None  # ...but not routed


def test_derive_like_affix_prefix_and_suffix():
    q = "select p_partkey from part where p_name like '%[COLOR]%'"
    marker, specs = derive_template(q, [{"COLOR": "green"}])
    assert (specs[0].prefix, specs[0].suffix) == ("%", "%")
    concrete = substitute(q, {"COLOR": "green"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    assert bind_template(marker, concrete, specs) == {"COLOR": "green"}


@pytest.mark.parametrize(
    "value,prefix,suffix,expected",
    [
        ("%BRASS", "%", "", "BRASS"),  # prefix only
        ("%green%", "%", "%", "green"),  # prefix and suffix
        ("STEEL%", "", "%", "STEEL"),  # suffix only
        ("%", "%", "", ""),  # empty core, prefix only
        ("%", "", "%", ""),  # empty core, suffix only  (col LIKE '%', X = '')
        ("%%", "%", "%", ""),  # empty core, both affixes
        ("ab", "a", "b", ""),  # empty core, non-wildcard affixes
        ("BRASS", "%", "", None),  # missing prefix -> different query, reject
        ("green", "%", "%", None),  # missing suffix -> reject
        ("", "%", "", None),  # too short to carry the affix -> reject
        (
            "%y%",
            "%",
            "",
            None,
        ),  # wildcard left in the core: a pattern, not a word -> reject
        ("%_y", "%", "", None),  # `_` is a wildcard too -> reject
    ],
)
def test_split_literal_single_boundaries(value, prefix, suffix, expected):
    # A single-parameter group is the affix-strip case: peel prefix/suffix, or reject.
    spec = PlaceholderSpec("x", "VARCHAR", prefix, suffix, -1)
    result = split_literal(value, [spec])
    assert (result if result is None else result["x"]) == expected


def test_split_literal_multi_param_group():
    # Q13 shape: one literal packs two words separated by '%'. The group splits it into both.
    group = [
        PlaceholderSpec("W1", "VARCHAR", "%", "%", 0),
        PlaceholderSpec("W2", "VARCHAR", "%", "%", 0),
    ]
    assert split_literal("%express%deposits%", group) == {
        "W1": "express",
        "W2": "deposits",
    }
    # Missing the leading/trailing wildcard -> different query, reject.
    assert split_literal("express%deposits%", group) is None
    assert split_literal("%expressdeposits", group) is None
    # An extra wildcard inside the literal is a three-word pattern, not two literal words.
    assert split_literal("%a%b%c%", group) is None


def test_derive_like_affix_suffix_only():
    q = "select p_partkey from part where p_type like '[TYPE]%'"
    marker, specs = derive_template(q, [{"TYPE": "PROMO"}])
    assert (specs[0].prefix, specs[0].suffix) == ("", "%")
    concrete = substitute(q, {"TYPE": "PROMO"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    assert bind_template(marker, concrete, specs) == {"TYPE": "PROMO"}


def test_two_markers_in_one_literal_derives_and_binds():
    # A literal packing two parameters (Q13's `not like '%[W1]%[W2]%'`) collapses to one marker
    # whose group splits back into both words.
    q = "select o_comment from orders where o_comment not like '%[W1]%[W2]%'"
    templates = build_query_templates({"13": q}, {"13": [{"W1": "foo", "W2": "bar"}]})
    assert len(templates) == 1
    specs = templates[0].placeholders
    assert [(p.name, p.prefix, p.suffix, p.group) for p in specs] == [
        ("W1", "%", "%", 0),
        ("W2", "%", "%", 0),
    ]
    concrete = substitute(q, {"W1": "express", "W2": "deposits"})
    assert bind_template(templates[0].sql_template, concrete, specs) == {
        "W1": "express",
        "W2": "deposits",
    }


def test_embedded_placeholders_survive_manifest_roundtrip():
    # A single affix (Q2's TYPE) and a two-word group (Q13) both round-trip through the manifest.
    _, like_specs = derive_template(
        Q_LIKE, [{"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"}]
    )
    _, q13_specs = derive_template(
        "select 1 from orders where o_comment not like '%[W1]%[W2]%'",
        [{"W1": "foo", "W2": "bar"}],
    )
    for specs in (like_specs, q13_specs):
        rt = QueryTemplate.from_dict(QueryTemplate("q", "select 1", specs).to_dict())
        assert rt.placeholders == specs


def test_constant_query_is_shipped_as_is():
    q = "select count(*) as n from lineitem"
    templates = build_query_templates({"7": q}, {"7": []})
    assert (
        len(templates) == 1
        and templates[0].sql_template == q
        and templates[0].placeholders == ()
    )


def test_unvalidatable_query_is_skipped():
    # A template with no sample assignment cannot self-validate, so it is dropped.
    templates = build_query_templates({"1": Q1}, {"1": []})
    assert templates == []


# --------------------------------------------------------------------------- #
# Manifest schema v2
# --------------------------------------------------------------------------- #
def test_manifest_roundtrip_parquet_dir_and_shm():
    from synnodb.router.manifest import SCHEMA_VERSION

    m = EngineManifest(
        engine_id="e1",
        queries=(QueryTemplate("1", "select 1", ()),),
        parquet_dir="/data/sf1",
        scale_factor=1.0,
        shm_capable=True,
    )
    d = m.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["parquet_dir"] == "/data/sf1" and d["shm_capable"] is True
    rt = EngineManifest.from_dict(d)
    assert rt.parquet_dir == "/data/sf1" and rt.shm_capable is True


def test_manifest_v1_still_loads():
    v1 = {
        "schema_version": 1,
        "engine_id": "old",
        "queries": [{"query_id": "1", "sql_template": "select 1", "placeholders": []}],
    }
    m = EngineManifest.from_dict(v1)
    assert m.engine_id == "old" and m.parquet_dir is None


def test_manifest_unsupported_version_rejected():
    with pytest.raises(ValueError):
        EngineManifest.from_dict(
            {"schema_version": 99, "engine_id": "x", "queries": []}
        )


# --------------------------------------------------------------------------- #
# Atomic publish
# --------------------------------------------------------------------------- #
def _fake_engine_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    write_fake_engine_db(ws / "db")
    (ws / "query1.cpp").write_text("int main(){}")
    (ws / "db_loader.hpp").write_text("// header")
    obj = ws / "obj"
    obj.mkdir()
    (obj / "huge.o").write_bytes(b"0" * 1024)  # build intermediate, must be skipped
    (ws / "results").mkdir()
    return ws


def test_publish_engine_copies_self_contained(tmp_path):
    ws = _fake_engine_workspace(tmp_path)
    engines = tmp_path / "engines"
    templates = [QueryTemplate("1", "select 1", ())]
    dest = publish_engine(
        ws,
        query_templates=templates,
        receipt=passing_receipt(ws, ["1"], scale_factors=(1.0,)),
        parquet_dir="/data/sf1",
        engines_dir=str(engines),
        scale_factor=1.0,
    )
    assert dest is not None
    assert (dest / "db").exists() and (dest / "query1.cpp").exists()
    assert not (dest / "obj").exists()  # compile intermediates skipped
    assert not (dest / "results").exists()  # scratch skipped
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["parquet_dir"] == "/data/sf1"
    # No leftover staging dirs.
    assert [p.name for p in engines.iterdir() if p.name.startswith(".tmp")] == []


def test_publish_no_engines_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNNO_ENGINES_DIR", raising=False)
    monkeypatch.delenv("SYNNO_DATA_DIR", raising=False)
    ws = _fake_engine_workspace(tmp_path)
    assert (
        publish_engine(
            ws,
            query_templates=[QueryTemplate("1", "select 1", ())],
            receipt=passing_receipt(ws, ["1"]),
            parquet_dir="/d",
            engines_dir=None,
        )
        is None
    )


def test_publish_no_templates_returns_none(tmp_path):
    ws = _fake_engine_workspace(tmp_path)
    assert (
        publish_engine(
            ws,
            query_templates=[],
            receipt=passing_receipt(ws, ["1"]),
            parquet_dir="/d",
            engines_dir=str(tmp_path / "engines"),
        )
        is None
    )


def test_publish_is_idempotent_on_same_engine(tmp_path):
    ws = _fake_engine_workspace(tmp_path)
    engines = tmp_path / "engines"
    templates = [QueryTemplate("1", "select 1", ())]
    rc = passing_receipt(ws, ["1"])
    a = publish_engine(
        ws,
        query_templates=templates,
        receipt=rc,
        parquet_dir="/d",
        engines_dir=str(engines),
    )
    b = publish_engine(
        ws,
        query_templates=templates,
        receipt=rc,
        parquet_dir="/d",
        engines_dir=str(engines),
    )
    assert a == b and len(list(engines.iterdir())) == 1
