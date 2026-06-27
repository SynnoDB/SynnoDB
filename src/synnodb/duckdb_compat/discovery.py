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


def _build_engine(manifest: EngineManifest, engine_dir: Path) -> Any:
    """A ProcessEngine over the published engine. ``synnodb.cpp_runner`` is imported lazily
    inside the engine (only when it actually runs), so discovery stays light."""
    from ..router.process_engine import ProcessEngine

    if not manifest.parquet_dir:
        raise ValueError(f"manifest for {manifest.engine_id} has no parquet_dir")
    return ProcessEngine(manifest.engine_id, engine_dir, manifest.parquet_dir)


def discover_engines(conn: Any, engines_dir: Path, registered: Set[str]) -> Set[str]:
    """Register engines under *engines_dir* that are not already in *registered*.

    Returns the updated set of registered engine ids. A published engine is a subdirectory
    containing ``manifest.json``; ``.``-prefixed staging directories (atomic publish) are
    skipped. An engine that cannot be registered against the live DB is logged and left out
    of the returned set, so a later scan retries it once the schema is compatible.
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
        if manifest.engine_id in registered:
            continue
        try:
            engine = _build_engine(manifest, child)
            register_manifest(conn, manifest, engine, strict=True)
        except Exception as exc:
            # Not compatible with this DB yet (or not buildable). Skip without recording it,
            # so it is retried on a later scan; a query never crashes over discovery.
            log.debug("engine %s at %s not registered: %s", manifest.engine_id, child, exc)
            continue
        registered.add(manifest.engine_id)
        log.info(
            "registered bespoke engine %s (%d queries) from %s",
            manifest.engine_id, len(manifest.queries), child,
        )
    return registered
