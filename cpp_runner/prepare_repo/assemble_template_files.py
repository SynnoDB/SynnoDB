import re
from pathlib import Path

from workloads.dataset.dataset_tables_dict import get_tables_for_benchmark
from utils.utils import DBStorage


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
    else:
        return "\n".join(
            f'{indent}tables->{name} = ReadParquetTable(path + "{name}.parquet");'
            for name in tables
        )


def build_template_files(benchmark: str, db_storage: DBStorage) -> dict[str, str]:
    """Build template file contents without writing to disk."""
    project_dir = Path(__file__).parent
    src_dir = project_dir / "templates"
    ssd_dir = src_dir / "ssd"

    if db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
        file_sources: list[tuple[str, Path]] = [
            ("parquet_reader.hpp", ssd_dir / "parquet_reader.hpp"),
            ("parquet_reader.cpp", ssd_dir / "parquet_reader.cpp"),
            ("db_loader.hpp", ssd_dir / "db_loader.hpp"),
            ("db_loader.cpp", ssd_dir / "db_loader.cpp"),
            ("file_loader_utils.hpp", ssd_dir / "file_loader_utils.hpp"),
            ("file_loader_utils.cpp", ssd_dir / "file_loader_utils.cpp"),
            ("query_impl.hpp", src_dir / "query_impl.hpp"),
            ("query_impl.cpp", src_dir / "query_impl.cpp"),
            ("buffer_pool.hpp", ssd_dir / "buffer_pool.hpp"),
            ("column_handle.hpp", ssd_dir / "column_handle.hpp"),
        ]
        persistent_storage = True
    elif db_storage == DBStorage.IN_MEMORY:
        file_sources = [
            ("parquet_reader.hpp", src_dir / "parquet_reader.hpp"),
            ("parquet_reader.cpp", src_dir / "parquet_reader.cpp"),
            ("db_loader.hpp", src_dir / "db_loader.hpp"),
            ("db_loader.cpp", src_dir / "db_loader.cpp"),
            ("query_impl.hpp", src_dir / "query_impl.hpp"),
            ("query_impl.cpp", src_dir / "query_impl.cpp"),
        ]
        persistent_storage = False
    else:
        raise ValueError(f"Unsupported db source: {db_storage}")

    tables = get_tables_for_benchmark(benchmark)

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
                    tables,
                    persistent_storage=persistent_storage,
                ),
            )
        elif filename == "parquet_reader.cpp":
            file_content = replace_cpp_marked_block(
                file_content,
                "table-reads",
                _gen_table_reads(
                    tables,
                    persistent_storage=persistent_storage,
                ),
            )

        result[filename] = file_content

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
