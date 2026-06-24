from pathlib import Path

from cpp_runner.compiler.compiler_factory import CompilerFactory, FilePaths


class BFFCompilerFactory(CompilerFactory):
    """Compiler factory for the BFF (bespoke file format) use-case.

    Mirrors :class:`OLAPCompilerFactory` but targets the loader -> writer ->
    query pipeline defined in ``db_bff.cpp``:

      * ``libloader.so``  — reads Parquet via Arrow into ``ParquetTables``
        (shared with OLAP; uses ``api/olap/loader_api.cpp``).
      * ``libwriter.so``  — the full ``IngestApi`` plugin (write + read +
        system-binding). The agent implements ``write_impl.cpp``,
        ``read_impl.cpp`` and ``binding_impl.cpp`` in the workspace; together
        with the fixed ``api/bff/ingest_api.cpp`` they encode ``ParquetTables``
        into ``.bff`` files and can read them back.
      * ``libquery.so``   — answers queries by reading from the BFF files. It
        links the shared ``query_api.cpp`` plus the generated ``query_impl.cpp``
        / ``queryN.cpp`` and the agent's ``read_impl.cpp`` so the query code can
        call the BFF read API directly. ``queryN.cpp`` files are auto-discovered
        and appended to this lib by the dynamic compiler.

    ``--no-undefined`` (set in the base Compiler) means every symbol referenced
    inside a ``.so`` must be defined within it, which is why ``read_impl.cpp``
    appears in both the writer and the query lib.
    """

    def _get_libs(
        self,
        file_paths: FilePaths,
    ) -> tuple[dict[str, list[Path | str]], list[Path]]:
        bff_api_dir = file_paths.api_path / "bff"
        olap_api_dir = file_paths.api_path / "olap"

        libs: dict[str, list[Path | str]] = {
            "loader": [
                olap_api_dir / "loader_api.cpp",
                "parquet_reader.cpp",
                file_paths.cpp_helpers_path / "loader_utils.cpp",
            ],
            "writer": [
                bff_api_dir / "ingest_api.cpp",
                "write_impl.cpp",
                "read_impl.cpp",
                "binding_impl.cpp",
                "parquet_reader.cpp",
                file_paths.cpp_helpers_path / "loader_utils.cpp",
            ],
            "query": [
                file_paths.api_path / "query_api.cpp",
                "query_impl.cpp",
                "read_impl.cpp",
                file_paths.cpp_helpers_path / "cpu_affinity.cpp",
            ],
        }

        # db_bff.cpp includes both "loader_api.hpp" (api/olap) and
        # "ingest_api.hpp" (api/bff); expose both directories on the include path.
        include_dirs = [bff_api_dir, olap_api_dir]

        return libs, include_dirs

    def _get_usecase_src(self, file_paths: FilePaths) -> Path:
        return file_paths.db_cpp_path.parent / "db_bff.cpp"
