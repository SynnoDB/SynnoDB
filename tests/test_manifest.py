"""Engine manifest: round-trip, register-and-route, and the compatibility gate."""
from __future__ import annotations

import pyarrow as pa
import pytest

import synnodb
from synnodb.router import (
    ColumnSpec,
    EngineManifest,
    LocalCallableEngine,
    PlaceholderSpec,
    QueryTemplate,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    build_manifest_from_dir,
    check_compatibility,
    content_engine_id,
    infer_duckdb_type,
    register_manifest,
    write_manifest_for_engine,
)

TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= ?"


def _manifest(expected_tables=None):
    return EngineManifest(
        engine_id="eng-abc123",
        storage_mode="flat",
        scale_factor=1.0,
        source_run_id="run-xyz",
        expected_tables=expected_tables or {},
        queries=(
            QueryTemplate(
                query_id="1",
                sql_template=TEMPLATE,
                placeholders=(PlaceholderSpec("p0", "INTEGER"),),
            ),
        ),
    )


def _engine():
    snapshot = [1, 2, 3, 4, 5]
    return LocalCallableEngine(
        "eng-abc123",
        {"1": lambda ph: pa.table({"c": pa.array([sum(1 for a in snapshot if a >= int(ph["p0"]))], pa.int64())})},
    )


def _con():
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    return con


# --------------------------------------------------------------------------- #
def test_manifest_roundtrip_dict_and_file(tmp_path):
    m = _manifest({"t": (ColumnSpec("a", "INTEGER"), ColumnSpec("b", "VARCHAR"))})
    assert EngineManifest.from_dict(m.to_dict()) == m
    path = m.write(tmp_path)
    assert path.name == "manifest.json"
    assert EngineManifest.read(tmp_path) == m


def test_manifest_rejects_unknown_schema_version():
    d = _manifest().to_dict()
    d["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        EngineManifest.from_dict(d)


def test_register_manifest_routes_and_matches_duckdb():
    con = _con()
    register_manifest(con, _manifest(), _engine())
    sql = "SELECT count(*) AS c FROM t WHERE a >= 3"
    assert con.execute(sql).fetchall() == con.duckdb.execute(sql).fetchall()
    assert len(con.router.registry) == 1


def test_compatibility_ok_when_schema_matches():
    con = _con()
    expected = {"t": (ColumnSpec("a", "INTEGER"), ColumnSpec("b", "VARCHAR"))}
    assert check_compatibility(con, _manifest(expected)) == []


def test_compatibility_flags_missing_table():
    con = _con()
    expected = {"absent": (ColumnSpec("x", "INTEGER"),)}
    problems = check_compatibility(con, _manifest(expected))
    assert any("missing" in p for p in problems)


def test_compatibility_flags_type_drift():
    con = _con()
    expected = {"t": (ColumnSpec("a", "BIGINT"), ColumnSpec("b", "VARCHAR"))}  # a is really INTEGER
    problems = check_compatibility(con, _manifest(expected))
    assert any("differs" in p for p in problems)


def test_register_manifest_strict_rejects_incompatible():
    con = _con()
    incompatible = _manifest({"t": (ColumnSpec("a", "BIGINT"),)})
    with pytest.raises(ValueError, match="incompatible"):
        register_manifest(con, incompatible, _engine())


# --------------------------------------------------------------------------- #
# Content-addressed engine id + factory-side manifest builder
# --------------------------------------------------------------------------- #
def test_content_engine_id_is_deterministic_and_change_sensitive():
    a = content_engine_id({"query_impl.cpp": "int main(){}", "db.hpp": "struct D{};"})
    b = content_engine_id({"db.hpp": "struct D{};", "query_impl.cpp": "int main(){}"})  # order-independent
    c = content_engine_id({"query_impl.cpp": "int main(){return 1;}", "db.hpp": "struct D{};"})
    assert a == b and a != c and a.startswith("eng-")


def test_build_manifest_from_dir_writes_and_addresses(tmp_path):
    (tmp_path / "query_impl.cpp").write_text("// generated engine\nint q(){return 0;}")
    (tmp_path / "db_loader.hpp").write_text("struct Database{};")
    queries = [QueryTemplate("1", TEMPLATE, (PlaceholderSpec("p0", "INTEGER"),))]
    m = build_manifest_from_dir(tmp_path, queries, storage_mode="bespoke", scale_factor=10.0)
    assert m.engine_id.startswith("eng-")
    assert (tmp_path / "manifest.json").exists()
    assert EngineManifest.read(tmp_path) == m
    # rebuilding identical sources yields the same id; changing a source changes it.
    same = build_manifest_from_dir(tmp_path, queries, write=False)
    assert same.engine_id == m.engine_id
    (tmp_path / "query_impl.cpp").write_text("// changed")
    changed = build_manifest_from_dir(tmp_path, queries, write=False)
    assert changed.engine_id != m.engine_id


@pytest.mark.parametrize(
    "sample,duck",
    [(5, "INTEGER"), (1.5, "DOUBLE"), (True, "BOOLEAN"), ("1998-09-01", "DATE"),
     ("42", "INTEGER"), ("3.14", "DOUBLE"), ("BUILDING", "VARCHAR")],
)
def test_infer_duckdb_type(sample, duck):
    assert infer_duckdb_type(sample) == duck


def test_factory_to_runtime_full_loop(tmp_path):
    """Engine dir + generator-style placeholders -> manifest.json -> register -> route."""
    # 1. factory side: a generated engine dir + the generator's query metadata
    #    (placeholders as {name: sample_value}, exactly what gen_query_fn returns).
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "query_impl.cpp").write_text("// generated engine sources")
    metadata = [("1", "SELECT count(*) AS c FROM t WHERE a >= $p0", {"p0": 2})]
    manifest = write_manifest_for_engine(engine_dir, metadata, storage_mode="flat", scale_factor=1.0)
    assert (engine_dir / "manifest.json").exists()
    assert manifest.queries[0].placeholders[0] == __import__(
        "synnodb.router", fromlist=["PlaceholderSpec"]
    ).PlaceholderSpec("p0", "INTEGER")

    # 2. runtime side: read it back and register against a live connection, then route.
    loaded = EngineManifest.read(engine_dir)
    con = _con()
    register_manifest(con, loaded, _engine())
    sql = "SELECT count(*) AS c FROM t WHERE a >= 3"
    assert con.execute(sql).fetchall() == con.duckdb.execute(sql).fetchall()
