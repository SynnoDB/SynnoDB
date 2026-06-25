import logging
from pathlib import Path

from cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_mt,
    prepare_optim,
    prepare_repo_and_load_snapshot,
)
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool, RunWorkerResult
from utils.utils import DBStorage

# from tools.fasttest.run import RunTool

logger = logging.getLogger(__name__)


class BespokeRunner:
    name = "Bespoke"

    def __init__(
        self,
        db_engine: "RunTool",  # type: ignore  # noqa: F821
        snapshotter: GitSnapshotter,
    ) -> None:
        self._db_engine = db_engine
        self._snapshotter = snapshotter
        self._active_snapshot: str | None = None

    def restore_snapshot(
        self,
        snapshot: str,
        benchmark: str,
        query_list: list[str],
        cache_path: Path,
        is_mt: bool,
        db_storage: DBStorage,
    ) -> None:
        if self._active_snapshot == snapshot:
            return

        prepare_repo_and_load_snapshot(
            snapshotter=self._snapshotter,
            snapshot=snapshot,
            prepare_fn=prepare_mt if is_mt else prepare_optim,
            benchmark=benchmark,
            query_list=query_list,
            cache_path=cache_path,
            db_storage=db_storage,
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
        unique_query_ids = list(dict.fromkeys(query_list))

        logger.info("Running ./db for benchmark...")
        result: RunWorkerResult = self._db_engine.run_worker(
            scale_factor=scale_factor,
            optimize=True,
            query_id=unique_query_ids,
            stdin_args_data=args_list,
            echo_output=True,
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
