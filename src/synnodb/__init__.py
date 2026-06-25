"""SynnoDB — programmatic entry point to the agent pipeline."""
from synnodb.api import (
    Stage,
    StageParam,
    SynnoConfig,
    SynnoDB,
    register_stage,
)
from synnodb.results import (
    BaseImplementation,
    CorrectnessReport,
    GeneratedEngine,
    MultiThreadedImplementation,
    OptimizedImplementation,
    StageArtifact,
    StoragePlan,
)

__all__ = [
    # driver + config
    "SynnoDB",
    "SynnoConfig",
    # stage registry (extensibility)
    "Stage",
    "StageParam",
    "register_stage",
    # domain result types
    "StageArtifact",
    "StoragePlan",
    "GeneratedEngine",
    "BaseImplementation",
    "OptimizedImplementation",
    "MultiThreadedImplementation",
    "CorrectnessReport",
]
