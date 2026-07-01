"""Auto-discovery of published bespoke engines.

A finished engine is published into an engines directory as ``<engine_id>/``, holding the
compiled ``db`` binary, the generated sources, and a ``manifest.json``. A SynnoConnection
scans that directory and registers any engine it has not registered yet, so a base
implementation that finishes after ``connect()`` starts serving with no code change.

Registration recomputes each query's output schema and table fingerprint from the live
DuckDB (the source of truth) and is refused for an engine whose tables are absent or differ.
A not-yet-registerable engine (for example, the user has not loaded its tables yet) is left
for the next scan rather than recorded as done, so it registers once the data is in place.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Set

from ..errors import EngineResourceError, SynnoError, SynnoUnsupportedQuery
from ..router.manifest import EngineManifest, register_manifest

log = logging.getLogger("synnodb.discovery")


def resolve_engines_dir(engines: "str | Path | None") -> Optional[Path]:
    """The engines directory: the explicit argument, else ``SYNNO_ENGINES_DIR``, else
    ``$SYNNO_DATA_DIR/engines``. ``None`` when nothing is configured (discovery disabled).

    Reads the environment directly rather than via ``settings.get_data_dir`` so a plain
    drop-in user who never set ``SYNNO_DATA_DIR`` gets no error, just no discovery.
    """
    if engines is not None:
        return Path(engines)
    env = os.getenv("SYNNO_ENGINES_DIR")
    if env:
        return Path(env)
    data = os.getenv("SYNNO_DATA_DIR")
    if data:
        return Path(data) / "engines"
    return None


def _quote_ident(name: str) -> str:
    """A safely-quoted SQL identifier (doubles embedded double-quotes)."""
    return '"' + name.replace('"', '""') + '"'


def _close_quietly(engine: Any) -> None:
    closer = getattr(engine, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _engine_tables(manifest: EngineManifest) -> list:
    """The tables the engine needs present. ``expected_tables`` when set, else parsed from
    the query templates (older manifests)."""
    if manifest.expected_tables:
        return list(manifest.expected_tables.keys())
    from ..router.normalize import tables_in

    tables: set = set()
    for q in manifest.queries:
        tables |= set(tables_in(q.sql_template))
    return sorted(tables)


def _tables_present(conn: Any, tables: "list") -> set:
    """Subset of *tables* that exist (by name, case-insensitive) on the connection."""
    if not tables:
        return set()
    duck = getattr(conn, "duckdb", conn)
    rows = duck.execute(
        "SELECT DISTINCT lower(table_name) FROM information_schema.tables"
    ).fetchall()
    have = {r[0] for r in rows}
    return {t for t in tables if t.lower() in have}


def _resolve_parquet_dir(
    manifest: EngineManifest, engine_dir: Path, *, require_exists: bool = True
) -> Optional[Path]:
    """The engine's bundled/standalone snapshot dir, resolving a relative ``parquet_dir``
    against the engine directory (so a published package is portable). None if not configured
    (or, when *require_exists*, not present on disk).

    A manifest is attacker-controllable data, so a *relative* ``parquet_dir`` must stay inside the
    engine package: a value like ``"../../etc"`` is path traversal that would point the runtime at
    an arbitrary host directory. Such a path is refused loudly rather than resolved.
    """
    if not manifest.parquet_dir:
        return None
    raw = Path(manifest.parquet_dir)
    if raw.is_absolute():
        p = raw
    else:
        engine_root = engine_dir.resolve()
        p = (engine_dir / raw).resolve()
        if p != engine_root and engine_root not in p.parents:
            raise SynnoError(
                f"engine {manifest.engine_id}: parquet_dir {manifest.parquet_dir!r} escapes the "
                f"engine package ({p} is not under {engine_root}); refusing to read it"
            )
    if require_exists and not p.exists():
        return None
    return p


def _engine_extra_env(
    manifest: EngineManifest, threads_override: Optional[int]
) -> dict:
    """The env an engine subprocess runs under, fixing its thread count to the parallelism it
    was built/validated for (``manifest.threads``), or a connect-time override
    (``config={'threads': N}``). Resolved against this machine through the same helper the
    factory uses, so serving runs the engine at the count it was validated at. Empty when
    neither is known (older engines): the engine keeps its own default thread count."""
    threads = threads_override if threads_override is not None else manifest.threads
    if threads is None:
        return {}
    from synnodb.utils.core_utils import core_ids_to_env, get_cores_for_current_machine

    _, core_ids = get_cores_for_current_machine(
        leave_core_0_out=True, allow_hyperthreading=True, ncores_to_use=threads
    )
    return {"CORE_IDS": core_ids_to_env(core_ids)}


def _build_engine(
    manifest: EngineManifest,
    engine_dir: Path,
    *,
    threads_override: Optional[int] = None,
) -> Any:
    """Construct a ``ProcessEngine`` over the engine's snapshot (the parquet/standalone plane).
    Kept as the construction seam so a test can substitute an in-process engine.
    ``synnodb.cpp_runner`` is imported lazily inside the engine, so discovery stays light."""
    from ..router.process_engine import ProcessEngine

    parquet_dir = _resolve_parquet_dir(manifest, engine_dir, require_exists=False)
    if parquet_dir is None:
        raise ValueError(f"manifest for {manifest.engine_id} has no parquet_dir")
    return ProcessEngine(
        manifest.engine_id,
        engine_dir,
        str(parquet_dir),
        extra_env=_engine_extra_env(manifest, threads_override),
    )


def _mount_snapshot_views(conn: Any, tables: "list", parquet_dir: Path) -> None:
    """Expose the engine's own snapshot as views, so a connection with no data of its own can
    serve (and cross-check) the synthesized database."""
    duck = getattr(conn, "duckdb", conn)
    for t in tables:
        pf = parquet_dir / f"{t}.parquet"
        if pf.exists():
            # The path is inlined (a view stores its query text, so it cannot hold a runtime
            # bind parameter); it is engine-controlled, escaped defensively.
            lit = "'" + str(pf).replace("'", "''") + "'"
            duck.execute(
                f"CREATE OR REPLACE VIEW {_quote_ident(t)} AS SELECT * FROM read_parquet({lit})"
            )


def _aligned_arrow(conn: Any, tables: "list") -> dict:
    """Fetch each table from the connection as Arrow for the engine to ingest. The C++ loader
    reads columns by name and canonicalizes their types, so ``SELECT *`` is sufficient and
    safest - no projection (which could drop a column) or cast (which could narrow a value)."""
    duck = getattr(conn, "duckdb", conn)
    return {
        t: duck.execute(f"SELECT * FROM {_quote_ident(t)}").to_arrow_table()
        for t in tables
    }


def _shm_schema_ok(conn: Any, manifest: EngineManifest) -> bool:
    """Whether the live schema is verified to match the engine build, so the shm hot-load may
    serve the connection's own data.

    This gate runs BEFORE ingest for two reasons. First, a mismatch must never write a
    ``/dev/shm`` segment or serve data, so the check cannot be left to ``register_manifest``
    (which runs after ``ingest``). Second, an engine that declares no ``expected_tables`` is
    refused outright: an empty ``expected_tables`` makes ``register_manifest``'s compatibility
    check a no-op, so the engine would ingest and serve whatever same-named tables the live DB
    happens to hold - silently wrong data. A shm-capable engine must declare its schema.
    """
    from ..router.manifest import check_compatibility

    if not manifest.expected_tables:
        log.warning(
            "engine %s is shm_capable but declares no expected_tables; refusing the shm hot-load "
            "plane - cannot verify the live schema matches the engine build, so it could serve "
            "wrong data. Republish the engine with expected_tables.",
            manifest.engine_id,
        )
        return False
    problems = check_compatibility(conn, manifest)
    if problems:
        log.info(
            "engine %s: live schema incompatible with the shm hot-load build, not ingesting: %s",
            manifest.engine_id,
            "; ".join(problems),
        )
        return False
    return True


def _bind_engine(
    conn: Any,
    manifest: EngineManifest,
    engine_dir: Path,
    *,
    mount: bool,
    threads_override: Optional[int] = None,
) -> Any:
    """Choose and build an engine for *manifest* against *conn*, or return ``None`` when it is
    not servable yet (data not loaded and no snapshot to mount).

    Plane selection: serve the connection's own live data over the shm hot-load when the
    engine is shm-capable, its tables are present, and its schema is verified to match the
    engine build (:func:`_shm_schema_ok`); otherwise serve the engine's bundled snapshot via a
    standalone ``ProcessEngine`` (mounting it as views first when asked). Both planes run the
    engine at *threads_override* / the manifest's recorded thread count (:func:`_engine_extra_env`).
    """
    from ..router.process_engine import ShmHotLoadEngine

    tables = _engine_tables(manifest)
    present = _tables_present(conn, tables)

    if (
        manifest.shm_capable
        and set(tables) <= present
        and _shm_schema_ok(conn, manifest)
    ):
        engine = ShmHotLoadEngine(
            manifest.engine_id,
            engine_dir,
            extra_env=_engine_extra_env(manifest, threads_override),
        )
        try:
            engine.ingest(_aligned_arrow(conn, tables))
        except EngineResourceError:
            # Shared memory is too small for the hot-load. Degrade gracefully to the disk-backed
            # parquet plane when the engine bundles a snapshot; otherwise the failure propagates so
            # the scan reports it (a shm-only engine has no lower-memory plane to fall back to).
            _close_quietly(engine)
            if not manifest.parquet_dir:
                raise
            log.warning(
                "engine %s: shm hot-load did not fit; serving from the disk-backed parquet "
                "plane instead",
                manifest.engine_id,
            )
        else:
            return engine

    if manifest.parquet_dir:  # the standalone parquet plane is configured
        if mount:
            snapshot = _resolve_parquet_dir(manifest, engine_dir, require_exists=True)
            missing = [t for t in tables if t not in present]
            if snapshot is not None and missing:
                _mount_snapshot_views(conn, missing, snapshot)
                present = _tables_present(conn, tables)
        if set(tables) <= present:
            engine = _build_engine(
                manifest, engine_dir, threads_override=threads_override
            )
            return engine
    return None


def _promote_to_shm(
    conn: Any,
    manifest: EngineManifest,
    engine_dir: Path,
    *,
    threads_override: Optional[int] = None,
) -> bool:
    """Re-bind an already-registered engine from the parquet/mounted plane to the faster shm
    hot-load, once the connection's live tables are present and verified to match the build.

    The data plane is otherwise frozen at first bind: an engine that mounted its bundled snapshot
    (no live data) would never move to serving the connection's own data even after it loads.
    Returns True if a promotion happened. Safe and idempotent - a no-op once the engine is already
    on shm, not shm-capable, or its live tables are absent/incompatible.
    """
    if not manifest.shm_capable:
        return False
    from ..router.process_engine import ShmHotLoadEngine

    registry = getattr(getattr(conn, "router", None), "registry", None)
    if registry is None:
        return False
    current = next(
        (
            b.engine
            for b in registry.bindings()
            if b.engine_id == manifest.engine_id
            and Path(getattr(b.engine, "workspace", "")) == engine_dir
        ),
        None,
    )
    if current is None or isinstance(current, ShmHotLoadEngine):
        return False  # not bound here, or already on the shm plane
    tables = _engine_tables(manifest)
    if not (
        set(tables) <= _tables_present(conn, tables) and _shm_schema_ok(conn, manifest)
    ):
        return False
    engine = ShmHotLoadEngine(
        manifest.engine_id,
        engine_dir,
        extra_env=_engine_extra_env(manifest, threads_override),
    )
    engine.ingest(_aligned_arrow(conn, tables))
    try:
        register_manifest(
            conn, manifest, engine, strict=True
        )  # replaces the package's bindings
    except Exception as exc:
        _close_quietly(engine)
        log.debug(
            "engine %s shm promotion failed, keeping current plane: %s",
            manifest.engine_id,
            exc,
        )
        return False
    _close_quietly(current)  # release the superseded parquet-plane engine
    log.info(
        "engine %s promoted to the shm hot-load plane (live data now present)",
        manifest.engine_id,
    )
    return True


def discover_engines(
    conn: Any,
    engines_dir: Path,
    registered: Set[str],
    *,
    mount: bool = False,
    threads_override: Optional[int] = None,
) -> Set[str]:
    """Register engines under *engines_dir* that are not already in *registered*.

    Returns the updated set of registered engine ids. A published engine is a subdirectory
    containing ``manifest.json``; ``.``-prefixed staging directories (atomic publish) are
    skipped. An engine that cannot be served yet (its data is not loaded and it has no
    mountable snapshot) is left out of the returned set, so a later scan retries it. With
    *mount*, an engine's bundled snapshot is exposed as views when the connection lacks its
    tables (the synthesized-database path).
    """
    registered = set(registered)
    try:
        children = sorted(p for p in engines_dir.iterdir() if p.is_dir())
    except OSError:
        return registered  # directory does not exist yet
    for child in children:
        if child.name.startswith("."):  # .tmp-* staging directory
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = EngineManifest.read(manifest_path)
        except Exception as exc:
            log.warning("ignoring unreadable manifest at %s: %s", manifest_path, exc)
            continue
        # Identity is the published package (its directory) AND its content engine_id, not the
        # engine_id alone: two packages built from identical sources share an engine_id but are
        # distinct (e.g. one bundles a snapshot, the other does not), so dedup-by-id would shadow
        # the only servable one. Including the engine_id still lets a re-publish at the same name
        # (new content -> new id) be re-discovered, and re-binds the same package only when needed.
        key = f"{child.name}\x1f{manifest.engine_id}"
        if key in registered:
            # upgrade to shm if live data is now present
            _promote_to_shm(conn, manifest, child, threads_override=threads_override)
            continue
        try:
            engine = _bind_engine(
                conn, manifest, child, mount=mount, threads_override=threads_override
            )
            if engine is None:
                continue  # not servable yet; retry on a later scan
            try:
                register_manifest(conn, manifest, engine, strict=True)
            except Exception:
                # The engine may already hold resources (a shm hot-load ingested its data);
                # release them so a registration failure does not leak a warm subprocess or
                # /dev/shm segments before re-raising into the outer handler.
                _close_quietly(engine)
                raise
        except SynnoUnsupportedQuery as exc:
            # A permanent refusal (a query's output types are outside the exact envelope), not a
            # transient "data not loaded yet". Surface it loudly and record the package so it is
            # neither retried nor re-logged each scan; the query is correctly served by DuckDB.
            log.warning(
                "engine %s at %s will not be routed - %s",
                manifest.engine_id,
                child,
                exc,
            )
            registered.add(key)
            continue
        except SynnoError as exc:
            # A misconfigured/unsafe package (e.g. a parquet_dir that escapes the engine dir):
            # a real problem the operator should see, not a transient retry.
            log.warning("engine %s at %s skipped - %s", manifest.engine_id, child, exc)
            registered.add(key)
            continue
        except Exception as exc:
            # Not compatible with this DB yet (or not buildable). Skip without recording it,
            # so it is retried on a later scan; a query never crashes over discovery.
            log.debug(
                "engine %s at %s not registered (will retry): %s",
                manifest.engine_id,
                child,
                exc,
            )
            continue
        registered.add(key)
        log.info(
            "registered bespoke engine %s (%d queries) from %s",
            manifest.engine_id,
            len(manifest.queries),
            child,
        )
    return registered
