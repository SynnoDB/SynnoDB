from __future__ import annotations

import functools
import logging
import os
import random
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List

from cpp_runner.compiler.compiler_utils import make_compiler
from observability.benchmark.systems.base import SystemRunner
from observability.benchmark.writer import BenchmarkWriter
from observability.logging.logger import setup_logging
from observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from run_gen_base_impl import validate_snapshot
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool
from tools.validate.query_validator_class import format_args_string
from utils.utils import DBStorage, create_dir_and_set_permissions, parse_db_storage
from workloads.dataset.dataset_tables_dict import get_dataset_name
from workloads.dataset.query_gen_factory import get_query_gen

if TYPE_CHECKING:
    from synth_framework.git_snapshotter import GitSnapshotter
    from tools.run import RunTool

logger = logging.getLogger(__name__)

AVAILABLE_SYSTEMS = ("bespoke", "clickhouse", "duckdb", "umbra")


def _filter_query_ids(all_ids: List[str], query_ids: str | None) -> List[str]:
    if not query_ids:
        return all_ids
    if query_ids.strip().lower() == "all":
        return all_ids

    def _normalize_query_id(raw: str) -> str:
        qid = raw.strip().lower()
        if qid.startswith("q"):
            qid = qid[1:]
        m = re.fullmatch(r"0*(\d+)([a-z]?)", qid)
        if m:
            num, suffix = m.groups()
            return f"{int(num)}{suffix}"
        return qid

    requested: list[str] = []
    for part in query_ids.split(","):
        part = part.strip()
        if not part:
            continue
        requested.append(_normalize_query_id(part))
    if not requested:
        return all_ids
    requested_set = set(requested)
    filtered = [qid for qid in all_ids if _normalize_query_id(qid) in requested_set]
    if not filtered:
        available = ", ".join(all_ids[:30])
        raise ValueError(
            f"No matching query IDs found for: {query_ids}. "
            f"Available in snapshot: {available}"
        )
    return filtered


def _parse_num_threads(raw: str) -> List[int]:
    threads = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        val = int(part)
        if val < 1:
            raise ValueError(f"Thread count must be >= 1, got {val}")
        threads.append(val)
    if not threads:
        raise ValueError("num_threads list is empty.")
    return threads


def _parse_scale_factors(raw: str) -> List[float]:
    parts = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        if "." in part:
            parts.append(float(part))
        else:
            parts.append(int(part))
    if not parts:
        raise ValueError("scale_factors list is empty.")
    return parts


def _parse_systems(raw: str) -> list[str]:
    """Parse comma-separated system names, lower-cased."""
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _resolve_system(args) -> str:
    if getattr(args, "system", None):
        system_name = args.system.strip().lower()
        if system_name not in AVAILABLE_SYSTEMS:
            raise ValueError(
                f"Unknown system '{system_name}'. Available: {', '.join(AVAILABLE_SYSTEMS)}"
            )
        return system_name

    systems = _parse_systems(getattr(args, "systems", "") or "")
    if len(systems) == 1:
        system_name = systems[0]
        if system_name not in AVAILABLE_SYSTEMS:
            raise ValueError(
                f"Unknown system '{system_name}'. Available: {', '.join(AVAILABLE_SYSTEMS)}"
            )
        return system_name
    if len(systems) > 1:
        raise ValueError(
            "Run one system at a time. Use --system <name> and write each run "
            "to its own CSV, then combine CSVs with the plot command."
        )
    raise ValueError("Provide --system <name>.")


def _build_runner(
    system_name: str,
    db_engine: RunTool | None,
    snapshotter: GitSnapshotter | None,
    parquet_path: Path,
    benchmark: str,
    scale_factors: list,
    db_storage: DBStorage,
    disk_db_dir: Path | None = None,
    num_threads: int = 1,
) -> SystemRunner:
    if system_name == "bespoke":
        from observability.benchmark.systems.bespoke import BespokeRunner

        assert db_engine is not None, "db_engine required for BespokeRunner"
        assert snapshotter is not None, "snapshotter required for BespokeRunner"
        return BespokeRunner(db_engine=db_engine, snapshotter=snapshotter)

    if system_name == "duckdb":
        from observability.benchmark.systems.duckdb import DuckDBRunner

        return DuckDBRunner(
            parquet_path=parquet_path,
            benchmark=benchmark,
            num_threads=num_threads,
            db_storage=db_storage,
            disk_db_dir=disk_db_dir,
        )

    if system_name == "umbra":
        from observability.benchmark.systems.umbra import UmbraRunner

        return UmbraRunner(
            parquet_path=parquet_path,
            benchmark=benchmark,
            scale_factors=scale_factors,
            container_num_cores=num_threads,
            allow_auto_restarts=True,
            setup=True,
            db_storage=db_storage,
            disk_db_dir=disk_db_dir,
        )

    if system_name == "clickhouse":
        from observability.benchmark.systems.clickhouse import ClickHouseRunner

        assert db_storage == DBStorage.IN_MEMORY, (
            "ClickHouseRunner currently only supports in-memory DB source"
        )

        return ClickHouseRunner(
            parquet_path=parquet_path,
            benchmark=benchmark,
            scale_factors=scale_factors,
            container_num_cores=num_threads,
        )

    raise ValueError(
        f"Unknown system '{system_name}'. Available: {', '.join(AVAILABLE_SYSTEMS)}"
    )


def _write_header(writer: BenchmarkWriter) -> None:
    writer.write_header_if_needed(
        [
            "query_id",
            "scale_factor",
            "benchmark",
            "system",
            "num_threads",
            "time_ms",
            "hostname",
            "snapshot",
        ]
    )


def run_benchmark(args) -> None:
    old_umask = os.umask(0)
    try:
        try:
            from agents.tracing import set_tracing_disabled

            set_tracing_disabled(True)
        except ImportError:
            pass

        setup_logging(logging.DEBUG, logfile=None)
        system_name = _resolve_system(args)
        use_snapshots = system_name == "bespoke"

        snapshots: list[str] = []
        wandb_run_ids: list[str] = []
        if getattr(args, "snapshots", None):
            snapshots = [s.strip() for s in args.snapshots.split(",") if s.strip()]
        if getattr(args, "wandb_ids", None):
            wandb_run_ids = [s.strip() for s in args.wandb_ids.split(",") if s.strip()]

        assert not len(wandb_run_ids) > 0 or not len(snapshots), (
            "Provide either --snapshots or --wandb_ids, not both."
        )

        if use_snapshots and (not snapshots and not wandb_run_ids):
            raise ValueError(
                "Provide --snapshots or --wandb_ids when benchmarking the bespoke system."
            )

        # out_path = Path(__file__).parent / "output"
        out_path = Path(__file__).parent.parent.parent / "output"
        assert out_path.is_dir(), f"Expected output directory at {out_path}"
        snapshotter: GitSnapshotter | None = None
        if use_snapshots:
            snapshotter = GitSnapshotter(
                cache_repo=None
                if args.disable_repo_sync
                else "git://c01/bespoke_cache.git",
                working_dir=out_path,
                extra_gitignore=[],
            )
            snapshotter.fetch_snapshots()

        host = socket.gethostname()

        scale_factors = _parse_scale_factors(args.scale_factors)
        logger.info(f"Scale factors: {', '.join(map(str, scale_factors))}")
        parquet_path = (
            Path(args.artifacts_dir) / f"{get_dataset_name(args.benchmark)}_parquet"
        )
        logger.info(f"Parquet path: {parquet_path.as_posix()}")

        query_ids = get_all_query_ids(args.benchmark)

        # prepare query generator
        gen_query_fn = get_query_gen(args.benchmark)

        num_threads_list = _parse_num_threads(getattr(args, "num_threads", "1") or "1")

        if system_name == "bespoke":
            assert snapshotter is not None
            parquet_dir = (
                args.base_parquet_dir + f"/{get_dataset_name(args.benchmark)}_parquet/"
            )

            assert isinstance(args.db_storage, DBStorage), (
                f"Expected DBStorage for bespoke system, got type {type(args.db_storage)}"
            )
            compiler = make_compiler(
                cwd=out_path,
                db_storage=args.db_storage,
                untracked_cpp_runner_content="",
            )
            db_engine = RunTool(
                cwd=out_path,
                query_validator=None,
                dataset_name=get_dataset_name(args.benchmark),
                base_parquet_dir=parquet_dir,
                run_stats_collector=None,
                db_storage=args.db_storage,
                compiler=compiler,
            )
        else:
            db_engine = None

        query_ids = _filter_query_ids(query_ids, args.query_ids)

        if use_snapshots:
            if len(snapshots) > 0:
                run_snapshots = snapshots
            else:
                # retrieve snapshot names from wandb run IDs
                run_snapshots = []
                is_mt = []
                db_storage = []
                for wandb_id in wandb_run_ids:
                    statistics, config, _ = wandb_retrieve_metrics_for_run(
                        args.benchmark, wandb_id, fetch_latest_runtimes=False
                    )
                    validate_snapshot(
                        config,
                        args.benchmark,
                        None,
                        query_ids,
                        model=None,
                        db_storage=args.db_storage,
                    )

                    run_snapshots.append(statistics["code/snapshot_hash"])
                    is_mt.append(config["conv_mode"] == "mt")
                    db_storage.append(parse_db_storage(config["db_storage"].lower()))
        else:
            run_snapshots = [""]
            is_mt = [None]
            db_storage = [None]

        if args.csv is None:
            # assemble output file
            csv_dir = Path(args.artifacts_dir) / "benchmark_logs"
            create_dir_and_set_permissions(csv_dir)

            # filename format: date_time_system_benchmark.csv
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{system_name}_{args.benchmark}.csv"

            csv_path = csv_dir / filename
        else:
            csv_path = Path(args.csv)

        assert not csv_path.exists(), (
            f"Output CSV {csv_path} already exists. Please provide a different path or remove the existing file."
        )

        writer = BenchmarkWriter(csv_path)

        build_runner_fn = functools.partial(
            _build_runner,
            system_name=system_name,
            db_engine=db_engine,
            snapshotter=snapshotter,
            parquet_path=parquet_path,
            benchmark=args.benchmark,
            scale_factors=scale_factors,
            db_storage=args.db_storage,
            disk_db_dir=Path(args.disk_db_dir)
            if getattr(args, "disk_db_dir", None)
            else None,
        )

        try:
            logger.info(f"Appending benchmark CSV to {csv_path}")
            _write_header(writer)

            if system_name == "bespoke":
                runner = build_runner_fn()

            for num_threads in num_threads_list:
                if system_name != "bespoke":
                    # other runner require num_threads at initialization, so build them in the loop
                    logger.warning(
                        f"Re-initializing runner for num_threads={num_threads}"
                    )
                    runner = build_runner_fn(num_threads=num_threads)

                logger.info(
                    f"Benchmarking system: {runner.name} num_threads={num_threads}"
                )
                logger.info(f"Benchmarking queries: {','.join(map(str, query_ids))}")

                for snapshot, db, mt in zip(run_snapshots, db_storage, is_mt):
                    if use_snapshots:
                        cache_path = Path(args.artifacts_dir) / "cache"
                        restore_snapshot = getattr(runner, "restore_snapshot")
                        restore_snapshot(
                            snapshot,
                            benchmark=args.benchmark,
                            query_list=query_ids,
                            cache_path=cache_path,
                            is_mt=mt,
                            db_storage=db,
                        )

                    for scale_factor in scale_factors:
                        logger.info(
                            f"Scale factor: {scale_factor} num_threads={num_threads}"
                        )
                        query_list, sql_list, args_list = _make_query_batch(
                            gen_query_fn=gen_query_fn,
                            query_ids=query_ids,
                            instantiations=args.instantiations,
                            repetitions=args.repetitions,
                        )

                        if system_name == "bespoke":
                            add_args = {
                                "parallelism": num_threads > 1,
                                "core_ids": list(range(num_threads))
                                if num_threads > 1
                                else None,
                            }
                        else:
                            add_args = {}

                        timings = runner.run_scale_factor(
                            scale_factor=scale_factor,
                            query_list=query_list,
                            sql_list=sql_list,
                            args_list=args_list,
                            **add_args,
                        )
                        rows = _timing_rows(
                            query_list=query_list,
                            timings=timings,
                            scale_factor=scale_factor,
                            benchmark=args.benchmark,
                            system=runner.name,
                            num_threads=num_threads,
                            host=host,
                            snapshot=snapshot,
                        )
                        writer.write_rows(rows)
        finally:
            writer.close()
    finally:
        os.umask(old_umask)


def _make_query_batch(
    gen_query_fn,
    query_ids: list[str],
    instantiations: int,
    repetitions: int,
) -> tuple[list[str], list[str], list[str]]:
    sql_list: list[str] = []
    placeholder_list: list[dict] = []
    query_list: list[str] = []

    for inst_idx in range(instantiations):
        rnd = random.Random(42 + inst_idx)
        inst_queries: list[str] = []
        inst_sql: list[str] = []
        inst_placeholders: list[dict] = []
        for query_id in query_ids:
            _, query, placeholders = gen_query_fn(query_name=f"Q{query_id}", rnd=rnd)
            inst_queries.append(str(query_id))
            inst_sql.append(query)
            inst_placeholders.append(placeholders)
        for _ in range(repetitions):
            query_list.extend(inst_queries)
            sql_list.extend(inst_sql)
            placeholder_list.extend(inst_placeholders)

    args_list = format_args_string(query_list, placeholder_list)
    return query_list, sql_list, args_list


def _timing_rows(
    query_list: list[str],
    timings: list[float | None],
    scale_factor: float,
    benchmark: str,
    system: str,
    num_threads: int,
    host: str,
    snapshot: str,
) -> list[list[object]]:
    if len(timings) != len(query_list):
        raise RuntimeError(
            f"Expected {len(query_list)} timings from {system}, got {len(timings)}."
        )

    return [
        [
            query_id,
            scale_factor,
            benchmark,
            system,
            num_threads,
            time_ms,
            host,
            snapshot,
        ]
        for query_id, time_ms in zip(query_list, timings)
        if time_ms is not None
    ]


def get_all_query_ids(benchmark: str) -> List[str]:
    if benchmark == "tpch":
        query_ids = [str(i) for i in range(1, 23)]
    elif benchmark == "ceb":
        query_ids = [
            "1a",
            "2a",
            "2b",
            "2c",
            "3a",
            "3b",
            "4a",
            "5a",
            "6a",
            "7a",
            "8a",
            "9a",
            "9b",
            "10a",
            "11a",
            "11b",
        ]
    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")

    return query_ids
