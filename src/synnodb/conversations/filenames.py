from __future__ import annotations

from dataclasses import dataclass

from synnodb.utils.cli_config import Usecase

PLAN_FILENAME_BY_USECASE = {
    Usecase.OLAP: "storage_plan.txt",
}


def get_plan_filename(usecase: Usecase = Usecase.OLAP) -> str:
    try:
        return PLAN_FILENAME_BY_USECASE[usecase]
    except KeyError as exc:
        raise ValueError(f"Unsupported usecase: {usecase}") from exc


@dataclass(frozen=True)
class Filenames:
    """The well-known filenames of a prepared workspace, typed.

    This is the curated view exposed on ``ConvContext`` (``ctx.filenames``);
    ``get_filenames()`` remains for internal callers that predate it.
    """

    queries_path: str
    builder_path: str
    builder_hpp_path: str
    builder_cpp_path: str
    query_impl_path: str
    args_path: str
    base_impl_todo_filename: str
    plan_filename: str
    thread_pool_filename: str

    @property
    def storage_plan_filename(self) -> str:
        return self.plan_filename

    @classmethod
    def for_usecase(cls, usecase: Usecase = Usecase.OLAP) -> "Filenames":
        names = get_filenames(usecase)
        return cls(
            queries_path=names["queries_path"],
            builder_path=names["builder_path"],
            builder_hpp_path=names["builder_hpp_path"],
            builder_cpp_path=names["builder_cpp_path"],
            query_impl_path=names["query_impl_path"],
            args_path=names["args_path"],
            base_impl_todo_filename=names["base_impl_todo_filename"],
            plan_filename=names["plan_filename"],
            thread_pool_filename=names["thread_pool_filename"],
        )


def get_filenames(usecase: Usecase = Usecase.OLAP) -> dict[str, str]:
    queries_path = "queries.md"
    if usecase == Usecase.OLAP:
        builder_path = "db_loader.hpp/db_loader.cpp"
        builder_cpp_path = "db_loader.cpp"
        builder_hpp_path = "db_loader.hpp"
    else:
        raise ValueError(
            f"Unsupported usecase: {usecase}, {type(usecase)}",
        )

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
