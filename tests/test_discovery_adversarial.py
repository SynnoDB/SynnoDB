"""Regression tests for the engine discovery / publish / manifest hardening (C1, H1-H4, M1-M3, L1).

Each test began as an adversarial probe that exposed a defect; it now asserts the *fixed*
behavior. ``test_GUARD_*`` tests pin behavior that was reviewed and found already correct. All
use a real in-memory DuckDB plus a fake engine, so no compiled C++ engine is needed.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import duckdb
import pytest

from synnodb.duckdb_compat import connect
from synnodb.duckdb_compat.connection import SynnoConnection
from synnodb.duckdb_compat.discovery import (
    _aligned_arrow,
    _tables_present,
    discover_engines,
)
from synnodb.router.manifest import (
    EngineManifest,
    QueryTemplate,
    build_manifest_from_dir,
)
from synnodb.router.registry import ColumnSpec, TemplateRegistry
from synnodb.workloads.engine_publish import publish_engine

from receipt_helpers import passing_receipt, write_fake_engine_db


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeEngine:
    """A stand-in for ProcessEngine/ShmHotLoadEngine that records what it ingested and
    whether it was closed, so a test can assert on data-plane selection and cleanup."""

    instances: list = []

    def __init__(self, engine_id: str, workspace, *args, **kwargs):
        # Mirrors both ProcessEngine(engine_id, workspace, parquet_dir) and
        # ShmHotLoadEngine(engine_id, workspace) constructor arities.
        self.engine_id = engine_id
        self.workspace = Path(workspace)
        self.parquet_dir = args[0] if args else None
        self.ingested = None
        self.closed = False
        self.output_schemas: dict = {}
        FakeEngine.instances.append(self)

    def ingest(self, tables: dict) -> None:
        self.ingested = {k: v for k, v in tables.items()}

    def close(self) -> None:
        self.closed = True


class FakeRouter:
    def __init__(self):
        self.registry = TemplateRegistry()

    # router.why / router.stats are unused by the lifecycle tests.


def _conn_with_router():
    """A SynnoConnection over a fresh in-memory DuckDB and a real registry."""
    inner = duckdb.connect(":memory:")
    return SynnoConnection(inner, FakeRouter())


def _make_workspace(tmp_path, src="int main(){return 0;}") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    write_fake_engine_db(
        ws / "db"
    )  # a real build-id so the publish gate's identity check runs
    (ws / "engine.cpp").write_text(src)
    return ws


def _publish_manifest(engine_dir: Path, manifest: EngineManifest) -> None:
    engine_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(engine_dir)


@pytest.fixture(autouse=True)
def _reset_fake_engines():
    FakeEngine.instances.clear()
    yield
    FakeEngine.instances.clear()


# --------------------------------------------------------------------------- #
# 1. Atomicity / races in the named-republish path
# --------------------------------------------------------------------------- #
def test_named_publish_crash_during_flip_keeps_old_engine(tmp_path, monkeypatch):
    """H1 (fixed): a crash during republish must never leave a missing engine. Publishing now
    writes a fresh immutable version and atomically flips the `<name>` symlink onto it as the very
    last step; if that flip fails, the old version stays fully linked and discoverable.
    """
    import synnodb.workloads.engine_publish as ep

    ws = _make_workspace(tmp_path)
    engines = tmp_path / "engines"
    d1 = publish_engine(
        ws,
        query_templates=[QueryTemplate("1", "select 1", ())],
        receipt=passing_receipt(ws, ["1"]),
        engines_dir=str(engines),
        name="synno-foo",
    )
    assert (Path(d1) / "manifest.json").exists()

    # Make the final symlink flip fail (simulate a crash at the last atomic step).
    real_replace = ep.os.replace

    def boom(src, dst):
        if Path(dst).name == "synno-foo":  # the flip onto <name>
            raise RuntimeError("crash during flip")
        return real_replace(src, dst)

    monkeypatch.setattr(ep.os, "replace", boom)
    with pytest.raises(RuntimeError):
        publish_engine(
            ws,
            query_templates=[QueryTemplate("1", "select 2", ())],
            receipt=passing_receipt(ws, ["1"]),
            engines_dir=str(engines),
            name="synno-foo",
        )
    monkeypatch.setattr(ep.os, "replace", real_replace)

    # The engine is still present and valid (the old version) - never lost.
    dest = engines / "synno-foo"
    assert dest.exists() and (dest / "manifest.json").exists(), (
        "engine vanished after a failed republish: the swap is not crash-atomic"
    )
    # And it still resolves/discovers cleanly.
    conn = _conn_with_router()
    try:
        discover_engines(conn, engines, set())  # must not raise on the engines dir
    finally:
        conn.close()


def test_concurrent_republish_same_name_is_safe(tmp_path):
    """H2 (fixed): concurrent republishes of the same name are serialized by a per-name lock and
    each lands as an atomic symlink flip, so none raises and the engine stays valid throughout.
    """
    ws = _make_workspace(tmp_path)
    engines = tmp_path / "engines"
    publish_engine(
        ws,
        query_templates=[QueryTemplate("1", "select 1", ())],
        receipt=passing_receipt(ws, ["1", "2", "3"]),
        engines_dir=str(engines),
        name="synno-foo",
    )

    errors: list = []

    def worker(qid: str):
        try:
            for _ in range(5):
                publish_engine(
                    ws,
                    query_templates=[QueryTemplate(qid, f"select {qid}", ())],
                    receipt=passing_receipt(ws, ["1", "2", "3"]),
                    engines_dir=str(engines),
                    name="synno-foo",
                )
        except Exception as exc:  # noqa: BLE001 - any failure is the bug
            errors.append((qid, exc))

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent republish raised (not lock-serialized): {errors!r}"
    dest = engines / "synno-foo"
    assert dest.exists() and (dest / "manifest.json").exists(), (
        "engine invalid after concurrent republish"
    )
    # Superseded versions are GC'd: at most a small bounded number remain under .versions.
    versions = list((engines / ".versions").glob("synno-foo@*"))
    assert len(versions) <= 2, (
        f"superseded versions not collected: {[v.name for v in versions]}"
    )


def test_GUARD_discovery_skips_dot_prefixed_staging_and_trash(tmp_path):
    """A leftover `.tmp-*` / `.old-*` directory must never be discovered as an engine. This is
    the one mitigation that does hold: discovery skips '.'-prefixed dirs. (Pins the behavior.)
    """
    engines = tmp_path / "engines"
    engines.mkdir()
    # A leftover trash dir that *contains a valid manifest* (the worst case).
    trash = engines / ".old-synno-foo-leftover"
    man = build_manifest_from_dir(
        _make_workspace(tmp_path), [QueryTemplate("1", "select 1", ())], write=False
    )
    _publish_manifest(trash, man)
    conn = _conn_with_router()
    try:
        registered = discover_engines(conn, engines, set())
        assert registered == set(), "a .-prefixed leftover must not be discovered"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. Plane selection: shm hot-load bypasses schema compatibility
# --------------------------------------------------------------------------- #
def test_shm_plane_refuses_ingest_without_expected_tables(tmp_path, monkeypatch):
    """C1 (fixed): a shm-capable manifest with EMPTY `expected_tables` must be refused, not bound.
    Previously `_bind_engine` decided the shm plane on table-name presence alone and the only
    schema gate (`register_manifest`) was skipped when `expected_tables` was empty, so the engine
    ingested whatever same-named tables the live DB held - silently serving wrong results. The
    `_shm_schema_ok` gate now refuses an engine that cannot declare/verify its schema.
    """
    import synnodb.router.process_engine as pe

    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeEngine)

    engines = tmp_path / "engines"
    engine_dir = engines / "synno-x"
    ws = _make_workspace(tmp_path)
    # shm-capable, NO expected_tables. The query is structurally valid against ANY shape of
    # `lineitem` (count(*)), so describe_output succeeds and nothing gates the schema.
    man = build_manifest_from_dir(
        ws,
        [QueryTemplate("1", "select count(*) as n from lineitem", ())],
        shm_capable=True,
        write=False,
    )
    _publish_manifest(engine_dir, man)
    # Engine sources must hash to the manifest's engine_id for discovery to bind it.
    (engine_dir / "engine.cpp").write_text("int main(){return 0;}")

    conn = _conn_with_router()
    try:
        # Live DB has a `lineitem` with a COMPLETELY different schema than the TPC-H lineitem
        # the engine was built for (one VARCHAR column instead of the 16 typed TPC-H columns).
        conn.duckdb.execute(
            "CREATE TABLE lineitem(wrong_col VARCHAR); INSERT INTO lineitem VALUES ('garbage')"
        )
        registered = discover_engines(conn, engines, set())
        ingested = [e for e in FakeEngine.instances if e.ingested is not None]
        ingested_cols = [list(e.ingested["lineitem"].schema.names) for e in ingested]
        # FAILS: the engine registered (templates=1) AND ingested the schema-incompatible
        # table into shm, with no gate - `register_manifest`'s check_compatibility only runs
        # when expected_tables is set. The engine would now serve this query from wrong data.
        assert registered == set() and not ingested, (
            "shm plane hot-loaded a schema-incompatible table and registered it with no "
            f"expected_tables gate: registered={registered}, ingested cols={ingested_cols}, "
            f"templates={conn.router.registry.stats()['templates']}"
        )
    finally:
        conn.close()


def test_GUARD_shm_plane_with_expected_tables_rejects_wrong_schema(
    tmp_path, monkeypatch
):
    """When `expected_tables` IS set but the live schema differs, the engine is rejected BEFORE
    ingest by the `_shm_schema_ok` gate - so no `/dev/shm` segment is ever written and there is
    nothing to clean up. (Stronger than the old ingest-then-close behavior.)
    """
    import synnodb.router.process_engine as pe

    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeEngine)

    engines = tmp_path / "engines"
    engine_dir = engines / "synno-x"
    ws = _make_workspace(tmp_path)
    man = build_manifest_from_dir(
        ws,
        [QueryTemplate("1", "select sum(l_quantity) from lineitem", ())],
        shm_capable=True,
        expected_tables={"lineitem": (ColumnSpec("l_quantity", "BIGINT"),)},
        write=False,
    )
    _publish_manifest(engine_dir, man)
    (engine_dir / "engine.cpp").write_text("int main(){return 0;}")

    conn = _conn_with_router()
    try:
        conn.duckdb.execute("CREATE TABLE lineitem(wrong_col VARCHAR)")
        registered = discover_engines(conn, engines, set())
        assert registered == set(), "wrong schema must not register"
        # The schema gate runs BEFORE ingest, so an incompatible engine is never ingested:
        # no /dev/shm segment is written, nothing to clean up.
        ingested = [e for e in FakeEngine.instances if e.ingested is not None]
        assert not ingested, (
            "an incompatible shm engine must be rejected before ingest (no shm write)"
        )
    finally:
        conn.close()


def test_engine_promoted_to_shm_when_live_data_arrives(tmp_path, monkeypatch):
    """L1 (fixed): an engine first bound on the mounted-parquet plane (no live data) is promoted
    to the faster shm hot-load on a later scan once the connection's real tables are present and
    verified - the data plane is no longer frozen at first bind.
    """
    import synnodb.router.process_engine as pe

    class FakeProcess(FakeEngine):
        pass

    class FakeShm(FakeEngine):
        pass

    monkeypatch.setattr(pe, "ProcessEngine", FakeProcess)
    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeShm)

    engines = tmp_path / "engines"
    engine_dir = engines / "synno-x"
    ws = _make_workspace(tmp_path)
    snap = engine_dir / "data"
    snap.mkdir(parents=True)
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS l_quantity) TO '{snap}/lineitem.parquet' (FORMAT PARQUET)"
    )
    man = build_manifest_from_dir(
        ws,
        [QueryTemplate("1", "select sum(l_quantity) from lineitem", ())],
        shm_capable=True,
        parquet_dir="data",
        expected_tables={"lineitem": (ColumnSpec("l_quantity", "INTEGER"),)},
        write=False,
    )
    _publish_manifest(engine_dir, man)
    (engine_dir / "engine.cpp").write_text("int main(){return 0;}")

    conn = _conn_with_router()
    try:
        # 1st scan, no live data: binds on the mounted-parquet plane.
        registered = discover_engines(conn, engines, set(), mount=True)
        assert isinstance(conn.router.registry.bindings()[0].engine, FakeProcess)

        # Live data arrives with the engine's schema (replace the mounted view with a real table).
        conn.duckdb.execute("DROP VIEW IF EXISTS lineitem")
        conn.duckdb.execute(
            "CREATE TABLE lineitem(l_quantity INTEGER); INSERT INTO lineitem VALUES (1)"
        )

        # Rescan: the engine is promoted to the shm hot-load plane (and ingests the live data).
        discover_engines(conn, engines, registered, mount=True)
        promoted = conn.router.registry.bindings()[0].engine
        assert isinstance(promoted, FakeShm) and promoted.ingested is not None
    finally:
        conn.close()


def test_GUARD_partial_table_presence_does_not_bind_shm(tmp_path, monkeypatch):
    """A shm engine needs tables {orders, lineitem}; only `orders` is present. The shm plane
    requires ALL tables present (`set(tables) <= present`), so it must not bind, and with no
    snapshot it stays unservable. Pins the partial-presence guard.
    """
    import synnodb.router.process_engine as pe

    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeEngine)

    engines = tmp_path / "engines"
    engine_dir = engines / "synno-x"
    ws = _make_workspace(tmp_path)
    man = build_manifest_from_dir(
        ws,
        [QueryTemplate("1", "select * from orders, lineitem", ())],
        shm_capable=True,
        write=False,
    )
    _publish_manifest(engine_dir, man)
    (engine_dir / "engine.cpp").write_text("int main(){return 0;}")

    conn = _conn_with_router()
    try:
        conn.duckdb.execute("CREATE TABLE orders(o_orderkey BIGINT)")  # lineitem absent
        registered = discover_engines(conn, engines, set())
        assert registered == set()
        assert not [e for e in FakeEngine.instances if e.ingested is not None]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. SQL / identifier safety
# --------------------------------------------------------------------------- #
def test_GUARD_aligned_arrow_and_quote_ident_handle_quote_in_name(tmp_path):
    """`_quote_ident` doubles embedded double-quotes; `_aligned_arrow` uses it. A table whose
    name contains a double-quote is fetched correctly and is not an injection vector. Pins that
    the SELECT path is identifier-safe (the quoting concern is refuted for SELECT *).
    """
    conn = _conn_with_router()
    try:
        conn.duckdb.execute(
            'CREATE TABLE "we""ird"(x INTEGER); INSERT INTO "we""ird" VALUES (7)'
        )
        got = _aligned_arrow(conn, ['we"ird'])
        assert got['we"ird'].column(0).to_pylist() == [7]
        # Presence check is case-folded; DuckDB folds quoted idents case-insensitively, so the
        # two agree here. (Documented coupling, not a bug for DuckDB specifically.)
        assert 'WE"IRD' in _tables_present(conn, ['WE"IRD'])
    finally:
        conn.close()


def test_GUARD_mount_snapshot_view_path_with_quote_is_escaped(tmp_path):
    """`_mount_snapshot_views` inlines the parquet path into CREATE VIEW. A path containing a
    single quote must be escaped (it is, via doubling), or it is SQL injection into the view
    text. Pins that a quote in the directory name does not break out.
    """
    from synnodb.duckdb_compat.discovery import _mount_snapshot_views

    weird_dir = tmp_path / "od'd"  # apostrophe in the path
    weird_dir.mkdir()
    pf = weird_dir / "lineitem.parquet"
    lit = "'" + str(pf).replace("'", "''") + "'"
    duckdb.connect().execute(f"COPY (SELECT 5 AS v) TO {lit} (FORMAT PARQUET)")
    conn = _conn_with_router()
    try:
        _mount_snapshot_views(conn, ["lineitem"], weird_dir)
        assert conn.duckdb.execute("SELECT v FROM lineitem").fetchone()[0] == 5
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 5. Manifest schema: parquet_dir traversal
# --------------------------------------------------------------------------- #
def test_parquet_dir_traversal_is_rejected(tmp_path):
    """H4 (fixed): a relative `parquet_dir` that escapes the engine package (`../../secret`) is a
    path-traversal vector from an attacker-controllable manifest. The resolver now refuses it
    loudly instead of pointing the runtime at an arbitrary host directory; a contained relative
    snapshot still resolves normally.
    """
    from synnodb.duckdb_compat.discovery import _resolve_parquet_dir
    from synnodb.errors import SynnoError

    engine_dir = tmp_path / "engines" / "synno-x"
    engine_dir.mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    rel = os.path.relpath(outside, engine_dir)  # e.g. "../../secret"
    man = EngineManifest(engine_id="e", queries=(), parquet_dir=rel)
    with pytest.raises(SynnoError):
        _resolve_parquet_dir(man, engine_dir, require_exists=False)

    # A contained relative snapshot is still fine.
    (engine_dir / "data").mkdir()
    ok = EngineManifest(engine_id="e", queries=(), parquet_dir="data")
    resolved = _resolve_parquet_dir(ok, engine_dir, require_exists=False)
    assert resolved is not None and resolved.name == "data"
    assert engine_dir.resolve() in resolved.resolve().parents


# --------------------------------------------------------------------------- #
# 6. connection.py lifecycle
# --------------------------------------------------------------------------- #
def test_optimize_refuses_name_collision_with_different_db(tmp_path):
    """M1 (fixed): `optimize_database` records the source database in the manifest and refuses to
    overwrite an engine of the same name (e.g. the default `synno-tpch`) that was built for a
    *different* database, so two `tpch.db` files in different directories don't silently clobber
    each other. `force=True` (or an explicit `name`) overrides.
    """
    from synnodb.errors import SynnoUnsupportedQuery
    from synnodb.optimize import optimize_database

    engines = tmp_path / "engines"
    ws = _make_workspace(
        tmp_path
    )  # has a 'db' file; the conflict check fires before any DB load

    # An engine named synno-tpch already published for database A.
    publish_engine(
        ws,
        query_templates=[QueryTemplate("1", "select 1", ())],
        receipt=passing_receipt(ws, ["1"]),
        engines_dir=str(engines),
        name="synno-tpch",
        source_db="/data/alpha/tpch.db",
    )

    # optimize_database for a DIFFERENT tpch.db (same stem -> same default name) must refuse.
    with pytest.raises(SynnoUnsupportedQuery) as ei:
        optimize_database(
            "/data/beta/tpch.db",
            ["1"],
            engine_workspace=ws,
            engines_dir=str(engines),
            benchmark="tpch",
        )
    assert "different database" in str(ei.value)


def test_parent_close_keeps_cursor_engines_alive(tmp_path):
    """H3 (fixed): a cursor shares the parent's router/registry, so the engines are shared. A
    shared refcount now keeps them alive until the LAST handle closes - closing the parent while
    a cursor is still open must not tear down an engine the cursor routes to; the last close
    releases it.
    """
    inner = duckdb.connect(":memory:")
    router = FakeRouter()
    parent = SynnoConnection(inner, router, owns_router=True)
    # Register a fake engine into the shared registry via a binding.
    eng = FakeEngine("e1", tmp_path)
    from synnodb.router.registration import make_binding

    parent.duckdb.execute("CREATE TABLE t(x int)")
    binding = make_binding(
        parent, template_sql="select x from t", engine=eng, query_id="1"
    )
    router.registry.register(binding)

    cur = parent.cursor()  # shares router/registry, joins the refcount
    assert not cur._owns_router
    parent.close()
    # The engine the cursor still routes to must stay open.
    assert not eng.closed, (
        "parent.close() closed a shared engine an open cursor still depends on"
    )
    cur.close()
    # The last handle to close releases the shared engines.
    assert eng.closed, "the last connection to close must release the shared engines"


def test_write_parquet_on_no_result_raises_clear_error(tmp_path):
    """M3 (fixed): `write_parquet`/`write_csv` with no current result raise a clear SynnoDB error
    ("no result to write - call execute() first"), not DuckDB's opaque "No open result set"."""
    from synnodb.errors import SynnoError

    conn = connect(":memory:")
    try:
        with pytest.raises(SynnoError) as ei:
            conn.write_parquet(str(tmp_path / "out.parquet"))
        assert "no result to write" in str(ei.value).lower()
        with pytest.raises(SynnoError):
            conn.write_csv(str(tmp_path / "out.csv"))
    finally:
        conn.close()


def test_distinct_packages_same_engine_id_are_both_discovered(tmp_path, monkeypatch):
    """M2 (fixed): two published packages built from identical sources share a content-addressed
    engine_id but are distinct packages. Discovery now identifies a package by its directory AND
    engine_id, so neither is shadowed by the other's id - the old engine_id-only dedup would skip
    one once the other registered, hiding the only servable package for some connection.
    """
    import synnodb.router.process_engine as pe

    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeEngine)
    monkeypatch.setattr(pe, "ProcessEngine", FakeEngine)

    engines = tmp_path / "engines"
    ws = _make_workspace(tmp_path)

    # Two packages built from identical sources -> the SAME content engine_id, each bundling a
    # snapshot (so each is servable via mount) but serving a different query so they do not collide
    # in the registry. The publish directory differs.
    ids = set()
    for name, qid in (("aaa-pkg", "1"), ("zzz-pkg", "2")):
        d = engines / name
        snap = d / "data"
        snap.mkdir(parents=True)
        duckdb.connect().execute(
            f"COPY (SELECT 1 AS l_quantity) TO '{snap}/lineitem.parquet' (FORMAT PARQUET)"
        )
        man = build_manifest_from_dir(
            ws,
            [QueryTemplate(qid, f"select sum(l_quantity) + {qid} from lineitem", ())],
            shm_capable=True,
            parquet_dir="data",
            write=False,
        )
        _publish_manifest(d, man)
        (d / "engine.cpp").write_text("int main(){return 0;}")
        ids.add(man.engine_id)
    assert len(ids) == 1  # identical sources -> one shared engine_id

    conn = _conn_with_router()  # empty in-memory connection
    try:
        registered = discover_engines(conn, engines, set(), mount=True)
        # Both distinct packages are discovered: identity includes the publish directory, so the
        # shared engine_id no longer shadows one of them (the old id-only dedup recorded just one).
        assert len(registered) == 2
        assert {k.split("\x1f")[0] for k in registered} == {"aaa-pkg", "zzz-pkg"}
    finally:
        conn.close()


def test_shm_budget_failure_degrades_to_parquet_plane(tmp_path, monkeypatch):
    """When the shm hot-load does not fit in shared memory, an engine that bundles a snapshot
    degrades gracefully to the disk-backed parquet plane instead of failing to bind at all.
    """
    import synnodb.router.process_engine as pe
    from synnodb.errors import EngineResourceError

    class FakeShmNoFit(FakeEngine):
        def ingest(self, tables):
            raise EngineResourceError(
                "not enough shared memory", context={"needed_MiB": 999}
            )

    class FakeProcess(FakeEngine):
        pass

    monkeypatch.setattr(pe, "ShmHotLoadEngine", FakeShmNoFit)
    monkeypatch.setattr(pe, "ProcessEngine", FakeProcess)

    engines = tmp_path / "engines"
    engine_dir = engines / "synno-x"
    ws = _make_workspace(tmp_path)
    snap = engine_dir / "data"
    snap.mkdir(parents=True)
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS l_quantity) TO '{snap}/lineitem.parquet' (FORMAT PARQUET)"
    )
    man = build_manifest_from_dir(
        ws,
        [QueryTemplate("1", "select sum(l_quantity) from lineitem", ())],
        shm_capable=True,
        parquet_dir="data",
        expected_tables={"lineitem": (ColumnSpec("l_quantity", "INTEGER"),)},
        write=False,
    )
    _publish_manifest(engine_dir, man)
    (engine_dir / "engine.cpp").write_text("int main(){return 0;}")

    conn = _conn_with_router()
    try:
        # Live data present, so the shm plane is attempted first - and fails to fit.
        conn.duckdb.execute(
            "CREATE TABLE lineitem(l_quantity INTEGER); INSERT INTO lineitem VALUES (1)"
        )
        registered = discover_engines(conn, engines, set())
        assert registered  # still bound...
        eng = conn.router.registry.bindings()[0].engine
        assert isinstance(eng, FakeProcess)  # ...on the parquet plane, not shm
    finally:
        conn.close()
