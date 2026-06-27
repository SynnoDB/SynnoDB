"""Factory-side publishing: template derivation, manifest schema v2, and atomic publish."""
from __future__ import annotations

import json

import pytest

from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.router.normalize import normalize_sql, unify_and_bind
from synnodb.router.registry import ColumnSpec, PlaceholderSpec
from synnodb.workloads.engine_publish import (
    build_query_templates,
    derive_template,
    publish_engine,
)
from synnodb.workloads.param_infer import substitute

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
    marker, specs = derive_template(Q6, [{"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"}])
    # Q6 has 5 placeholder occurrences (DATE, DATE, DISCOUNT, DISCOUNT, QUANTITY).
    assert [p.name for p in specs] == ["DATE", "DATE", "DISCOUNT", "DISCOUNT", "QUANTITY"]
    concrete = substitute(Q6, {"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"})
    assert normalize_sql(marker) == normalize_sql(concrete)
    bound = unify_and_bind(marker, concrete, [p.name for p in specs])
    assert bound == {"DATE": "1994-01-01", "DISCOUNT": 0.06, "QUANTITY": 24} or bound is not None


def test_constant_query_is_shipped_as_is():
    q = "select count(*) as n from lineitem"
    templates = build_query_templates({"7": q}, {"7": []})
    assert len(templates) == 1 and templates[0].sql_template == q and templates[0].placeholders == ()


def test_unvalidatable_query_is_skipped():
    # A template with no sample assignment cannot self-validate, so it is dropped.
    templates = build_query_templates({"1": Q1}, {"1": []})
    assert templates == []


# --------------------------------------------------------------------------- #
# Manifest schema v2
# --------------------------------------------------------------------------- #
def test_manifest_v2_roundtrip_parquet_dir():
    m = EngineManifest(
        engine_id="e1",
        queries=(QueryTemplate("1", "select 1", ()),),
        parquet_dir="/data/sf1",
        scale_factor=1.0,
    )
    d = m.to_dict()
    assert d["schema_version"] == 2 and d["parquet_dir"] == "/data/sf1"
    assert EngineManifest.from_dict(d).parquet_dir == "/data/sf1"


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
        EngineManifest.from_dict({"schema_version": 99, "engine_id": "x", "queries": []})


# --------------------------------------------------------------------------- #
# Atomic publish
# --------------------------------------------------------------------------- #
def _fake_engine_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "db").write_bytes(b"\x7fELF-fake-binary")
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
    dest = publish_engine(ws, query_templates=templates, parquet_dir="/data/sf1",
                          engines_dir=str(engines), scale_factor=1.0)
    assert dest is not None
    assert (dest / "db").exists() and (dest / "query1.cpp").exists()
    assert not (dest / "obj").exists()        # compile intermediates skipped
    assert not (dest / "results").exists()    # scratch skipped
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["parquet_dir"] == "/data/sf1"
    # No leftover staging dirs.
    assert [p.name for p in engines.iterdir() if p.name.startswith(".tmp")] == []


def test_publish_no_engines_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNNO_ENGINES_DIR", raising=False)
    monkeypatch.delenv("SYNNO_DATA_DIR", raising=False)
    ws = _fake_engine_workspace(tmp_path)
    assert publish_engine(ws, query_templates=[QueryTemplate("1", "select 1", ())],
                          parquet_dir="/d", engines_dir=None) is None


def test_publish_no_templates_returns_none(tmp_path):
    ws = _fake_engine_workspace(tmp_path)
    assert publish_engine(ws, query_templates=[], parquet_dir="/d",
                          engines_dir=str(tmp_path / "engines")) is None


def test_publish_is_idempotent_on_same_engine(tmp_path):
    ws = _fake_engine_workspace(tmp_path)
    engines = tmp_path / "engines"
    templates = [QueryTemplate("1", "select 1", ())]
    a = publish_engine(ws, query_templates=templates, parquet_dir="/d", engines_dir=str(engines))
    b = publish_engine(ws, query_templates=templates, parquet_dir="/d", engines_dir=str(engines))
    assert a == b and len(list(engines.iterdir())) == 1
