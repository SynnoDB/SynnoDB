from __future__ import annotations

from dataclasses import dataclass

from synnodb.utils.cli_config import Usecase
from synnodb.utils.utils import EngineLang

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

    The names depend on the engine language: prompts read them from here rather
    than hardcoding ``db_loader.cpp``, so the same prompt document works for a
    C++ and a Rust workspace.
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
    # The per-query implementation file, as a pattern with a ``{qid}`` slot
    # (e.g. "query{qid}.cpp"). Rendered by :meth:`query_file`.
    query_file_pattern: str
    # How a group of query files is referred to in prose, e.g. "query*.cpp".
    query_file_glob: str

    def query_file(self, query_id: str | int) -> str:
        return self.query_file_pattern.format(qid=query_id)

    @property
    def storage_plan_filename(self) -> str:
        return self.plan_filename

    @classmethod
    def for_usecase(
        cls,
        usecase: Usecase = Usecase.OLAP,
        language: EngineLang = EngineLang.CPP,
    ) -> "Filenames":
        names = get_filenames(usecase, language)
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
            query_file_pattern=names["query_file_pattern"],
            query_file_glob=names["query_file_glob"],
        )


def get_filenames(
    usecase: Usecase = Usecase.OLAP,
    language: EngineLang = EngineLang.CPP,
) -> dict[str, str]:
    if usecase != Usecase.OLAP:
        raise ValueError(
            f"Unsupported usecase: {usecase}, {type(usecase)}",
        )

    queries_path = "queries.md"
    base_impl_todo_filename = "base_impl_todo.txt"
    plan_filename = get_plan_filename(usecase)

    if language == EngineLang.CPP:
        builder_hpp_path = "db_loader.hpp"
        builder_cpp_path = "db_loader.cpp"
        builder_path = "db_loader.hpp/db_loader.cpp"
        query_impl_path = "query_impl.cpp"
        args_path = "args_parser.hpp"
        thread_pool_filename = "thread_pool.hpp"
        query_file_pattern = "query{qid}.cpp"
        query_file_glob = "query*.cpp"
    elif language == EngineLang.RUST:
        # The Rust workspace is a cargo workspace of three cdylib crates
        # (loader / builder / query) behind the same C plugin ABI. There is no
        # header/impl split, so builder_hpp_path and builder_cpp_path are the
        # same file; builder_path is what prompts name when they mean "the
        # storage layer".
        builder_hpp_path = "builder/src/lib.rs"
        builder_cpp_path = "builder/src/lib.rs"
        builder_path = "builder/src/lib.rs"
        query_impl_path = "query/src/lib.rs"
        args_path = "query/src/args.rs"
        thread_pool_filename = "query/src/pool.rs"
        query_file_pattern = "query/src/q{qid}.rs"
        query_file_glob = "query/src/q*.rs"
    else:
        raise ValueError(f"Unsupported engine language: {language}")

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
        "query_file_pattern": query_file_pattern,
        "query_file_glob": query_file_glob,
    }
