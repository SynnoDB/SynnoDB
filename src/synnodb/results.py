"""Domain objects returned by the pipeline stages.

A stage produces a real artifact — a storage plan is a text document, a base
implementation is a set of generated C++ source files — not just a wandb run id.
These classes carry the artifact's content (read from the run's workspace) plus
its provenance (the wandb ``run_id`` used to chain stages, and the workspace it
was written to). They are immutable snapshots: the content is captured when the
stage finishes, so a later stage reusing the same workspace cannot mutate them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from synnodb.api import SynnoConfig


@dataclass(frozen=True)
class StageArtifact:
    """Provenance common to every stage output."""

    run_id: str | None          # wandb run id: chaining token + provenance
    workspace: Path             # directory the run wrote its output to
    config: "SynnoConfig"       # the settings the stage ran with

    def __bool__(self) -> bool:
        """Truthy when there is a wandb run id to chain off."""
        return self.run_id is not None


@dataclass(frozen=True)
class StoragePlan(StageArtifact):
    """The storage layout document produced by ``createStoragePlan``."""

    path: Path                  # storage_plan.txt on disk
    text: str                   # its contents, captured when the stage finished

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return (
            f"StoragePlan(run_id={self.run_id!r}, path={self.path.as_posix()!r}, "
            f"{len(self.text)} chars)"
        )


@dataclass(frozen=True)
class GeneratedEngine(StageArtifact):
    """A generated C++ engine (base / optimized / multi-threaded variants)."""

    files: Mapping[str, str]    # filename -> source, captured when the stage finished

    def file(self, name: str) -> str:
        """Source of a generated file (e.g. 'db_loader.cpp', 'query_impl.cpp')."""
        return self.files[name]

    @property
    def loader(self) -> str:
        return self.files.get("db_loader.cpp", "")

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.files)[:4])
        more = "" if len(self.files) <= 4 else f", +{len(self.files) - 4} more"
        return f"{type(self).__name__}(run_id={self.run_id!r}, files=[{names}{more}])"


@dataclass(frozen=True)
class BaseImplementation(GeneratedEngine):
    """The correct, build-optimized engine from ``createBaseImpl``."""


@dataclass(frozen=True)
class OptimizedImplementation(GeneratedEngine):
    """The performance-optimized engine from ``runOptimLoop``."""


@dataclass(frozen=True)
class MultiThreadedImplementation(GeneratedEngine):
    """The multi-threaded engine from ``addMultiThreading``."""


@dataclass(frozen=True)
class CorrectnessReport(StageArtifact):
    """Result of validating an engine at a larger scale factor."""

    target_sf: float

    def __repr__(self) -> str:
        return f"CorrectnessReport(run_id={self.run_id!r}, target_sf={self.target_sf})"
