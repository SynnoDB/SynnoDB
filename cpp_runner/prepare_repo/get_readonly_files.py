def get_readonly_files() -> tuple[set[str], set[str]]:
    """Get a list of files in the workspace that should be treated as read-only."""

    readonly_files_not_git_tracked = {
        "args_parser.hpp",
        "query_impl.hpp",
        "query_impl.cpp",
        "parquet_reader.hpp",
        "parquet_reader.cpp",
    }

    readonly_files_to_be_git_tracked = {
        "queries.txt",
    }

    return readonly_files_not_git_tracked, readonly_files_to_be_git_tracked
