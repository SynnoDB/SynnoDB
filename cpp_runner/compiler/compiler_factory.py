import os
from dataclasses import dataclass
from pathlib import Path

from cpp_runner.compiler.compiler_utils import _DynamicQueryCompiler
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker


@dataclass
class FilePaths:
    api_path: Path
    cpp_helpers_path: Path
    hotpatch_path: Path
    db_cpp_path: Path


class CompilerFactory:
    def _get_paths(self, cwd: Path):
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

        file_paths = FilePaths(
            api_path=api_path,
            cpp_helpers_path=cpp_helpers_path,
            hotpatch_path=hotpatch_path,
            db_cpp_path=db_cpp_path,
        )

        return file_paths

    def _get_libs(
        self,
        file_paths: FilePaths,
    ) -> tuple[dict[str, list[Path | str]], list[Path]]:
        raise NotImplementedError

    def make_compiler(
        self,
        cwd: Path,
        untracked_cpp_runner_content: str,
        compile_cache_dir: Path | None = None,
        do_not_cache: bool = False,
        git_snapshotter: GitSnapshotter | None = None,
        runtime_tracker: RuntimeTracker | None = None,
        only_from_cache: bool = False,
    ):
        file_paths = CompilerFactory()._get_paths(cwd)

        libs, include_dirs = self._get_libs(file_paths)

        args = dict(
            working_dir=cwd,
            libs=libs,
            main_src=file_paths.db_cpp_path,
            include_dirs=[
                file_paths.api_path,
                file_paths.cpp_helpers_path,
                file_paths.hotpatch_path,
            ]
            + include_dirs,
            app_extra_srcs=[file_paths.hotpatch_path / "build_id.cpp"],
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


def relpath(target: Path, base: Path) -> Path:
    return Path(os.path.relpath(target, base))
