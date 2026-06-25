from __future__ import annotations

from utils.cli_config import Usecase

PLAN_FILENAME_BY_USECASE = {
    Usecase.OLAP: "storage_plan.txt",
    Usecase.BFF: "file_format_plan.txt",
}


def get_plan_filename(usecase: Usecase = Usecase.OLAP) -> str:
    try:
        return PLAN_FILENAME_BY_USECASE[usecase]
    except KeyError as exc:
        raise ValueError(f"Unsupported usecase: {usecase}") from exc


def get_filenames(usecase: Usecase = Usecase.OLAP) -> dict[str, str]:
    queries_path = "queries.md"
    if usecase == Usecase.BFF:
        # BFF use-case: the agent implements the bespoke file-format writer in
        # write_impl.cpp, the reader in read_impl.cpp, and declares the concrete
        # on-disk format handles in bff_format.hpp.
        builder_path = "write_impl.cpp"
        builder_cpp_path = "write_impl.cpp"
        builder_hpp_path = "bff_format.hpp"
    elif usecase == Usecase.OLAP:
        builder_path = "db_loader.hpp/db_loader.cpp"
        builder_cpp_path = "db_loader.cpp"
        builder_hpp_path = "db_loader.hpp"
    else:
        raise ValueError(f"Unsupported usecase: {usecase}")

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
