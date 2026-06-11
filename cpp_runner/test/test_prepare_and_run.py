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
from tools.run import RunTool, RunWorkerResult
from tools.validate.query_validator_class import format_args_string
from utils.utils import DBStorage, sha256
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
    workload_provider = OLAPWorkloadProvider(benchmark=benchmark)
    db_storage = DBStorage.IN_MEMORY
    parquet_dir = (
        f"/mnt/labstore/bespoke_olap/{workload_provider.dataset_name}_parquet/"
    )
    parquet_dir = (
        Path(__file__).parent.parent.parent
        / "data"
        / f"{workload_provider.dataset_name}_parquet"
    )
    parallelism = False
    core_ids = None
    ##########################

    if benchmark == OLAPWorkload.CEB:
        scale_factor = 0.25
    elif benchmark == OLAPWorkload.TPC_H:
        scale_factor = 1
    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")

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

    bespoke_engine = RunTool(
        cwd=work_dir,
        query_validator=None,
        dataset_name=workload_provider.dataset_name,
        base_parquet_dir=parquet_dir,
        run_stats_collector=None,
        db_storage=db_storage,
        compiler=compiler,
    )

    instantiations = 2
    repetitions = 2
    inst_query_list, inst_sql_list, inst_args_list, inst_hash_list = _make_query_batch(
        gen_query_fn=workload_provider.get_query_gen_fn(),
        query_ids=workload_provider.query_ids,
        instantiations=instantiations,
        repetitions=repetitions,
    )

    result: RunWorkerResult = bespoke_engine.run_worker(
        scale_factor=scale_factor,
        optimize=True,
        trace_mode=True,
        query_id=inst_query_list,
        stdin_args_data=inst_args_list,
        echo_output=True,
        parallelism=parallelism,
        core_ids=core_ids,
    )

    assert result.query_results is not None, "No query results returned"
    assert (
        len(result.query_results)
        == len(workload_provider.query_ids) * instantiations * repetitions
    ), (
        f"Number of query results does not match number of queries run: {len(result.query_results)} != {len(workload_provider.query_ids) * instantiations * repetitions}"
    )
    for res in result.query_results:
        logger.info(res)


def _make_query_batch(
    gen_query_fn,
    query_ids: list[str],
    instantiations: int,
    repetitions: int,
) -> tuple[list[str], list[str], list[str], list[str]]:
    sql_list: list[str] = []
    placeholder_list: list[dict] = []
    query_list: list[str] = []
    hash_list: list[str] = []

    for inst_idx in range(instantiations):
        rnd = random.Random(42 + inst_idx)
        inst_queries: list[str] = []
        inst_sql: list[str] = []
        inst_placeholders: list[dict] = []
        inst_hash_list: list[str] = []
        for query_id in query_ids:
            _, query, placeholders = gen_query_fn(query_name=f"Q{query_id}", rnd=rnd)
            inst_queries.append(str(query_id))
            inst_sql.append(query)
            inst_placeholders.append(placeholders)
            inst_hash_list.append(sha256(query))
        for _ in range(repetitions):
            query_list.extend(inst_queries)
            sql_list.extend(inst_sql)
            placeholder_list.extend(inst_placeholders)
            hash_list.extend(inst_hash_list)

    args_list = format_args_string(query_list, placeholder_list)
    return query_list, sql_list, args_list, hash_list


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-prep", action="store_true")
    args = parser.parse_args()

    main(args)
