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
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Sequence

from .errors import SynnoUnsupportedQuery

if TYPE_CHECKING:
    from .workloads.validation_receipt import ValidationReceipt

log = logging.getLogger("synnodb.optimize")

DATA_PLANES = ("auto", "parquet", "shm")


def _resolve_benchmark(benchmark: Any) -> Any:
    """Resolve a registered workload name (``"tpch"``) to its identity, with a clear error.

    The core ships no built-in workloads, so *benchmark* must name a workload registered from
    the outside (register_workload(...) or a bring-your-own builder). A already-resolved
    Workload/WorkloadId passes through unchanged."""
    from .workloads.workload_provider import Workload, WorkloadId
    from .workloads.workload_spec import resolve_workload

    if isinstance(benchmark, (Workload, WorkloadId)):
        return benchmark
    return resolve_workload(str(benchmark))


def _validate_engine_against_source(
    engine_workspace: Path,
    provider: Any,
    query_ids: Sequence[str],
    inner: Any,
    *,
    planes: Sequence[str],
    bundle: Optional[Path],
    expected_tables: Mapping[str, Any],
    dataset: str,
    scale_factor: Optional[float],
) -> "ValidationReceipt":
    """Cross-check the built engine against the source DuckDB it was optimized from, and return a
    pass :class:`ValidationReceipt` for the publish gate.

    The optimizer has no agent-loop ``QueryValidator``, but it holds the source database in memory
    (``inner``) - the perfect oracle. For each requested data plane we run every publish query
    through the runtime engine path (the router's ``ProcessEngine`` / ``ShmHotLoadEngine``) and
    compare its Arrow output to DuckDB's, exactly as the router's live cross-check does. This runs
    the actual binary against ground truth, so it is a stronger proof than a template marker check.
    Raises on the first divergence or execution failure, so the caller publishes nothing.
    """
    from .router.adapt import results_diff, results_equal
    from .router.backend import DuckDBBackend
    from .router.normalize import has_order_by, order_by_key_indices
    from .router.process_engine import ProcessEngine, ShmHotLoadEngine
    from .workloads.engine_publish import (
        _BRACKET,
        _lookup_template,
        _sample_assignments,
    )
    from .workloads.query_params import substitute
    from .workloads.validation_receipt import (
        PASS,
        PLANE_PARQUET,
        ValidatedQuery,
        ValidationReceipt,
        engine_build_ids,
    )

    backend = DuckDBBackend(inner)

    # Concrete bindings per query, drawn from the workload generator. A constant query (no
    # placeholders) validates as itself, with a single empty binding. The same bindings are used
    # across planes so the receipt's stated coverage is consistent with what every plane proved.
    bracket_by_qid: dict[str, str] = {}
    bindings_by_qid: dict[str, list[dict]] = {}
    for qid in query_ids:
        bracket = _lookup_template(provider.sql_dict, qid)
        if not bracket:
            raise SynnoUnsupportedQuery(
                [f"no query template for '{qid}' in the {dataset} workload"],
                engine_id=qid,
            )
        samples = _sample_assignments(provider, qid, 2)
        if not samples and _BRACKET.search(bracket):
            raise SynnoUnsupportedQuery(
                [
                    f"could not sample any parameter bindings for query '{qid}'; cannot validate it"
                ],
                engine_id=qid,
            )
        bracket_by_qid[qid] = bracket
        bindings_by_qid[qid] = samples or [{}]

    def _cross_check(engine: Any) -> None:
        for qid in query_ids:
            bracket = bracket_by_qid[qid]
            for binding in bindings_by_qid[qid]:
                concrete = substitute(bracket, binding)
                reference = backend.execute_arrow(concrete)
                table, _server_ms = engine.run(qid, binding)
                ordered = has_order_by(concrete)
                order_keys = (
                    order_by_key_indices(concrete, reference.column_names)
                    if ordered
                    else None
                )
                if not results_equal(
                    table, reference, ordered=ordered, order_keys=order_keys
                ):
                    diffs, total = results_diff(
                        table, reference, ordered=ordered, order_keys=order_keys
                    )
                    raise RuntimeError(
                        f"engine output for query {qid} does not match the source database for "
                        f"bindings {binding}: {total} differing cell(s), first {diffs[:5]}"
                    )

    engine_id = f"optimize-validate-{engine_workspace.name}"
    for plane in planes:
        if plane == PLANE_PARQUET:
            assert bundle is not None, (
                "parquet plane validation needs an exported snapshot"
            )
            engine: Any = ProcessEngine(engine_id, engine_workspace, bundle)
        else:
            engine = ShmHotLoadEngine(engine_id, engine_workspace)
            engine.ingest(
                {
                    t: backend.execute_arrow(f'SELECT * FROM "{t}"')
                    for t in expected_tables
                }
            )
        try:
            _cross_check(engine)
        finally:
            engine.close()

    validated_queries = tuple(
        ValidatedQuery(qid, tuple(bindings_by_qid[qid])) for qid in query_ids
    )
    return ValidationReceipt(
        snapshot_id=None,  # the optimizer has no git snapshotter; build_ids are the identity check
        build_ids=engine_build_ids(engine_workspace),
        validated_queries=validated_queries,
        coverage_policy=(
            "engine output cross-checked against the source DuckDB for sampled bindings per query"
        ),
        data_planes=tuple(planes),
        dataset=dataset,
        validated_scale_factors=(float(scale_factor),)
        if scale_factor is not None
        else (),
        mode="optimizer-crosscheck",
        live_run=True,
        verdict=PASS,
    )


def optimize_database(
    database: "str | Path",
    query_ids: Sequence[str],
    *,
    engine_workspace: "str | Path",
    benchmark: str,
    engines_dir: "str | Path | None" = None,
    name: Optional[str] = None,
    data_plane: str = "auto",
    scale_factor: Optional[float] = None,
    threads: Optional[int] = None,
    force: bool = False,
) -> Path:
    """Publish ``synno-<dbname>`` for *database*, routing *query_ids* to *engine_workspace*.

    *data_plane* selects what the published engine can do: ``"shm"`` hot-loads its tables from
    the connected in-memory DuckDB; ``"parquet"`` bundles a standalone snapshot exported from
    the database; ``"auto"`` (default) does both. Returns the published engine directory.

    *threads* records the degree of parallelism the engine was built/validated for; the runtime
    serves it at that thread count (a ``synnodb.connect(config={'threads': N})`` overrides it).

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
    from .workloads.engine_publish import (
        _BRACKET,
        _lookup_template,
        publish_from_provider,
    )
    from .workloads.workload_provider_olap import OLAPWorkloadProvider

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
                [
                    f"an engine named '{name}' already exists, built for a different database",
                    f"existing: {prev.source_db}",
                    f"new:      {source_db}",
                ],
                engine_id=name,
            )
    bench = _resolve_benchmark(benchmark)

    inner = _duckdb.connect(":memory:")
    bundle: Optional[Path] = None
    try:
        loaded = load_database_into_memory(inner, database)
        provider = OLAPWorkloadProvider(
            benchmark=bench,
            base_parquet_dir=database.parent,
            db_storage=DBStorage.IN_MEMORY,
            bespoke_ssd_storage_dir=None,
            query_ids=query_ids,
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
                [
                    "the selected queries reference no base tables, so there is nothing to optimize"
                ],
                engine_id=name,
            )
        missing = sorted(t for t in referenced if t not in present)
        if missing:
            raise SynnoUnsupportedQuery(
                [
                    f"table '{t}' is referenced by the selected queries but is not in "
                    f"{database.name}"
                    for t in missing
                ],
                engine_id=name,
            )
        expected_tables = {}
        for t in sorted(referenced):
            cols = inner.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE lower(table_name) = ? ORDER BY ordinal_position",
                [t],
            ).fetchall()
            expected_tables[present[t]] = tuple(
                ColumnSpec(n, str(dt)) for n, dt in cols
            )

        if data_plane in ("parquet", "auto"):
            bundle = Path(tempfile.mkdtemp(prefix="synno-snapshot-"))
            export_tables_to_parquet(inner, list(expected_tables.keys()), bundle)

        # Gate the publish on a real cross-check of the built engine against the source DuckDB,
        # over every plane being published. Refuses (raising, publishing nothing) on any divergence
        # or execution failure.
        planes = []
        if data_plane in ("parquet", "auto"):
            planes.append("parquet")
        if data_plane in ("shm", "auto"):
            planes.append("shm")
        receipt = _validate_engine_against_source(
            engine_workspace,
            provider,
            query_ids,
            inner,
            planes=planes,
            bundle=bundle,
            expected_tables=expected_tables,
            dataset=bench.value,
            scale_factor=scale_factor,
        )

        dest = publish_from_provider(
            engine_workspace,
            provider,
            query_ids,
            receipt=receipt,
            engines_dir=str(target),
            name=name,
            shm_capable=data_plane in ("shm", "auto"),
            bundle_parquet_dir=str(bundle) if bundle is not None else None,
            expected_tables=expected_tables,
            scale_factor=scale_factor,
            source_db=source_db,
            threads=threads,
        )
        if dest is None:
            raise RuntimeError(
                "publish produced no engine (no routable templates derived)"
            )
        log.info(
            "optimized %s -> %s (tables=%s, plane=%s)",
            database,
            dest,
            sorted(expected_tables),
            data_plane,
        )
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
    ap.add_argument(
        "--query", "-q", required=True, help="comma-separated query ids, e.g. 1,6"
    )
    ap.add_argument(
        "--engine-workspace", required=True, help="a built engine workspace (has ./db)"
    )
    ap.add_argument(
        "--engines-dir",
        default=None,
        help="where to publish (default: SYNNO_ENGINES_DIR)",
    )
    ap.add_argument(
        "--name", default=None, help="published name (default: synno-<dbname>)"
    )
    ap.add_argument(
        "--benchmark",
        required=True,
        help="Name of a registered workload whose query templates the engine serves. The core "
        "ships no built-in workloads; register the one you want first (register_workload(...) "
        "or a bring-your-own builder).",
    )
    ap.add_argument("--data-plane", default="auto", choices=DATA_PLANES)
    ap.add_argument("--scale-factor", type=float, default=None)
    ap.add_argument(
        "--threads",
        type=int,
        default=None,
        help="degree of parallelism the engine was built/validated for; the runtime "
        "serves it at this thread count (overridable per connection via "
        "config={'threads': N}). Default: the engine's own default.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing engine of the same name even if built for a different DB",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dest = optimize_database(
        args.database,
        args.query.split(","),
        engine_workspace=args.engine_workspace,
        benchmark=args.benchmark,
        engines_dir=args.engines_dir,
        name=args.name,
        data_plane=args.data_plane,
        scale_factor=args.scale_factor,
        threads=args.threads,
        force=args.force,
    )
    print(dest)


if __name__ == "__main__":
    cli()
