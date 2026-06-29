"""Publishing under a friendly ``synno-<db>`` name, bundling a self-contained snapshot, and
resolving a relative ``parquet_dir`` against the engine directory."""
from __future__ import annotations

import duckdb

from synnodb.duckdb_compat.discovery import _resolve_parquet_dir
from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.workloads.engine_publish import publish_engine


def _fake_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "db").write_text("#!/bin/sh\n")  # stand-in binary
    (ws / "engine.cpp").write_text("int main(){return 0;}")  # a source for the content hash
    return ws


def _snapshot(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    duckdb.connect().execute(f"COPY (SELECT 1 AS x) TO '{snap}/t.parquet' (FORMAT PARQUET)")
    return snap


def test_publish_named_with_bundled_snapshot(tmp_path):
    ws = _fake_workspace(tmp_path)
    engines = tmp_path / "engines"
    dest = publish_engine(
        ws, query_templates=[QueryTemplate("1", "SELECT * FROM t", ())],
        engines_dir=str(engines), name="synno-foo", shm_capable=True,
        bundle_parquet_dir=str(_snapshot(tmp_path)),
    )
    assert dest is not None and dest.name == "synno-foo"
    man = EngineManifest.read(dest / "manifest.json")
    assert man.shm_capable is True
    assert man.parquet_dir == "data"  # portable relative reference
    assert (dest / "data" / "t.parquet").exists()


def test_named_republish_replaces(tmp_path):
    ws = _fake_workspace(tmp_path)
    engines = tmp_path / "engines"
    snap = _snapshot(tmp_path)
    first = publish_engine(ws, query_templates=[QueryTemplate("1", "SELECT * FROM t", ())],
                           engines_dir=str(engines), name="synno-foo", shm_capable=True,
                           bundle_parquet_dir=str(snap))
    second = publish_engine(ws, query_templates=[QueryTemplate("1", "SELECT * FROM t", ())],
                            engines_dir=str(engines), name="synno-foo", shm_capable=False,
                            bundle_parquet_dir=str(snap))
    assert second == first  # same friendly directory
    assert EngineManifest.read(first / "manifest.json").shm_capable is False  # replaced in place
    # The only discoverable entry is the engine itself; publish infrastructure (.versions, .locks)
    # is '.'-prefixed and skipped by discovery, and no '.tmp-*'/'.link-*' staging leaks remain.
    entries = sorted(p.name for p in engines.iterdir())
    assert [e for e in entries if not e.startswith(".")] == ["synno-foo"]
    assert not [e for e in entries if e.startswith(".tmp-") or e.startswith(".link-")]
    # The superseded version is collected: exactly one live version remains under .versions.
    assert len(list((engines / ".versions").glob("synno-foo@*"))) == 1


def test_shm_only_publish_has_no_snapshot(tmp_path):
    ws = _fake_workspace(tmp_path)
    engines = tmp_path / "engines"
    dest = publish_engine(ws, query_templates=[QueryTemplate("1", "SELECT * FROM t", ())],
                          engines_dir=str(engines), name="synno-shm", shm_capable=True)
    man = EngineManifest.read(dest / "manifest.json")
    assert man.shm_capable is True
    assert man.parquet_dir is None
    assert not (dest / "data").exists()


def test_resolve_relative_and_absolute_parquet_dir(tmp_path):
    engine_dir = tmp_path / "eng"
    (engine_dir / "data").mkdir(parents=True)
    rel = EngineManifest(engine_id="e", queries=(), parquet_dir="data")
    assert _resolve_parquet_dir(rel, engine_dir) == engine_dir / "data"
    absolute = EngineManifest(engine_id="e", queries=(), parquet_dir=str(engine_dir / "data"))
    assert _resolve_parquet_dir(absolute, engine_dir) == engine_dir / "data"
    none = EngineManifest(engine_id="e", queries=())
    assert _resolve_parquet_dir(none, engine_dir) is None
    missing = EngineManifest(engine_id="e", queries=(), parquet_dir="absent")
    assert _resolve_parquet_dir(missing, engine_dir) is None
    assert _resolve_parquet_dir(missing, engine_dir, require_exists=False) == engine_dir / "absent"
