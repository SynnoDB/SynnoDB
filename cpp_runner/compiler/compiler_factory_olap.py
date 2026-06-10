from pathlib import Path

from cpp_runner.compiler.compiler_factory import CompilerFactory, FilePaths
from utils.utils import DBStorage


class OLAPCompilerFactory(CompilerFactory):
    def __init__(self, db_storage: DBStorage):
        self.db_storage = db_storage
        super().__init__()

    def _get_libs(
        self,
        file_paths: FilePaths,
    ) -> tuple[dict[str, list[Path | str]], list[Path]]:
        libs = {
            "loader": [
                file_paths.api_path / "olap" / "loader_api.cpp",
                "parquet_reader.cpp",
                file_paths.cpp_helpers_path / "loader_utils.cpp",
            ],
            "builder": [
                file_paths.api_path / "olap" / "builder_api.cpp",
                "db_loader.cpp",
                file_paths.cpp_helpers_path / "cpu_affinity.cpp",
            ],
            "query": [
                file_paths.api_path / "query_api.cpp",
                "query_impl.cpp",
                file_paths.cpp_helpers_path / "cpu_affinity.cpp",
            ],
        }

        if self.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
            libs["builder"].extend(
                [
                    "file_loader_utils.cpp",
                    "parquet_reader.cpp",
                    file_paths.cpp_helpers_path / "loader_utils.cpp",
                ]
            )

        elif self.db_storage == DBStorage.IN_MEMORY:
            # file-loader utils not needed
            pass
        else:
            raise ValueError(f"Unsupported db source: {self.db_storage}")

        include_dirs = [file_paths.api_path / "olap"]

        return libs, include_dirs
