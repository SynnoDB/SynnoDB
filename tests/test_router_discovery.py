"""Engine auto-discovery: a published manifest is found, registered, and routed to, with no
explicit registration call. Uses an in-process engine (the manifest's ProcessEngine builder
is replaced) so no compiled binary is needed.
"""

from __future__ import annotations


import pyarrow as pa
import pytest

import synnodb
from synnodb.duckdb_compat import discovery
from synnodb.duckdb_compat.discovery import resolve_engines_dir
from synnodb.router import (
    LocalCallableEngine,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
)
from synnodb.router.manifest import EngineManifest, QueryTemplate
from synnodb.router.registry import ColumnSpec, PlaceholderSpec

SNAPSHOT = [1, 2, 3, 4, 5]
TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= ?"


def _engine_fn(ph):
    x = int(ph["p0"])
    return pa.table({"c": pa.array([sum(1 for a in SNAPSHOT if a >= x)], pa.int64())})


def _write_manifest(engines_dir, engine_id="eng-test", expected_tables=None):
    d = engines_dir / engine_id
    d.mkdir(parents=True)
    manifest = EngineManifest(
        engine_id=engine_id,
        queries=(QueryTemplate("1", TEMPLATE, (PlaceholderSpec("p0", "INTEGER"),)),),
        parquet_dir="/unused",
        expected_tables=expected_tables or {},
    )
    manifest.write(d)
    return d


@pytest.fixture
def patched_engine(monkeypatch):
    # Replace the ProcessEngine builder with an in-process engine so no binary is needed.
    monkeypatch.setattr(
        discovery,
        "_build_engine",
        lambda manifest, engine_dir, **_: LocalCallableEngine(
            manifest.engine_id, {"1": _engine_fn}
        ),
    )


def _con(engines_dir):
    con = synnodb.connect(
        engines=str(engines_dir),
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER)")
    con.duckdb.execute("INSERT INTO t SELECT * FROM range(1, 6)")
    return con


def test_resolve_engines_dir_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("SYNNO_ENGINES_DIR", raising=False)
    monkeypatch.delenv("SYNNO_DATA_DIR", raising=False)
    assert resolve_engines_dir(None) is None
    monkeypatch.setenv("SYNNO_DATA_DIR", str(tmp_path))
    assert resolve_engines_dir(None) == tmp_path / "engines"
    monkeypatch.setenv("SYNNO_ENGINES_DIR", str(tmp_path / "e"))
    assert resolve_engines_dir(None) == tmp_path / "e"
    assert resolve_engines_dir("/explicit").as_posix() == "/explicit"  # explicit wins


def test_discovers_and_routes(patched_engine, tmp_path):
    engines = tmp_path / "engines"
    _write_manifest(engines)
    con = _con(engines)
    con.refresh_engines()
    assert con.router_stats()["registry"]["templates"] == 1
    sql = "SELECT count(*) AS c FROM t WHERE a >= 3"
    assert con.execute(sql).fetchall() == con.duckdb.execute(sql).fetchall()
    assert con.router_stats()["session"]["routed"] == 1
    assert con.router_stats()["session"]["cross_check_mismatch"] == 0


def test_auto_pickup_after_connect(patched_engine, tmp_path):
    engines = tmp_path / "engines"
    engines.mkdir()
    con = _con(engines)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 3"
    con.execute(sql)  # nothing published yet -> DuckDB
    assert con.router_stats()["session"]["routed"] == 0
    _write_manifest(engines)  # a base impl "finishes"
    con.refresh_engines()
    con.execute(sql)
    assert con.router_stats()["session"]["routed"] == 1


def test_incompatible_engine_is_skipped_then_retried(patched_engine, tmp_path):
    engines = tmp_path / "engines"
    # Requires a table the DB does not have -> strict compat fails -> skipped, not registered.
    _write_manifest(engines, expected_tables={"absent": (ColumnSpec("x", "INTEGER"),)})
    con = _con(engines)
    con.refresh_engines()
    assert con.router_stats()["registry"]["templates"] == 0
    # It is not recorded as registered, so a later scan retries it; create the table and the
    # next refresh registers it.
    con.duckdb.execute("CREATE TABLE absent(x INTEGER)")
    con.refresh_engines()
    assert con.router_stats()["registry"]["templates"] == 1


def test_malformed_manifest_is_ignored(patched_engine, tmp_path):
    engines = tmp_path / "engines"
    bad = engines / "broken"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{ not valid json")
    con = _con(engines)
    con.refresh_engines()  # must not raise
    assert con.router_stats()["registry"]["templates"] == 0


def test_staging_dirs_are_skipped(patched_engine, tmp_path):
    engines = tmp_path / "engines"
    staging = engines / ".tmp-eng-xyz"
    staging.mkdir(parents=True)
    EngineManifest(
        engine_id="eng-staging",
        queries=(QueryTemplate("1", TEMPLATE, (PlaceholderSpec("p0", "INTEGER"),)),),
    ).write(staging)
    con = _con(engines)
    con.refresh_engines()
    assert con.router_stats()["registry"]["templates"] == 0  # .tmp-* ignored


def test_no_engines_dir_means_no_discovery(tmp_path):
    con = synnodb.connect(engines=None, policy=RouterPolicy(mode=RouterMode.SAMPLED))
    con.duckdb.execute("CREATE TABLE t(a INTEGER)")
    con.refresh_engines()  # no-op, must not raise
    assert con.router_stats()["registry"]["templates"] == 0
