import argparse
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(Path(__file__).parent.parent.parent.as_posix())
from cpp_runner.compiler.compiler_factory_olap import OLAPCompilerFactory
from cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from observability.logging.logger import setup_logging
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool, RunToolMode, RunWorkerResult
from tools.validate.query_validator_class import QueryValidator
from utils.utils import DBStorage
from workloads.query_execution_cache import QueryExecutionCache
from workloads.system_factory import SystemFactory
from workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

setup_logging(level=logging.DEBUG)

logger = logging.getLogger(__name__)


def main(args):
    sys.path.append(Path(__file__).parent.parent.parent.as_posix())

    work_dir = Path(__file__).parent / "output"
    snapshotter = GitSnapshotter(working_dir=work_dir)

    # random values: date_time_random
    rnd_str = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(1, 100000)}"

    ##### CONFIGURATION #####
    benchmark = OLAPWorkload.TPC_H
    db_storage = DBStorage.IN_MEMORY
    parallelism = False
    core_ids = None  # [3, 4, 5, 6]
    ##########################

    dataset_name = OLAPWorkloadProvider._get_dataset_name(benchmark)
    # parquet_dir = f"/mnt/labstore/bespoke_olap/{dataset_name}_parquet/"
    parquet_dir = (
        Path(__file__).parent.parent.parent / "data" / f"{dataset_name}_parquet"
    )
    workload_provider = OLAPWorkloadProvider(
        benchmark=benchmark,
        base_parquet_dir=parquet_dir,
        db_storage=db_storage,
        bespoke_storage_dir=None,
    )

    if not args.skip_prep:
        prepare_workspace_provider = OLAPPrepareWorkspace(
            db_storage=db_storage,
            workload_provider=workload_provider,
            workspace_dir=work_dir,
            git_snapshotter=snapshotter,
            prepare_cache_dir=None,
        )

        prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=None,
            prepare="base",
            conv_name=f"test_conv_{rnd_str}",
            add_sample_trace=True,
            prepare_workspace_provider=prepare_workspace_provider,
            usecase_prepare_args=dict(
                storage_plan="DEMO STORAGE PLAN CONTENT",
            ),
        )

    compiler = OLAPCompilerFactory(db_storage=db_storage).make_compiler(
        cwd=work_dir,
        untracked_cpp_runner_content="",
    )
    compiler.set_compile_options(optimize=True, trace_mode=True)

    # compile
    comp_result, _, _ = compiler.build_cached(skip_cache=True, write_cache=False)
    assert comp_result is None, f"Compilation failed with error: {comp_result}"

    query_exec_cache = QueryExecutionCache(
        query_execution_cache_dir=None, system_factory=SystemFactory()
    )

    query_validator = QueryValidator(
        validate_cache_dir=None,
        workspace_path=work_dir,
        query_execution_cache=query_exec_cache,
        all_query_ids=workload_provider.query_ids,
        git_snapshotter=snapshotter,
    )

    bespoke_engine = RunTool(
        cwd=work_dir,
        query_validator=query_validator,
        dataset_name=workload_provider.dataset_name,
        base_parquet_dir=parquet_dir,
        run_stats_collector=None,
        db_storage=db_storage,
        compiler=compiler,
        workload_provider=workload_provider,
        parallelism=parallelism,
        core_ids=core_ids,
    )

    # instantiations = 2
    # repetitions = 2
    # inst_query_list, inst_sql_list, inst_args_list, inst_hash_list = _make_query_batch(
    #     gen_query_fn=workload_provider.get_query_gen_fn(),
    #     query_ids=workload_provider.query_ids,
    #     instantiations=instantiations,
    #     repetitions=repetitions,
    # )

    result: RunWorkerResult = bespoke_engine.run_worker(
        mode=RunToolMode.FAST_CHECK,
        optimize=True,
        query_ids=None,  # ["1"],
        trace_mode=True,
        echo_output=True,
        parallelism=parallelism,
        core_ids=core_ids,
    )

    assert result.query_results is not None, "Expected query results, but got None"

    for res in result.query_results:
        logger.info(res)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-prep", action="store_true")
    args = parser.parse_args()

    main(args)
