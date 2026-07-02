"""Track (use-case) selection for the benchmarker.

The benchmarker can run against different usecases that share the same
``WorkloadProvider`` / ``RunTool`` plumbing:

* ``Usecase.OLAP`` — the classic in-DB OLAP engine (systems: bespoke, duckdb,
  umbra, clickhouse).

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from synnodb.cpp_runner.compiler.compiler_cached import CachedCompiler
from synnodb.cpp_runner.compiler.compiler_factory_olap import OLAPCompilerFactory
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.cli_config import Usecase
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import Workload, WorkloadProvider
from synnodb.workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

# Systems that are meaningful per use-case.
AVAILABLE_SYSTEMS_BY_USECASE: dict[Usecase, tuple[str, ...]] = {
    Usecase.OLAP: ("bespoke", "duckdb", "umbra", "clickhouse"),
}


@dataclass
class DuckDBConfig:
    """Everything the DuckDB reference runner needs, independent of track."""

    dataset_tables: list[str]
    run_on_parquet: bool
    db_storage: DBStorage
    parquet_base_dir: Path  # contains per-scale-factor subdirs sf<N>/


@dataclass
class BespokePrep:
    """Bespoke compile + snapshot-prepare bundle for one track."""

    make_compiler: Callable[[Path], CachedCompiler]
    make_prepare_workspace: Callable[
        [WorkloadProvider, Path, GitSnapshotter, Path | None], PrepareWorkspace
    ]


@dataclass
class TrackConfig:
    usecase: Usecase
    workload: Workload
    provider: WorkloadProvider
    dataset_name: str
    duckdb: DuckDBConfig
    bespoke_db_storage: DBStorage
    bespoke_memory_budget_mb: int | None
    bespoke: BespokePrep


def available_systems(usecase: Usecase) -> tuple[str, ...]:
    return AVAILABLE_SYSTEMS_BY_USECASE[usecase]


def resolve_workload(usecase: Usecase, benchmark: str) -> Workload:
    """Map a ``--benchmark`` string onto the right Workload enum for the track."""
    if usecase == Usecase.OLAP:
        return OLAPWorkload(benchmark)
    raise ValueError(f"Unknown usecase: {usecase}")


def dataset_name_for(usecase: Usecase, workload: Workload) -> str:
    if usecase == Usecase.OLAP:
        return OLAPWorkloadProvider._get_dataset_name(workload)  # type: ignore[arg-type]
    raise ValueError(f"Unknown usecase: {usecase}")


def build_track(
    *,
    usecase: Usecase,
    benchmark: str,
    parquet_base_dir: Path,
    bespoke_ssd_storage_dir: Path,
    cli_db_storage: DBStorage,
    memory_budget_mb: int | None,
) -> TrackConfig:
    """Assemble the full per-track configuration for a benchmark run.

    ``parquet_base_dir`` must contain per-scale-factor subdirs (``sf1/`` ...),
    which both the workload provider (for the bespoke ``./db`` loader) and the
    duckdb reference runner read from.
    """
    workload = resolve_workload(usecase, benchmark)
    dataset_name = dataset_name_for(usecase, workload)

    if usecase == Usecase.OLAP:
        assert isinstance(workload, OLAPWorkload)
        provider: WorkloadProvider = OLAPWorkloadProvider(
            benchmark=workload,
            base_parquet_dir=parquet_base_dir,
            db_storage=cli_db_storage,
            bespoke_ssd_storage_dir=bespoke_ssd_storage_dir,
            query_cache_dir=None,
            memory_limit_mb=memory_budget_mb,
        )
        duckdb = DuckDBConfig(
            dataset_tables=OLAPWorkloadProvider._dataset_tables(workload),  # type: ignore[arg-type]
            run_on_parquet=True,
            db_storage=cli_db_storage,
            parquet_base_dir=parquet_base_dir,
        )
        bespoke = BespokePrep(
            make_compiler=lambda cwd: OLAPCompilerFactory(
                db_storage=cli_db_storage
            ).make_compiler(cwd=cwd, untracked_cpp_runner_content=""),
            make_prepare_workspace=lambda wp, ws, snap, cache: OLAPPrepareWorkspace(
                db_storage=cli_db_storage,
                workload_provider=wp,
                workspace_dir=ws,
                git_snapshotter=snap,
                prepare_cache_dir=cache,
            ),
        )
        return TrackConfig(
            usecase=usecase,
            workload=workload,
            provider=provider,
            dataset_name=dataset_name,
            duckdb=duckdb,
            bespoke_db_storage=cli_db_storage,
            bespoke_memory_budget_mb=memory_budget_mb,
            bespoke=bespoke,
        )

    raise ValueError(f"Unknown usecase: {usecase}")
