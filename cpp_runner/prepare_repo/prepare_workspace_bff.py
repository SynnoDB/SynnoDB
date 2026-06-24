from pathlib import Path

from conversations.filenames import get_plan_filename
from cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from workloads.workload_provider_bff import BFFWorkloadProvider


class BFFPrepareWorkspace(PrepareWorkspace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        assert isinstance(self.workload_provider, BFFWorkloadProvider), (
            f"Expected workload_provider to be an instance of BFFWorkloadProvider, got {type(self.workload_provider)}"
        )

    def _assemble_usecase_files(
        self, storage_plan: str | None = None
    ) -> dict[str, str]:
        """Build template file contents without writing to disk."""
        project_dir = Path(__file__).parent
        src_dir = project_dir / "templates"
        bff_api_dir = project_dir.parent / "api" / "bff"

        file_sources = [
            ("parquet_reader.hpp", src_dir / "parquet_reader.hpp"),
            ("parquet_reader.cpp", src_dir / "parquet_reader.cpp"),
            ("db_loader.hpp", src_dir / "db_loader.hpp"),
            ("db_loader.cpp", src_dir / "db_loader.cpp"),
            ("read_api.hpp", bff_api_dir / "read_api.hpp"),
            ("write_api.hpp", bff_api_dir / "write_api.hpp"),
            ("filter_pushdown.hpp", bff_api_dir / "filter_pushdown.hpp"),
            ("system_binding.hpp", bff_api_dir / "system_binding.hpp"),
            ("ingest_types.hpp", bff_api_dir / "ingest_types.hpp"),
        ]

        assert isinstance(self.workload_provider, BFFWorkloadProvider), (
            f"Expected workload_provider to be an instance of BFFWorkloadProvider, got {type(self.workload_provider)}"
        )

        result: dict[str, str] = {}
        for filename, source_path in file_sources:
            if not source_path.is_file():
                raise FileNotFoundError(f"Source file not found: {source_path}")

            file_content = source_path.read_text()
            if filename == "query_impl.hpp":
                file_content = _add_bff_ingest_include(file_content)
            result[filename] = file_content

        if storage_plan is not None:
            result[get_plan_filename("bff")] = storage_plan

        sql_template_list = [
            f"# Query **{q}**:\n```\n{self.workload_provider.sql_dict[self.workload_provider._query_key(q)]}\n```\n\n---\n"
            for q in self.workload_provider.query_ids
        ]
        qf_string = "\n".join(sql_template_list)

        result["queries.md"] = qf_string

        return result


def _add_bff_ingest_include(file_content: str) -> str:
    include_line = '#include "ingest_api.hpp"'
    if include_line in file_content:
        return file_content

    anchor = '#include "query_api.hpp"'
    if anchor not in file_content:
        raise ValueError("query_impl.hpp template is missing query_api.hpp include")

    return file_content.replace(anchor, anchor + "\n" + include_line, 1)
