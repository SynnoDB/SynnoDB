"""Pick the toolchain and the scaffold for the run's language.

The two places the language actually forks. Everything downstream of a built
engine -- the host binary, the pipe protocol, the router, the validator -- is
language-agnostic, so these are the only selections that need to happen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synnodb.cpp_runner.compiler.engine_builder import EngineBuilder
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.utils.utils import DBStorage, EngineLang


def make_prepare_workspace(
    language: EngineLang,
    *,
    workload_provider: Any,
    workspace_dir: Path,
    git_snapshotter: Any,
    db_storage: DBStorage,
    prepare_cache_dir: Path | None = None,
) -> PrepareWorkspace:
    """The scaffold writer for the run's language."""
    kwargs = dict(
        workload_provider=workload_provider,
        workspace_dir=workspace_dir,
        git_snapshotter=git_snapshotter,
        db_storage=db_storage,
        prepare_cache_dir=prepare_cache_dir,
    )
    if language == EngineLang.CPP:
        from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import (
            OLAPPrepareWorkspace,
        )

        return OLAPPrepareWorkspace(**kwargs)

    if language == EngineLang.RUST:
        from synnodb.cpp_runner.prepare_repo.prepare_workspace_rust import (
            RustPrepareWorkspace,
        )

        return RustPrepareWorkspace(**kwargs)

    raise ValueError(f"Unsupported engine language: {language}")


def make_engine_builder(
    language: EngineLang,
    *,
    cwd: Path,
    db_storage: DBStorage,
    untracked_cpp_runner_content: str,
    compile_cache_dir: Path | None = None,
    do_not_cache: bool = False,
    git_snapshotter: Any = None,
    runtime_tracker: Any = None,
    only_from_cache: bool = False,
) -> EngineBuilder:
    """The build toolchain for the run's language.

    Both satisfy ``EngineBuilder`` (set_compile_options + build), which is all the
    agent-facing tools use -- so CompileTool and RunTool never branch on language.
    """
    if language == EngineLang.CPP:
        from synnodb.cpp_runner.compiler.compiler_factory_olap import (
            OLAPCompilerFactory,
        )

        return OLAPCompilerFactory(db_storage=db_storage).make_compiler(
            cwd=cwd,
            untracked_cpp_runner_content=untracked_cpp_runner_content,
            compile_cache_dir=compile_cache_dir,
            do_not_cache=do_not_cache,
            git_snapshotter=git_snapshotter,
            runtime_tracker=runtime_tracker,
            only_from_cache=only_from_cache,
        )

    if language == EngineLang.RUST:
        from synnodb.cpp_runner.compiler.cargo_builder import CargoBuilder

        # No CachedCompiler wrapper yet: cargo already does incremental builds,
        # and the content-addressed compile cache is an optimization, not a
        # correctness requirement. Wiring CargoBuilder into CachedCompiler is a
        # follow-up (its key is language-neutral, so it will drop straight in).
        return CargoBuilder(working_dir=cwd)

    raise ValueError(f"Unsupported engine language: {language}")
