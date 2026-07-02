import logging
from pathlib import Path

from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.observability.benchmark.systems.track import BespokePrep
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run import RunTool, RunWorkerResult
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)


class BespokeRunner:
    """Runs the generated engine for a snapshot.

    Snapshot restoration and compilation are track-specific (different prepare
    workspace, prepare function and compiler), supplied via ``BespokePrep``. The
    actual benchmarking goes through the workload-provider-driven
    ``RunTool.run_worker(mode=BENCHMARK)``: the provider deterministically emits
    the same queries the benchmarker reports, and per-query ``elapsed_ms`` comes
    back in batch order.
    """

    name = "Bespoke"

    def __init__(
        self,
        provider: WorkloadProvider,
        bespoke_prep: BespokePrep,
        snapshotter: GitSnapshotter,
        workspace_dir: Path,
        parquet_base_dir: Path,
        dataset_name: str,
        db_storage: DBStorage,
        memory_budget_mb: int | None,
        prepare_cache_dir: Path | None = None,
    ) -> None:
        self._provider = provider
        self._prep = bespoke_prep
        self._snapshotter = snapshotter
        self._workspace_dir = workspace_dir
        self._parquet_base_dir = parquet_base_dir
        self._dataset_name = dataset_name
        self._db_storage = db_storage
        self._memory_budget_mb = memory_budget_mb
        self._prepare_cache_dir = prepare_cache_dir
        self._active_snapshot: str | None = None
        self._db_engine: RunTool | None = None

    def restore_snapshot(
        self,
        snapshot: str,
        is_mt: bool,
    ) -> None:
        if self._active_snapshot == snapshot and self._db_engine is not None:
            return

        prepare_workspace = self._prep.make_prepare_workspace(
            self._provider,
            self._workspace_dir,
            self._snapshotter,
            self._prepare_cache_dir,
        )

        # The benchmarker owns this workspace, so clean it non-interactively
        # before restoring. Otherwise framework-generated untracked files (e.g.
        # parquet_reader.cpp from a prior prepare) and the storage tmp/ dir make
        # the working dir "dirty" and prepare_repo_and_load_snapshot blocks on an
        # interactive confirmation prompt (which EOFs in a non-interactive run).
        is_dirty, status = self._snapshotter.is_dirty()
        if is_dirty:
            logger.info("Cleaning benchmark workspace before restore:\n%s", status)
            self._snapshotter.reset_changes()
            self._snapshotter.clear_untracked(include_ignored=True)

        # Replay the snapshot's own prepare record (features=None): the
        # workspace metadata file committed with the snapshot says what its
        # files were prepared with, so no is_mt-dependent prepare fn is needed.
        prepare_repo_and_load_snapshot(
            snapshotter=self._snapshotter,
            snapshot=snapshot,
            features=None,
            prepare_workspace_provider=prepare_workspace,
            parallelism=is_mt,  # ignored on the replay path
            do_not_cache=True,
        )

        compiler = self._prep.make_compiler(self._workspace_dir)
        self._db_engine = RunTool(
            workload_provider=self._provider,
            cwd=self._workspace_dir,
            dataset_name=self._dataset_name,
            base_parquet_dir=self._parquet_base_dir,
            db_storage=self._db_storage,
            compiler=compiler,
            run_stats_collector=None,
            query_validator=None,
            memory_budget_mb=self._memory_budget_mb,
        )
        self._active_snapshot = snapshot

    def run_scale_factor(
        self,
        scale_factor: float,
        query_list: list[str],
        sql_list: list[str],
        args_list: list[str],
        parallelism: bool | None = None,
        core_ids: list[int] | None = None,
    ) -> list[float | None]:
        assert self._db_engine is not None, (
            "restore_snapshot must be called before run_scale_factor"
        )

        # The provider emits queries for this scale factor (BENCHMARK mode); the
        # benchmarker has already configured set_benchmark_sf / instantiations /
        # repetitions on the same provider, so the produced workload matches the
        # query_list reported by the caller.
        self._provider.set_benchmark_sf(scale_factor)
        unique_query_ids = list(dict.fromkeys(query_list))

        logger.info("Running ./db benchmark (sf=%s)...", scale_factor)
        result: RunWorkerResult = self._db_engine.run_worker(
            mode=RunToolMode.BENCHMARK,
            optimize=True,
            query_ids=unique_query_ids,
            echo_output=True,
            external_call=True,
            parallelism=parallelism,
            core_ids=core_ids,
        )

        assert result.query_results is not None and len(result.query_results) > 0, (
            f"Expected query_results from ./db execution, got {result.query_results}. {result}"
        )

        exec_times = [qr.elapsed_ms for qr in result.query_results]
        if len(exec_times) != len(query_list):
            raise RuntimeError(
                f"Expected {len(query_list)} timings from ./db, got {len(exec_times)}."
            )
        return exec_times
