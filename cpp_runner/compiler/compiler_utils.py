import logging
import os
from pathlib import Path
from typing import Optional

from cpp_runner.compiler.compiler_cached import CachedCompiler
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from utils.utils import DBStorage

logger = logging.getLogger(__name__)


def relpath(target: Path, base: Path) -> Path:
    return Path(os.path.relpath(target, base))


class _DynamicQueryCompiler(CachedCompiler):
    """CachedCompiler that rescans the workspace for queryN.cpp files before each build."""

    def __init__(self, args: dict, **kwargs):
        super().__init__(args, **kwargs)

    def _refresh_query_sources(self) -> None:
        # retrieve all query files in the workdir
        query_files = sorted(self.workdir.glob("query[0-9a-z]*.cpp"))

        # make paths relative to the workdir
        query_files = [relpath(p, self.workdir) for p in query_files]

        # add query files to the "query" lib
        # FIX: Filter out stale entries before union. The old code accumulated files
        # that were later deleted (e.g. model creates query8_traced.cpp, deletes it,
        # but self.libs["query"] still has it from the previous _refresh call ->
        # compile crashes with "No such file or directory").
        # Old code:
        # self.libs["query"] = sorted(
        #     set(self.libs["query"] + query_files), key=lambda p: str(p)
        # )
        existing_base = [p for p in self.libs["query"] if (self.workdir / p).exists()]
        self.libs["query"] = sorted(
            set(existing_base + query_files), key=lambda p: str(p)
        )

    def build(self) -> Optional[str]:
        self._refresh_query_sources()
        return super().build()

    def build_cached(self, **kwargs):
        self._refresh_query_sources()
        return super().build_cached(**kwargs)


def make_compiler(
    cwd: Path,
    db_storage: DBStorage,
    untracked_cpp_runner_content: str,
    compile_cache_dir: Optional[Path] = None,
    do_not_cache: bool = False,
    git_snapshotter: Optional[GitSnapshotter] = None,
    runtime_tracker: Optional[RuntimeTracker] = None,
    only_from_cache: bool = False,
) -> CachedCompiler:
    api_path = relpath(
        Path(__file__).parent.parent / "api",
        cwd.resolve(),
    )
    cpp_helpers_path = relpath(
        Path(__file__).parent.parent / "cpp_helpers",
        cwd.resolve(),
    )
    hotpatch_path = relpath(
        Path(__file__).parent.parent / "hotpatch",
        cwd.resolve(),
    )
    db_cpp_path = relpath(
        Path(__file__).parent.parent / "db.cpp",
        cwd.resolve(),
    )

    logger.warning(f"Compiler API path: {api_path}")
    logger.warning(f"Compiler CPP helpers path: {cpp_helpers_path}")
    logger.warning(f"Compiler hotpatch path: {hotpatch_path}")

    libs = {
        "loader": [
            api_path / "loader_api.cpp",
            "parquet_reader.cpp",
            cpp_helpers_path / "loader_utils.cpp",
        ],
        "builder": [
            api_path / "builder_api.cpp",
            "db_loader.cpp",
            cpp_helpers_path / "cpu_affinity.cpp",
        ],
        "query": [
            api_path / "query_api.cpp",
            "query_impl.cpp",
            cpp_helpers_path / "cpu_affinity.cpp",
        ],
    }

    if db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
        libs["builder"].extend(
            [
                "file_loader_utils.cpp",
                "parquet_reader.cpp",
                cpp_helpers_path / "loader_utils.cpp",
            ]
        )

    elif db_storage == DBStorage.IN_MEMORY:
        # file-loader utils not needed
        pass
    else:
        raise ValueError(f"Unsupported db source: {db_storage}")

    args = dict(
        working_dir=cwd,
        libs=libs,
        main_src=db_cpp_path,
        include_dirs=[api_path, cpp_helpers_path, hotpatch_path],
        app_extra_srcs=[hotpatch_path / "build_id.cpp"],
        build_dir="build",
        link_libs=[],
        pkgconfig_libs=["arrow", "parquet"],
    )
    return _DynamicQueryCompiler(
        args=args,
        compile_cache_dir=compile_cache_dir,
        git_snapshotter=git_snapshotter,
        runtime_tracker=runtime_tracker,
        do_not_cache=do_not_cache,
        only_from_cache=only_from_cache,
        untracked_cpp_runner_content=untracked_cpp_runner_content,  # include untracked_cpp_runner_content in the compiler args, so that changes to it will invalidate the cache
    )
