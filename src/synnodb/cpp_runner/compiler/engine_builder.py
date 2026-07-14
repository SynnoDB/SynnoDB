"""The toolchain seam: what the rest of the system needs from a build.

``Compiler`` (g++ objects, -MMD dep files, .build_state.json, pkg-config) and a
future ``CargoBuilder`` have nothing in common internally -- cargo does its own
incremental compilation and dependency tracking, so there is nothing to share.
What they *do* share is the tiny surface the agent-facing tools actually use:

    tools/compile.py  -> set_compile_options(optimize=...) ; build()
    tools/run.py      -> the same, before running the binary

So that surface, not the C++ compiler, is the abstraction. A build either
succeeds (``None``) or hands back the error text, which goes straight to the
model as the compile-error feedback it fixes against.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class EngineBuilder(Protocol):
    """Builds the engine in a workspace into ``./db`` + ``build/lib*.so``."""

    def set_compile_options(
        self, optimize: bool = False, trace_mode: bool = False
    ) -> None:
        """Select the build profile before the next :meth:`build`."""
        ...

    def build(self) -> Optional[str]:
        """Build the engine.

        Returns ``None`` on success, otherwise the compiler's error output --
        which is fed back to the model verbatim, so it must be the real
        diagnostics, not a summary.
        """
        ...
