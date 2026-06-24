from __future__ import annotations

PLAN_FILENAME_BY_USECASE = {
    "olap": "storage_plan.txt",
    "bff": "file_format_plan.txt",
}


def _usecase_value(usecase: str | object) -> str:
    return getattr(usecase, "value", usecase)


def get_plan_filename(usecase: str | object = "olap") -> str:
    usecase_value = _usecase_value(usecase)
    try:
        return PLAN_FILENAME_BY_USECASE[usecase_value]
    except KeyError as exc:
        raise ValueError(f"Unsupported usecase: {usecase}") from exc


def get_filenames(usecase: str | object = "olap") -> dict[str, str]:
    queries_path = "queries.md"
    if _usecase_value(usecase) == "bff":
        # BFF use-case: the agent implements the bespoke file-format writer in
        # write_impl.cpp, the reader in read_impl.cpp, and declares the concrete
        # on-disk format handles in bff_format.hpp.
        builder_path = "write_impl.cpp"
        builder_cpp_path = "write_impl.cpp"
        builder_hpp_path = "bff_format.hpp"
    else:
        builder_path = "db_loader.hpp/db_loader.cpp"
        builder_cpp_path = "db_loader.cpp"
        builder_hpp_path = "db_loader.hpp"
    query_impl_path = "query_impl.cpp"
    args_path = "args_parser.hpp"
    base_impl_todo_filename = "base_impl_todo.txt"
    plan_filename = get_plan_filename(usecase)
    thread_pool_filename = "thread_pool.hpp"

    return {
        "queries_path": queries_path,
        "builder_path": builder_path,
        "builder_hpp_path": builder_hpp_path,
        "builder_cpp_path": builder_cpp_path,
        "query_impl_path": query_impl_path,
        "args_path": args_path,
        "base_impl_todo_filename": base_impl_todo_filename,
        "plan_filename": plan_filename,
        "storage_plan_filename": plan_filename,
        "thread_pool_filename": thread_pool_filename,
    }
