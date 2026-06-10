# add parent to path
import logging
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from demo_and_analysis.benchmark.run import get_all_query_ids
from misc.dataset.dataset_tables_dict import get_dataset_name
from misc.dataset.query_gen_factory import get_placeholders_fn, get_query_gen
from pipeline.cpp_runner.compiler_utils import make_compiler
from pipeline.tools.run import RunTool
from pipeline.tools.validate.query_validator_class import QueryValidator
from prepare_repo.assemble_query_and_args import get_sql_dict
from prepare_repo.get_readonly_files import get_readonly_files
from prepare_repo.prepare import prepare_repo
from utils.logging_and_reporting.logger import setup_logging
from utils.utils import DBStorage, get_disk_db_dir

setup_logging(level=logging.INFO)


class TestPrepareRepo(unittest.TestCase):
    def test_workspace_setup(self):
        cache_dir = Path("/mnt/labstore/bespoke_olap/cache")
        benchmark = "tpch"
        db_storage = DBStorage.IN_MEMORY
        gen_placeholders_fn = get_placeholders_fn(
            benchmark,
            do_not_cache=True,
            cache_dir=cache_dir / "placeholders_cache",
        )

        workspace_dir = Path(__file__).parent / "test_dir"

        # delete workspace if it exists
        if workspace_dir.exists():
            import shutil

            shutil.rmtree(workspace_dir)

        # create dest dir
        workspace_dir.mkdir(exist_ok=False)

        query_ids = get_all_query_ids(benchmark)
        readonly_files_not_git_tracked, readonly_files_git_tracked = (
            get_readonly_files()
        )

        disk_db_dir, bespoke_db_dir = get_disk_db_dir(db_storage, workspace_dir)

        untracked_cpp_runner_content = prepare_repo(
            workspace_dir,
            benchmark=benchmark,
            storage_plan="dies ist ein test plan",
            query_list=query_ids,
            sql_dict=get_sql_dict(benchmark),
            gen_placeholders_fn=gen_placeholders_fn,
            db_storage=db_storage,
            only_query_txt=False,
            readonly_files_not_git_tracked=readonly_files_not_git_tracked,
            write_non_tracked_only=False,
        )

        # compile
        self.compiler = make_compiler(
            workspace_dir,
            db_storage=db_storage,
            untracked_cpp_runner_content=untracked_cpp_runner_content,
        )

        self.compiler.build()

        # run
        sf_list: list[float] = [1]

        parquet_dir = f"/mnt/labstore/bespoke_olap/{benchmark}_parquet"
        query_validator = QueryValidator(
            benchmark=benchmark,
            gen_query_fn=get_query_gen(benchmark=benchmark),
            sf_list=sf_list,
            parquet_path=parquet_dir,
            wandb_pin_worker=True,
            all_query_ids=query_ids,
            num_random_query_instantiations=10,
            query_cache_dir=cache_dir / "query_cache",
            validate_cache_dir=None,
            workspace_path=workspace_dir,
            git_snapshotter=None,
            runtime_tracker=None,
            do_not_cache=True,
            run_umbra_as_well=False,
            db_storage=db_storage,
            disk_db_dir=disk_db_dir,
        )

        run_tool = RunTool(
            cwd=workspace_dir,
            query_validator=query_validator,
            run_stats_collector=None,
            dataset_name=get_dataset_name(benchmark),
            base_parquet_dir=parquet_dir,
            validate_output_truncation=True,
            compile_output_truncation=True,
            only_from_cache=False,
            parallelism=False,
            core_ids=None,
            db_storage=db_storage,
            bespoke_storage_dir=bespoke_db_dir,
            compiler=self.compiler,
        )

        run_tool.run(
            scale_factor=sf_list[0],
            optimize=True,
            query_id=query_ids,
        )


if __name__ == "__main__":
    unittest.main()
