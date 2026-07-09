"""Domain objects returned by the pipeline stages.

A stage produces a real artifact — a storage plan is a text document, a base
implementation is a set of generated C++ source files — not just a wandb run id.
These classes carry the artifact's content (read from the run's workspace) plus
its provenance (the wandb ``run_id`` used to chain stages, and the workspace it
was written to). They are immutable snapshots: the content is captured when the
stage finishes, so a later stage reusing the same workspace cannot mutate them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

if TYPE_CHECKING:
    from synnodb.api import SynnoConfig
    from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures


@dataclass(frozen=True)
class RunResult:
    """What a stage's ``main()`` returns: the wandb run id (None unless wandb
    logging was enabled) and the final git snapshot hash of the produced code.
    Either is a valid token to chain the next stage off of — the run id via
    wandb, the snapshot hash directly (same local repo)."""

    run_id: str | None
    snapshot_hash: str | None


@dataclass(frozen=True)
class StageArtifact:
    """Provenance common to every stage output."""

    run_id: str | None  # wandb run id: chaining token + provenance
    workspace: Path  # directory the run wrote its output to
    config: "SynnoConfig"  # the settings the stage ran with
    # Final git snapshot of the produced code. W&B-free chaining token: the next
    # stage can restore it directly from the local workspace repo. kw_only so the
    # existing positional constructors (and their subclass fields) are unaffected.
    snapshot_hash: str | None = field(default=None, kw_only=True)
    # The prepare record of the run that produced this artifact, mirrored from
    # the workspace metadata file (.synnodb_prepare.json) - the workspace, not
    # this artifact, is the source of truth. kw_only so the positional subclass
    # constructors are unaffected.
    prepare_features: "PrepareFeatures | None" = field(default=None, kw_only=True)

    def __bool__(self) -> bool:
        """Truthy when there is a wandb run id to chain off."""
        return self.run_id is not None


@dataclass(frozen=True)
class StoragePlan(StageArtifact):
    """The storage layout document produced by ``createStoragePlan``."""

    path: Path  # storage_plan.txt on disk
    text: str  # its contents, captured when the stage finished

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

    files: Mapping[str, str]  # filename -> source, captured when the stage finished

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


# ---------------------- result builders (workspace -> artifact) -------------
# Each reads the produced artifact out of the run's workspace and wraps it with
# provenance (run_id, snapshot_hash, workspace, config). Uniform signature so a
# ConversationPlan can carry any of them as its ``result``.

ResultBuilder = Callable[
    ["str | None", "str | None", Path, "SynnoConfig"], StageArtifact
]


def _engine_files(workspace: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(workspace.glob("*.cpp")) + sorted(workspace.glob("*.hpp")):
        try:
            files[p.name] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return files


def build_artifact(run_id, snapshot_hash, workspace, config) -> StageArtifact:
    return StageArtifact(run_id, workspace, config, snapshot_hash=snapshot_hash)


def build_storage_plan(run_id, snapshot_hash, workspace, config) -> StoragePlan:
    path = workspace / "storage_plan.txt"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return StoragePlan(
        run_id, workspace, config, path, text, snapshot_hash=snapshot_hash
    )


def build_base_impl(run_id, snapshot_hash, workspace, config) -> BaseImplementation:
    return BaseImplementation(
        run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash
    )


def build_optimized(
    run_id, snapshot_hash, workspace, config
) -> OptimizedImplementation:
    return OptimizedImplementation(
        run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash
    )


def build_multithreaded(
    run_id, snapshot_hash, workspace, config
) -> MultiThreadedImplementation:
    return MultiThreadedImplementation(
        run_id, workspace, config, _engine_files(workspace), snapshot_hash=snapshot_hash
    )


def make_correctness_builder(target_sf: float) -> ResultBuilder:
    """A result builder for a checkSfCorrectness plan at ``target_sf``."""

    def _build(run_id, snapshot_hash, workspace, config) -> CorrectnessReport:
        return CorrectnessReport(
            run_id,
            workspace,
            config,
            float(target_sf),
            snapshot_hash=snapshot_hash,
        )

    return _build
