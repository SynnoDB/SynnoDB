from pathlib import Path

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
        ssd_dir = src_dir / "olap" / "ssd"

        file_sources = [
            ("parquet_reader.hpp", src_dir / "parquet_reader.hpp"),
            ("parquet_reader.cpp", src_dir / "parquet_reader.cpp"),
            ("db_loader.hpp", src_dir / "db_loader.hpp"),
            ("db_loader.cpp", src_dir / "db_loader.cpp"),
            ("query_impl.hpp", src_dir / "query_impl.hpp"),
        ]

        assert isinstance(self.workload_provider, BFFWorkloadProvider), (
            f"Expected workload_provider to be an instance of BFFWorkloadProvider, got {type(self.workload_provider)}"
        )

        result: dict[str, str] = {}
        for filename, source_path in file_sources:
            if not source_path.is_file():
                raise FileNotFoundError(f"Source file not found: {source_path}")

            file_content = source_path.read_text()
            result[filename] = file_content

        if storage_plan is not None:
            result["storage_plan.txt"] = storage_plan

        sql_template_list = [
            f"# Query **{q}**:\n```\n{self.workload_provider.sql_dict[f'Q{q}']}\n```\n\n---\n"
            for q in self.workload_provider.query_ids
        ]
        qf_string = "\n".join(sql_template_list)

        result["queries.md"] = qf_string

        return result
