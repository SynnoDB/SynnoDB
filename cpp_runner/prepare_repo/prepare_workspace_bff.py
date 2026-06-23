from pathlib import Path
from string import Template

from conversations.filenames import get_plan_filename
from cpp_runner.prepare_repo.assemble_args_parser import assemble_args_parser_file
from cpp_runner.prepare_repo.assemble_query_impl import assemble_query_impl_file
from cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from cpp_runner.prepare_repo.prepare_workspace_olap import (
    _gen_table_defs,
    _gen_table_reads,
    replace_cpp_marked_block,
)
from utils.cli_config import Usecase
from workloads.workload_provider_bff import BFFWorkloadProvider


class BFFPrepareWorkspace(PrepareWorkspace):
    """Prepare the /output workspace for the BFF (bespoke file format) use-case.

    Mirrors :class:`OLAPPrepareWorkspace` but targets the loader -> writer ->
    query pipeline of ``db_bff.cpp``:

      * The loader reads Parquet into in-memory ``ParquetTables`` (shared
        ``parquet_reader.{hpp,cpp}``).
      * The writer (``write_impl.cpp``) encodes those tables into ``.bff`` files.
      * The reader (``read_impl.cpp`` + ``binding_impl.cpp``) and the generated
        ``queryN.cpp`` read them back to answer the queries.

    Read-only, framework-owned files (``parquet_reader.*``, ``query_impl.*``,
    ``args_parser.hpp``) are routed through the base class' read-only set. The
    files the agent implements (``read_impl.cpp``, ``write_impl.cpp``,
    ``binding_impl.cpp``, ``bff_format.hpp`` and the per-query ``queryN.*``) are
    tracked/editable. The ``api/bff`` headers are also written into the workspace
    as immutable reference for the agent to program against.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        assert isinstance(self.workload_provider, BFFWorkloadProvider), (
            f"Expected workload_provider to be an instance of BFFWorkloadProvider, got {type(self.workload_provider)}"
        )

    def _assemble_usecase_files(
        self, storage_plan: str | None = None, **usecase_args
    ) -> dict[str, str]:
        """Build template file contents without writing to disk."""
        assert isinstance(self.workload_provider, BFFWorkloadProvider), (
            f"Expected workload_provider to be an instance of BFFWorkloadProvider, got {type(self.workload_provider)}"
        )

        project_dir = Path(__file__).parent
        src_dir = project_dir / "templates"
        bff_template_dir = src_dir / "bff"
        bff_api_dir = project_dir.parent / "api" / "bff"

        table_names = self.workload_provider.dataset_tables

        # Framework-owned / read-only files. parquet_reader is shared with the
        # OLAP in-memory loader (Arrow tables in RAM, consumed by the writer).
        result: dict[str, str] = {}

        parquet_reader_hpp = (src_dir / "parquet_reader.hpp").read_text()
        parquet_reader_hpp = replace_cpp_marked_block(
            parquet_reader_hpp,
            "table-defs",
            _gen_table_defs(table_names, persistent_storage=False),
        )
        result["parquet_reader.hpp"] = parquet_reader_hpp

        parquet_reader_cpp = (src_dir / "parquet_reader.cpp").read_text()
        parquet_reader_cpp = replace_cpp_marked_block(
            parquet_reader_cpp,
            "table-reads",
            _gen_table_reads(table_names, persistent_storage=False),
        )
        result["parquet_reader.cpp"] = parquet_reader_cpp

        # BFF query-impl header (no in-memory Database; reads from the .bff files).
        result["query_impl.hpp"] = (bff_template_dir / "query_impl.hpp").read_text()

        # Generated query dispatch + per-query argument parsers.
        result["query_impl.cpp"] = assemble_query_impl_file(
            add_thread_pool_to_query_impl=usecase_args.get(
                "add_thread_pool_to_query_impl", False
            ),
            add_sample_trace_to_query_impl=usecase_args.get("add_sample_trace", False),
            query_list=self.workload_provider.query_ids,
            pin_to_core=3,
            drop_os_caches_for_each_query=False,
        )
        result["args_parser.hpp"] = assemble_args_parser_file(
            query_ids=self.workload_provider.query_ids,
            gen_placeholders_fn=self.workload_provider.get_placeholders_fn(),
            query_name_fn=self.workload_provider._query_key,
        )

        # Agent-editable read/write/binding implementations + shared format header.
        for filename in (
            "read_impl.cpp",
            "write_impl.cpp",
            "binding_impl.cpp",
            "bff_format.hpp",
        ):
            result[filename] = (bff_template_dir / filename).read_text()

        # api/bff reference headers (immutable; agent programs against these).
        for header in (
            "ingest_api.hpp",
            "read_api.hpp",
            "write_api.hpp",
            "filter_pushdown.hpp",
            "system_binding.hpp",
            "ingest_types.hpp",
        ):
            result[header] = (bff_api_dir / header).read_text()

        # Per-query skeletons (queryN.cpp is auto-added to the query lib).
        result.update(self._assemble_query_files(bff_template_dir))

        if storage_plan is not None:
            result[get_plan_filename(Usecase.BFF)] = storage_plan

        result["queries.md"] = self._assemble_queries_md()

        sql_template_list = [
            f"# Query **{q}**:\n```\n{self.workload_provider.sql_dict[f'Q{q}']}\n```\n\n---\n"
            for q in self.workload_provider.query_ids
        ]
        qf_string = "\n".join(sql_template_list)

        result["queries.md"] = qf_string

        return result

    def _assemble_query_files(self, bff_template_dir: Path) -> dict[str, str]:
        """Build per-query queryN.cpp / queryN.hpp from the BFF templates."""
        assert isinstance(self.workload_provider, BFFWorkloadProvider)

        hpp_template = Template((bff_template_dir / "queryX.hpp").read_text())
        cpp_template = Template((bff_template_dir / "queryX.cpp").read_text())

        result: dict[str, str] = {}
        for qid in self.workload_provider.query_ids:
            assert not qid.startswith("Q"), f"Query id should not start with 'Q': {qid}"
            sql = self.workload_provider.sql_dict[
                self.workload_provider._query_key(qid)
            ]
            result[f"query{qid}.hpp"] = hpp_template.substitute(qid=qid)
            result[f"query{qid}.cpp"] = cpp_template.substitute(qid=qid, query_sql=sql)
        return result

    def _assemble_queries_md(self) -> str:
        assert isinstance(self.workload_provider, BFFWorkloadProvider)
        sql_template_list = [
            f"# Query **{q}**:\n```\n{self.workload_provider.sql_dict[self.workload_provider._query_key(q)]}\n```\n\n---\n"
            for q in self.workload_provider.query_ids
        ]
        return "\n".join(sql_template_list)
