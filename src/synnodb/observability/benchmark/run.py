from __future__ import annotations

import logging
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List

import psutil

from synnodb.cpp_runner.prepare_repo.prepare_features import Parallelism
from synnodb.observability.benchmark.systems import track
from synnodb.observability.benchmark.writer import BenchmarkWriter
from synnodb.observability.logging.logger import setup_logging
from synnodb.observability.logging.wandb_api_helper import (
    wandb_retrieve_metrics_for_run,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.cli_config import Usecase
from synnodb.utils.core_utils import clamp_threads_to_available
from synnodb.utils.utils import DBStorage, create_dir_and_set_permissions
from synnodb.workloads.workload_provider import Workload, WorkloadProvider

if TYPE_CHECKING:
    from synnodb.observability.benchmark.systems.track import TrackConfig

logger = logging.getLogger(__name__)


def get_all_query_ids(benchmark) -> list[str]:
    """Return all query IDs for a benchmark (OLAP semantics).

    Kept as a module-level helper for backward compatibility: several
    ``observability.ui_template_runner`` modules import it from here. Accepts a
    benchmark string ("tpch", "ceb") or a :class:`Workload` enum value.
    """
    from synnodb.workloads.workload_provider_olap import _get_all_query_ids

    name = benchmark.value if isinstance(benchmark, Workload) else str(benchmark)
    return _get_all_query_ids(name)


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


def _resolve_system(args, usecase: Usecase) -> str:
    available = track.available_systems(usecase)
    raw = getattr(args, "system", None) or getattr(args, "systems", None)
    if not raw:
        raise ValueError(f"Provide --system <name>. Available: {', '.join(available)}")
    names = [s.strip().lower() for s in raw.split(",") if s.strip()]
    if len(names) != 1:
        raise ValueError(
            "Run one system at a time. Use --system <name> and write each run "
            "to its own CSV, then combine CSVs with the plot command."
        )
    system_name = names[0]
    if system_name not in available:
        raise ValueError(
            f"System '{system_name}' is not available for usecase '{usecase.value}'. "
            f"Available: {', '.join(available)}"
        )
    return system_name


def _synno_data_dir() -> Path:
    raw = os.getenv("SYNNO_DATA_DIR")
    assert raw is not None, "SYNNO_DATA_DIR environment variable is not set"
    return Path(raw)


def _core_ids_for(num_threads: int) -> list[int] | None:
    return list(range(num_threads)) if num_threads > 1 else None


def _resolve_snapshots(
    args,
    snapshots: list[str],
    wandb_run_ids: list[str],
    workload: Workload,
) -> tuple[list[str], list[Parallelism]]:
    """Return (snapshot_hashes, parallelism modes) for the bespoke system."""
    if snapshots:
        # Direct hashes: prepare mode (mt vs optim) is unknown -> default to
        # single-thread prep.
        return snapshots, [Parallelism.SINGLE_THREADED] * len(snapshots)

    run_snapshots: list[str] = []
    parallelisms: list[Parallelism] = []
    for wandb_id in wandb_run_ids:
        statistics, config, _ = wandb_retrieve_metrics_for_run(
            workload, wandb_id, fetch_latest_runtimes=False
        )
        snapshot_hash = statistics["code/snapshot_hash"]
        assert snapshot_hash and snapshot_hash != "N/A", (
            f"Could not resolve a snapshot hash from wandb run {wandb_id}: {snapshot_hash}"
        )
        run_snapshots.append(snapshot_hash)
        # Runs predating the Parallelism enum logged a bool; newer runs log the
        # enum's string value.
        recorded = config.get("needs_parallelism", False)
        is_mt = recorded is True or recorded == Parallelism.MULTI_THREADED.value
        parallelisms.append(
            Parallelism.MULTI_THREADED if is_mt else Parallelism.SINGLE_THREADED
        )
    return run_snapshots, parallelisms


def _build_runner(
    system_name: str,
    track_cfg: "TrackConfig",
    num_threads: int,
    snapshotter: GitSnapshotter | None,
    workspace_dir: Path,
    disk_db_dir: Path | None,
    scale_factors: list[float],
):
    if system_name == "bespoke":
        from synnodb.observability.benchmark.systems.bespoke import BespokeRunner

        assert snapshotter is not None, "snapshotter required for BespokeRunner"
        return BespokeRunner(
            provider=track_cfg.provider,
            bespoke_prep=track_cfg.bespoke,
            snapshotter=snapshotter,
            workspace_dir=workspace_dir,
            parquet_base_dir=track_cfg.duckdb.parquet_base_dir,
            dataset_name=track_cfg.dataset_name,
            db_storage=track_cfg.bespoke_db_storage,
            memory_budget_mb=track_cfg.bespoke_memory_budget_mb,
        )

    if system_name == "duckdb":
        from synnodb.observability.benchmark.systems.duckdb import DuckDBRunner

        ddb = track_cfg.duckdb
        return DuckDBRunner(
            parquet_path=ddb.parquet_base_dir,
            benchmark=track_cfg.workload,
            dataset_tables=ddb.dataset_tables,
            db_storage=ddb.db_storage,
            run_on_parquet=ddb.run_on_parquet,
            disk_db_dir=disk_db_dir,
            num_threads=num_threads,
        )

    if system_name == "umbra":
        from synnodb.observability.benchmark.systems.umbra import UmbraRunner

        return UmbraRunner(
            parquet_path=track_cfg.duckdb.parquet_base_dir,
            benchmark=track_cfg.workload,
            scale_factors=scale_factors,
            container_num_cores=num_threads,
            allow_auto_restarts=True,
            setup=True,
            db_storage=track_cfg.duckdb.db_storage,
            disk_db_dir=disk_db_dir,
        )

    if system_name == "clickhouse":
        from synnodb.observability.benchmark.systems.clickhouse import ClickHouseRunner

        return ClickHouseRunner(
            parquet_path=track_cfg.duckdb.parquet_base_dir,
            benchmark=track_cfg.workload,
            scale_factors=scale_factors,
            container_num_cores=num_threads,
        )

    raise ValueError(f"Unknown system '{system_name}'.")


def _produce_query_batch(
    provider: WorkloadProvider,
    query_ids: list[str],
    num_threads: int,
    core_ids: list[int] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Deterministically generate the canonical query batch for one scale factor.

    All systems consume the same batch (generation is seeded), so bespoke and the
    reference systems run identical queries and stay comparable.
    """
    batches = provider.produce_workload(
        run_mode=RunToolMode.BENCHMARK,
        query_ids=query_ids,
        num_threads=num_threads,
        core_ids=core_ids,
    )
    assert len(batches) == 1, (
        f"BENCHMARK mode should emit exactly one batch, got {len(batches)}."
    )
    entries = batches[0].query_list
    query_list = [e.query_id for e in entries]
    sql_list = [e.sql for e in entries]
    args_list = [e.query_args for e in entries]
    return query_list, sql_list, args_list


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


def _resolve_csv_path(args, synno: Path, system_name: str, benchmark: str) -> Path:
    if args.csv is not None:
        return Path(args.csv)
    csv_dir = synno / "benchmark_logs"
    create_dir_and_set_permissions(csv_dir)
    filename = (
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{system_name}_{benchmark}.csv"
    )
    return csv_dir / filename


def run_benchmark(args) -> None:
    old_umask = os.umask(0)
    try:
        try:
            from agents.tracing import set_tracing_disabled

            set_tracing_disabled(True)
        except ImportError:
            pass

        setup_logging(logging.DEBUG, logfile=None)

        usecase: Usecase = args.usecase
        system_name = _resolve_system(args, usecase)
        use_snapshots = system_name == "bespoke"

        snapshots: list[str] = []
        wandb_run_ids: list[str] = []
        if getattr(args, "snapshots", None):
            snapshots = [s.strip() for s in args.snapshots.split(",") if s.strip()]
        if getattr(args, "wandb_ids", None):
            wandb_run_ids = [s.strip() for s in args.wandb_ids.split(",") if s.strip()]

        assert not (snapshots and wandb_run_ids), (
            "Provide either --snapshots or --wandb_ids, not both."
        )
        if use_snapshots and not snapshots and not wandb_run_ids:
            raise ValueError(
                "Provide --snapshots or --wandb_ids when benchmarking the bespoke system."
            )

        synno = _synno_data_dir()
        workspace_path = Path(__file__).parent.parent.parent / "output"
        assert workspace_path.is_dir(), (
            f"Expected workspace/output directory at {workspace_path}"
        )

        cli_db_storage: DBStorage = args.db_storage

        workload = track.resolve_workload(usecase, args.benchmark)
        dataset_name = track.dataset_name_for(usecase, workload)
        parquet_base_dir = (
            synno / "workloads" / workload.value / f"{dataset_name}_parquet"
        )
        bespoke_ssd_storage_dir = workspace_path.absolute() / "tmp"

        track_cfg = track.build_track(
            usecase=usecase,
            benchmark=args.benchmark,
            parquet_base_dir=parquet_base_dir,
            bespoke_ssd_storage_dir=bespoke_ssd_storage_dir,
            cli_db_storage=cli_db_storage,
            memory_budget_mb=getattr(args, "memory_budget_mb", None),
        )
        provider = track_cfg.provider

        # CLI controls how many parameter sets / repetitions BENCHMARK mode emits.
        provider.set_benchmark_instantiations(args.instantiations)
        provider.set_benchmark_repetitions(args.repetitions)

        host = socket.gethostname()
        scale_factors = _parse_scale_factors(args.scale_factors)
        num_threads_list = _parse_num_threads(getattr(args, "num_threads", "1") or "1")
        query_ids = _filter_query_ids(provider.query_ids, args.query_ids)
        logger.info(f"Scale factors: {', '.join(map(str, scale_factors))}")
        logger.info(f"Parquet base dir: {parquet_base_dir.as_posix()}")

        snapshotter: GitSnapshotter | None = None
        if use_snapshots:
            snapshotter = GitSnapshotter(
                cache_repo=None
                if args.disable_repo_sync
                else os.environ.get(
                    "GIT_SNAPSHOTTER_SERVER", "git://c01/bespoke_cache.git"
                ),
                working_dir=workspace_path,
                extra_gitignore=[],
            )
            snapshotter.fetch_snapshots()
            run_snapshots, parallelism_list = _resolve_snapshots(
                args, snapshots, wandb_run_ids, workload
            )
        else:
            run_snapshots = [""]
            parallelism_list = [Parallelism.SINGLE_THREADED]

        disk_db_dir = (
            bespoke_ssd_storage_dir
            if cli_db_storage in (DBStorage.SSD, DBStorage.LABSTORE)
            else None
        )

        csv_path = _resolve_csv_path(args, synno, system_name, args.benchmark)
        assert not csv_path.exists(), (
            f"Output CSV {csv_path} already exists. Provide a different --csv path."
        )
        writer = BenchmarkWriter(csv_path)

        try:
            logger.info(f"Writing benchmark CSV to {csv_path}")
            _write_header(writer)

            for requested_threads in num_threads_list:
                # Catch a user-selected count that oversubscribes this host (more
                # threads than logical cores): warn and cap it at the max available, so
                # every downstream use (pinning, container sizing, the CSV row) reflects
                # the count actually run.
                num_threads = clamp_threads_to_available(
                    requested_threads, psutil.cpu_count(logical=True) or 1
                )
                runner = _build_runner(
                    system_name,
                    track_cfg,
                    num_threads,
                    snapshotter,
                    workspace_path,
                    disk_db_dir,
                    scale_factors,
                )
                logger.info(
                    f"Benchmarking {runner.name} (usecase={usecase.value}) "
                    f"num_threads={num_threads} queries={','.join(map(str, query_ids))}"
                )
                core_ids = _core_ids_for(num_threads)

                for snapshot, parallelism in zip(run_snapshots, parallelism_list):
                    if use_snapshots:
                        runner.restore_snapshot(snapshot, parallelism=parallelism)

                    for scale_factor in scale_factors:
                        logger.info(
                            f"Scale factor: {scale_factor} num_threads={num_threads}"
                        )
                        provider.set_benchmark_sf(scale_factor)
                        query_list, sql_list, args_list = _produce_query_batch(
                            provider, query_ids, num_threads, core_ids
                        )

                        if system_name == "bespoke":
                            add_args = {
                                "parallelism": num_threads > 1,
                                "core_ids": core_ids,
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
