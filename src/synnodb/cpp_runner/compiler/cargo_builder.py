"""Build a Rust engine workspace with cargo.

Satisfies the same ``EngineBuilder`` protocol as ``Compiler`` (see
engine_builder.py), so the agent-facing tools do not know or care which language
the engine is in: they call ``set_compile_options()`` then ``build()``.

Deliberately thin. Cargo already does incremental compilation and dependency
tracking, so there is nothing here like the g++ driver's object graph, .d files,
or .build_state.json -- reimplementing those on top of cargo would be building
the same thing twice.

The one thing cargo does NOT do for us is put the artifacts where the host looks:
the host dlopens ``./build/lib{loader,builder,query}.so`` relative to the
workspace, so the freshly built .so files are copied there after every build.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The three plugins the host loads. Named after the crates that produce them.
_PLUGINS = ("loader", "builder", "query")

# rustc diagnostics are far longer than gcc's, and RunTool/CompileTool truncate
# what the model sees. "short" keeps one line per error, so a build with several
# errors still fits in the budget instead of being cut off mid-diagnostic.
_MESSAGE_FORMAT = "short"


class CargoBuilder:
    """Builds a Rust engine: the three plugins with cargo, plus the C++ host.

    The host (``./db`` -- db.cpp + the hotpatch pipeline) is framework code and
    contains no engine logic, so it is the SAME binary for a Rust engine as for a
    C++ one and is simply compiled alongside. It has no Arrow dependency: it only
    moves opaque handles between plugins.
    """

    def __init__(self, working_dir: Path):
        self.workdir = Path(working_dir).resolve()
        self.optimize = False
        self.trace_mode = False

    # -- EngineBuilder ---------------------------------------------------------
    def set_compile_options(
        self, optimize: bool = False, trace_mode: bool = False
    ) -> None:
        self.optimize = optimize
        self.trace_mode = trace_mode

    def build(self) -> Optional[str]:
        host_error = self._build_host()
        if host_error is not None:
            return host_error

        cmd = ["cargo", "build", f"--message-format={_MESSAGE_FORMAT}"]
        if self.optimize:
            cmd.append("--release")
        if self.trace_mode:
            # Mirrors -DTRACE: the profiling macros compile away without it.
            cmd += ["--features", "query/trace"]

        logger.info(f"cargo: {' '.join(cmd)} (cwd={self.workdir})")
        proc = subprocess.run(
            cmd,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            # Keep the parent's env (rustup needs PATH/HOME) but make the output
            # deterministic, so an unchanged workspace produces an unchanged build.
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
        )
        if proc.returncode != 0:
            # cargo writes diagnostics to stderr; that text is what the model sees
            # and fixes against, so return it verbatim rather than summarizing.
            return proc.stderr or proc.stdout or "cargo build failed with no output"

        return self._stage_plugins()

    # -- internals -------------------------------------------------------------
    def _build_host(self) -> Optional[str]:
        """Compile ./db, the language-agnostic host that dlopens the plugins."""
        from synnodb.cpp_runner.compiler.compiler import Compiler
        from synnodb.cpp_runner.compiler.compiler_factory import CompilerFactory

        paths = CompilerFactory()._get_paths(self.workdir)
        compiler = Compiler(
            working_dir=self.workdir,
            # No libs: the plugins are cargo's job. This builds only the app.
            libs={},
            main_src=paths.db_cpp_path,
            app_extra_srcs=[
                paths.hotpatch_path / "build_id.cpp",
                paths.db_cpp_path.parent / "db_olap.cpp",
            ],
            include_dirs=[
                paths.api_path,
                paths.cpp_helpers_path,
                paths.hotpatch_path,
                paths.api_path / "olap",
            ],
            build_dir="build",
            link_libs=[],
            # The host moves opaque handles between plugins and never touches
            # Arrow; only the plugins do.
            pkgconfig_libs=[],
        )
        compiler.set_compile_options(optimize=self.optimize)
        return compiler.build()

    def _stage_plugins(self) -> Optional[str]:
        """Copy the built .so files to where the host dlopens them."""
        profile = "release" if self.optimize else "debug"
        target_dir = self.workdir / "target" / profile
        build_dir = self.workdir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        for name in _PLUGINS:
            src = target_dir / f"lib{name}.so"
            if not src.is_file():
                return (
                    f"cargo build succeeded but {src} is missing - the {name} crate "
                    f"must declare crate-type = [\"cdylib\", ...]"
                )
            dst = build_dir / f"lib{name}.so"
            # Replace rather than overwrite: the host may still have the previous
            # .so mapped, and writing into it in place would corrupt a live image.
            # A copy to a temp name + rename swaps the directory entry atomically.
            tmp = build_dir / f".{name}.so.tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)

        return None
