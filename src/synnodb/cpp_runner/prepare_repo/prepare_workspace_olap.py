import re
from pathlib import Path
from string import Template

from synnodb.conversations.filenames import get_plan_filename
from synnodb.cpp_runner.prepare_repo.assemble_args_parser import (
    assemble_args_parser_file,
)
from synnodb.cpp_runner.prepare_repo.assemble_query_impl import assemble_query_impl_file
from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.utils.cli_config import Usecase
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider


class OLAPPrepareWorkspace(PrepareWorkspace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        assert isinstance(self.workload_provider, OLAPWorkloadProvider), (
            f"Expected workload_provider to be an instance of OLAPWorkloadProvider, got {type(self.workload_provider)}"
        )

    @property
    def plan_filename(self) -> str:
        return get_plan_filename(Usecase.OLAP)

    def build_scaffold_files(self, features: PrepareFeatures) -> dict[str, str]:
        """Build template file contents without writing to disk."""
        project_dir = Path(__file__).parent
        src_dir = project_dir / "templates"
        ssd_dir = src_dir / "olap" / "ssd"

        persistent_storage = features.storage == "ssd"
        if persistent_storage:
            file_sources: list[tuple[str, Path]] = [
                ("parquet_reader.hpp", ssd_dir / "parquet_reader.hpp"),
                ("parquet_reader.cpp", ssd_dir / "parquet_reader.cpp"),
                ("db_loader.hpp", ssd_dir / "db_loader.hpp"),
                ("db_loader.cpp", ssd_dir / "db_loader.cpp"),
                ("file_loader_utils.hpp", ssd_dir / "file_loader_utils.hpp"),
                ("file_loader_utils.cpp", ssd_dir / "file_loader_utils.cpp"),
                ("buffer_pool.hpp", ssd_dir / "buffer_pool.hpp"),
                ("column_handle.hpp", ssd_dir / "column_handle.hpp"),
                # query_impl.hpp is storage-agnostic (the per-variant types come
                # in through db_loader.hpp); there is no ssd-specific copy.
                ("query_impl.hpp", src_dir / "query_impl.hpp"),
            ]
        else:
            file_sources = [
                ("parquet_reader.hpp", src_dir / "parquet_reader.hpp"),
                ("parquet_reader.cpp", src_dir / "parquet_reader.cpp"),
                ("db_loader.hpp", src_dir / "db_loader.hpp"),
                ("db_loader.cpp", src_dir / "db_loader.cpp"),
                ("query_impl.hpp", src_dir / "query_impl.hpp"),
            ]

        # Copy the ingress/egress helpers into the workspace (they are also on the compiler include
        # path). This lets the agent READ exactly what each helper does - and, since they are not in
        # the read-only set, ADAPT one if a column needs handling it does not yet cover (e.g. a
        # wider accumulator for a value that overflows int64). The quoted #include in
        # db_loader.cpp / queryX.cpp resolves to this workspace copy first.
        cpp_helpers_dir = project_dir.parent / "cpp_helpers"
        file_sources += [
            ("column_ingest.hpp", cpp_helpers_dir / "column_ingest.hpp"),
            ("column_egress.hpp", cpp_helpers_dir / "column_egress.hpp"),
        ]
        # The pool headers are scaffold prerequisites for both storage variants:
        # the generated queryX.cpp includes query_pool.hpp unconditionally (and
        # query_pool.hpp includes thread_pool.hpp). Both are read-only untracked
        # support files; whether query_impl.cpp actually *uses* the pool is the
        # parallel_ready_impl feature.
        file_sources += [
            ("thread_pool.hpp", src_dir / "thread_pool.hpp"),
            ("query_pool.hpp", src_dir / "query_pool.hpp"),
        ]

        assert isinstance(self.workload_provider, OLAPWorkloadProvider), (
            f"Expected workload_provider to be an instance of OLAPWorkloadProvider, got {type(self.workload_provider)}"
        )
        table_names = self.workload_provider.dataset_tables

        result: dict[str, str] = {}
        for filename, source_path in file_sources:
            if not source_path.is_file():
                raise FileNotFoundError(f"Source file not found: {source_path}")

            file_content = source_path.read_text()

            if filename == "parquet_reader.hpp":
                file_content = replace_cpp_marked_block(
                    file_content,
                    "table-defs",
                    _gen_table_defs(
                        table_names,
                        persistent_storage=persistent_storage,
                    ),
                )
            elif filename == "parquet_reader.cpp":
                file_content = replace_cpp_marked_block(
                    file_content,
                    "table-reads",
                    _gen_table_reads(
                        table_names,
                        persistent_storage=persistent_storage,
                    ),
                )

            result[filename] = file_content

        sql_template_list = [
            f"# Query **{q}**:\n```\n{self.workload_provider.sql_dict[f'Q{q}']}\n```\n\n---\n"
            for q in self.workload_provider.query_ids
        ]
        qf_string = "\n".join(sql_template_list)

        result["queries.md"] = qf_string

        result.update(self._assemble_query_files())

        # assemble
        general_files = dict()
        general_files["query_impl.cpp"] = assemble_query_impl_file(
            add_thread_pool_to_query_impl=features.parallel_ready_impl,
            tracing=features.tracing,
            add_sample_trace_to_query_impl=features.sample_trace,
            query_list=self.workload_provider.query_ids,
            pin_to_core=3,
            drop_os_caches_for_each_query=False,
        )

        general_files["args_parser.hpp"] = assemble_args_parser_file(
            query_ids=self.workload_provider.query_ids,
            gen_placeholders_fn=self.workload_provider.get_placeholders_fn(),
        )

        # assert no filename conflicts between file dicts
        assert not set(result.keys()) & set(general_files.keys()), (
            f"Filename conflict between usecase_files and general_files: {set(result.keys()) & set(general_files.keys())}"
        )

        return {**result, **general_files}

    def _assemble_query_files(self) -> dict[str, str]:
        """Build per-query file contents without writing to disk."""
        result: dict[str, str] = {}

        # generate queryX.hpp files from template:
        template_path = Path(__file__).parent / "templates" / "olap" / "queryX.hpp"
        template_str = template_path.read_text()
        template = Template(template_str)

        for qid in self.workload_provider.query_ids:
            result[f"query{qid}.hpp"] = template.substitute(qid=qid)

        # generate queryX.cpp files from template:
        template_path = Path(__file__).parent / "templates" / "olap" / "queryX.cpp"
        template_str = template_path.read_text()
        template = Template(template_str)

        for qid in self.workload_provider.query_ids:
            assert not qid.startswith("Q"), f"Query id should not start with 'Q': {qid}"
            result[f"query{qid}.cpp"] = template.substitute(
                qid=qid,
                query_sql=self.workload_provider.sql_dict[f"Q{qid}"],
            )

        return result


def replace_cpp_marked_block(text, marker_name, replacement):
    name = re.escape(marker_name)

    pattern = re.compile(
        rf"""(?ms)
        ^[ \t]*//[ \t]*start:[ \t]*{name}[ \t]*\r?\n?
        .*?
        ^[ \t]*//[ \t]*end:[ \t]*{name}[ \t]*(?:\r?\n|$)
        """,
        re.VERBOSE,
    )

    if replacement and not replacement.endswith(("\n", "\r\n")):
        replacement += "\n"

    result, n = pattern.subn(replacement, text, count=1)

    if n != 1:
        raise ValueError(f"expected exactly one replacement, got {n}")

    return result


def _gen_table_defs(tables: list[str], persistent_storage: bool) -> str:
    indent = " " * 4
    if persistent_storage:
        return "\n".join(f"{indent}std::string {name}_path;" for name in tables)
    else:
        return "\n".join(f"{indent}ArrowTable {name};" for name in tables)


def _gen_table_reads(tables: list[str], persistent_storage: bool) -> str:
    indent = " " * 4
    if persistent_storage:
        return "\n".join(
            f'{indent}tables->{name}_path = path + "{name}.parquet";' for name in tables
        )
    # In-memory plane: read each table from parquet, unless the shm hot-load is active
    # (SYNNODB_SHM_INGEST set), in which case map it zero-copy from its /dev/shm Arrow
    # segment. One binary serves both planes; the choice is made at run time by env.
    shm_reads = "\n".join(
        f"{indent}{indent}tables->{name} = "
        f'synnodb::ReadArrowTableFromShm(synnodb::shm_ingest_path_for("{name}"));'
        for name in tables
    )
    parquet_reads = "\n".join(
        f'{indent}{indent}tables->{name} = ReadParquetTable(path + "{name}.parquet");'
        for name in tables
    )
    return (
        f"{indent}if (synnodb::shm_ingest_enabled()) {{\n"
        f"{shm_reads}\n"
        f"{indent}}} else {{\n"
        f"{parquet_reads}\n"
        f"{indent}}}"
    )
