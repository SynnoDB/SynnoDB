import argparse
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(Path(__file__).parent.parent.parent.as_posix())
from cpp_runner.compiler.compiler_utils import make_compiler
from cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from observability.benchmark.run import get_all_query_ids
from observability.logging.logger import setup_logging
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool, RunWorkerResult
from tools.validate.query_validator_class import format_args_string
from utils.utils import DBStorage, sha256
from workloads.dataset.dataset_tables_dict import get_dataset_name
from workloads.dataset.query_gen_factory import get_query_gen

setup_logging(level=logging.DEBUG)

logger = logging.getLogger(__name__)


def main(args):
    sys.path.append(Path(__file__).parent.parent.parent.as_posix())

    work_dir = Path(__file__).parent / "output"
    snapshotter = GitSnapshotter(working_dir=work_dir)

    # random values: date_time_random
    rnd_str = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(1, 100000)}"

    ##### CONFIGURATION #####
    benchmark = "tpch"
    query_list = get_all_query_ids(benchmark)
    db_storage = DBStorage.IN_MEMORY
    parquet_dir = f"/mnt/labstore/bespoke_olap/{get_dataset_name(benchmark)}_parquet/"
    parquet_dir = (
        Path(__file__).parent.parent.parent
        / "data"
        / f"{get_dataset_name(benchmark)}_parquet"
    )
    parallelism = False
    core_ids = None
    scale_factor = 1.0
    ##########################

    if not args.skip_prep:
        prepare_repo_and_load_snapshot(
            snapshotter=snapshotter,
            snapshot=None,
            prepare="base",
            benchmark=benchmark,
            query_list=query_list,
            cache_path=None,
            db_storage=DBStorage.IN_MEMORY,
            conv_name=f"test_conv_{rnd_str}",
        )

    compiler = make_compiler(
        cwd=work_dir,
        db_storage=db_storage,
        untracked_cpp_runner_content="",
    )
    bespoke_engine = RunTool(
        cwd=work_dir,
        query_validator=None,
        dataset_name=get_dataset_name(benchmark),
        base_parquet_dir=parquet_dir,
        run_stats_collector=None,
        db_storage=db_storage,
        compiler=compiler,
    )

    instantiations = 2
    repetitions = 2
    inst_query_list, inst_sql_list, inst_args_list, inst_hash_list = _make_query_batch(
        gen_query_fn=get_query_gen(benchmark),
        query_ids=query_list,
        instantiations=instantiations,
        repetitions=repetitions,
    )

    result: RunWorkerResult = bespoke_engine.run_worker(
        scale_factor=scale_factor,
        optimize=True,
        query_id=inst_query_list,
        stdin_args_data=inst_args_list,
        echo_output=True,
        parallelism=parallelism,
        core_ids=core_ids,
    )

    logger.info(f"Run result: {result.msg}")
    logger.info(f"Run stdout: {result.out}")
    logger.info(f"Run stderr: {result.err}")

    assert result.query_results is not None, "No query results returned"
    assert (
        len(result.query_results) == len(query_list) * instantiations * repetitions
    ), (
        f"Number of query results does not match number of queries run: {len(result.query_results)} != {len(query_list) * instantiations * repetitions}"
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
