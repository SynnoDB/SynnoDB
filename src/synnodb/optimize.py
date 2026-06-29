"""Build a SynnoDB optimization for an existing DuckDB database.

``optimize_database`` loads a ``.db`` file into memory, derives router templates for the
chosen queries from the workload, and publishes an already-built engine as
``synno-<dbname>`` so the drop-in routes those queries to it. The published engine can carry
a bundled parquet snapshot (the self-contained, standalone plane) and/or be marked
shm-capable (the in-memory Arrow hot-load) - by default both (``data_plane="auto"``).

Generating *new* C++ for a query that has no engine yet is the agent factory's job
(``synnodb.main``); this binds an already-built engine workspace to a database.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .errors import SynnoUnsupportedQuery

log = logging.getLogger("synnodb.optimize")

DATA_PLANES = ("auto", "parquet", "shm")


def _resolve_benchmark(benchmark: Any, workload_enum: Any) -> Any:
    """Resolve a benchmark name (``"tpch"``) or enum to the workload enum, with a clear error."""
    if isinstance(benchmark, workload_enum):
        return benchmark
    try:
        return workload_enum(benchmark)  # by value, e.g. "tpch"
    except ValueError:
        pass
    try:
        return workload_enum[str(benchmark).upper()]  # by name, e.g. "TPCH"
    except KeyError:
        valid = ", ".join(m.value for m in workload_enum)
        raise ValueError(f"unknown benchmark {benchmark!r}; expected one of: {valid}")


def optimize_database(
    database: "str | Path",
    query_ids: Sequence[str],
    *,
    engine_workspace: "str | Path",
    benchmark: str = "tpch",
    engines_dir: "str | Path | None" = None,
    name: Optional[str] = None,
    data_plane: str = "auto",
    scale_factor: Optional[float] = None,
    force: bool = False,
) -> Path:
    """Publish ``synno-<dbname>`` for *database*, routing *query_ids* to *engine_workspace*.

    *data_plane* selects what the published engine can do: ``"shm"`` hot-loads its tables from
    the connected in-memory DuckDB; ``"parquet"`` bundles a standalone snapshot exported from
    the database; ``"auto"`` (default) does both. Returns the published engine directory.

    The default name is ``synno-<db stem>``; *force* overwrites an existing engine of that name
    even when it was built for a *different* database (otherwise a collision is refused, since two
    ``tpch.db`` files in different directories would both default to ``synno-tpch``).
    """
    import duckdb as _duckdb

    from .duckdb_compat.db_io import export_tables_to_parquet, load_database_into_memory
    from .duckdb_compat.discovery import resolve_engines_dir
    from .router.manifest import EngineManifest
    from .router.normalize import tables_in
    from .router.registry import ColumnSpec
    from .utils.utils import DBStorage
    from .workloads.engine_publish import _BRACKET, _lookup_template, publish_from_provider
    from .workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

    if data_plane not in DATA_PLANES:
        raise ValueError(f"data_plane must be one of {DATA_PLANES}, got {data_plane!r}")
    database = Path(database)
    engine_workspace = Path(engine_workspace)
    query_ids = [str(q) for q in query_ids]
    if not (engine_workspace / "db").exists():
        raise FileNotFoundError(
            f"engine workspace {engine_workspace} has no compiled 'db' binary - build the "
            "engine first (the agent factory) before binding it to a database"
        )
    target = resolve_engines_dir(engines_dir)
    if target is None:
        raise ValueError(
            "no engines directory: pass engines_dir or set SYNNO_ENGINES_DIR / SYNNO_DATA_DIR"
        )
    name = name or f"synno-{database.stem}"
    source_db = str(database.resolve())
    # Refuse to silently clobber an engine of the same name built for a different database.
    existing_manifest = target / name / "manifest.json"
    if existing_manifest.exists() and not force:
        try:
            prev = EngineManifest.read(existing_manifest)
        except Exception:
            prev = None
        if prev is not None and prev.source_db and prev.source_db != source_db:
            raise SynnoUnsupportedQuery(
                [f"an engine named '{name}' already exists, built for a different database",
                 f"existing: {prev.source_db}",
                 f"new:      {source_db}"],
                engine_id=name,
            )
    bench = _resolve_benchmark(benchmark, OLAPWorkload)

    inner = _duckdb.connect(":memory:")
    bundle: Optional[Path] = None
    try:
        loaded = load_database_into_memory(inner, database)
        provider = OLAPWorkloadProvider(
            benchmark=bench, base_parquet_dir=database.parent, db_storage=DBStorage.IN_MEMORY,
            bespoke_ssd_storage_dir=None, query_ids=query_ids,
        )

        # The tables the chosen queries actually read, intersected with what the database holds.
        # Replace each ``[NAME]`` marker with a neutral literal so the template parses
        # regardless of the workload generator (no dependence on drawing a sample).
        present = {t.lower(): t for t in loaded}
        referenced: set = set()
        for qid in query_ids:
            bracket = _lookup_template(provider.sql_dict, qid)
            if not bracket:
                continue
            concrete = _BRACKET.sub("1", bracket)
            referenced |= {t.lower() for t in tables_in(concrete)}

        # Every referenced table must be present. Publishing with a partial expected_tables
        # would ship an engine that declares fewer tables than its queries read; the shm plane
        # then cannot verify the missing table's schema and could serve wrong data (the C1 gap),
        # and the query would in any case be wrong. Refuse loudly instead.
        if not referenced:
            raise SynnoUnsupportedQuery(
                ["the selected queries reference no base tables, so there is nothing to optimize"],
                engine_id=name,
            )
        missing = sorted(t for t in referenced if t not in present)
        if missing:
            raise SynnoUnsupportedQuery(
                [f"table '{t}' is referenced by the selected queries but is not in "
                 f"{database.name}" for t in missing],
                engine_id=name,
            )
        expected_tables = {}
        for t in sorted(referenced):
            cols = inner.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE lower(table_name) = ? ORDER BY ordinal_position",
                [t],
            ).fetchall()
            expected_tables[present[t]] = tuple(ColumnSpec(n, str(dt)) for n, dt in cols)

        if data_plane in ("parquet", "auto"):
            bundle = Path(tempfile.mkdtemp(prefix="synno-snapshot-"))
            export_tables_to_parquet(inner, list(expected_tables.keys()), bundle)

        dest = publish_from_provider(
            engine_workspace, provider, query_ids,
            engines_dir=str(target), name=name,
            shm_capable=data_plane in ("shm", "auto"),
            bundle_parquet_dir=str(bundle) if bundle is not None else None,
            expected_tables=expected_tables,
            scale_factor=scale_factor,
            source_db=source_db,
        )
        if dest is None:
            raise RuntimeError("publish produced no engine (no routable templates derived)")
        log.info("optimized %s -> %s (tables=%s, plane=%s)",
                 database, dest, sorted(expected_tables), data_plane)
        return dest
    finally:
        inner.close()
        if bundle is not None:
            shutil.rmtree(bundle, ignore_errors=True)


def cli(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="synnodb-optimize",
        description="Build a SynnoDB optimization (synno-<dbname>) for an existing DuckDB database.",
    )
    ap.add_argument("database", help="path to the DuckDB .db file")
    ap.add_argument("--query", "-q", required=True, help="comma-separated query ids, e.g. 1,6")
    ap.add_argument("--engine-workspace", required=True, help="a built engine workspace (has ./db)")
    ap.add_argument("--engines-dir", default=None, help="where to publish (default: SYNNO_ENGINES_DIR)")
    ap.add_argument("--name", default=None, help="published name (default: synno-<dbname>)")
    from .workloads.workload_provider_olap import OLAPWorkload

    ap.add_argument("--benchmark", default="tpch", choices=[m.value for m in OLAPWorkload])
    ap.add_argument("--data-plane", default="auto", choices=DATA_PLANES)
    ap.add_argument("--scale-factor", type=float, default=None)
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing engine of the same name even if built for a different DB")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dest = optimize_database(
        args.database, args.query.split(","), engine_workspace=args.engine_workspace,
        benchmark=args.benchmark, engines_dir=args.engines_dir, name=args.name,
        data_plane=args.data_plane, scale_factor=args.scale_factor, force=args.force,
    )
    print(dest)


if __name__ == "__main__":
    cli()
