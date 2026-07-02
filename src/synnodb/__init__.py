"""SynnoDB — a DuckDB drop-in (router-fronted) and the engine factory behind it.

Two faces, one package:

* **The drop-in runtime** (light: ``duckdb``, ``pyarrow``, ``sqlglot``). ``import
  synnodb as duckdb`` and use ``connect`` / ``sql`` / ``execute`` exactly as DuckDB.
  Re-exports DuckDB's whole public namespace; with no engines registered, behavior
  is byte-identical to DuckDB.

* **The agent factory** that *generates* bespoke engines (``SynnoDB``, ``SynnoConfig``,
  the stages, the result artifacts). Heavy; needs the ``synnodb[factory]`` extra.
  These names are imported **lazily** (PEP 562 ``__getattr__``) so the drop-in stays
  light and importable without the LLM stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# The light drop-in surface (DuckDB namespace + connect/sql/execute + router types).
from synnodb import duckdb_compat as _compat
from synnodb.duckdb_compat import *  # noqa: F401,F403  (re-export DuckDB + drop-in API)
from synnodb.router import RouterMode, enable_debug_logging

if TYPE_CHECKING:  # for type-checkers/IDEs only; runtime resolves these lazily.
    from synnodb.api import SynnoConfig, SynnoDB
    from synnodb.conversations.conv_context import ConvContext
    from synnodb.conversations.stage_items import (
        AssertCorrect,
        Benchmark,
        Compact,
        DynamicStageConfig,
        MeasureBaselines,
        PerQueryLoop,
        PromptStage,
        StageItem,
        SupervisionHorizon,
        ValidateOff,
        ValidateOn,
        ValidateStdoutOff,
        ValidateStdoutOn,
    )
    from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
    from synnodb.plan import ConversationPlan, SupervisionPolicy
    from synnodb.results import (
        BaseImplementation,
        CorrectnessReport,
        GeneratedEngine,
        MultiThreadedImplementation,
        OptimizedImplementation,
        StageArtifact,
        StoragePlan,
    )

# name -> submodule it lives in; imported on first access only.
_LAZY_FACTORY = {
    "optimize_database": "synnodb.optimize",
    "SynnoDB": "synnodb.api",
    "SynnoConfig": "synnodb.api",
    "ConversationPlan": "synnodb.plan",
    "SupervisionPolicy": "synnodb.plan",
    "PrepareFeatures": "synnodb.cpp_runner.prepare_repo.prepare_features",
    "ConvContext": "synnodb.conversations.conv_context",
    "StageItem": "synnodb.conversations.stage_items",
    "PromptStage": "synnodb.conversations.stage_items",
    "DynamicStageConfig": "synnodb.conversations.stage_items",
    "PerQueryLoop": "synnodb.conversations.stage_items",
    "AssertCorrect": "synnodb.conversations.stage_items",
    "MeasureBaselines": "synnodb.conversations.stage_items",
    "Compact": "synnodb.conversations.stage_items",
    "Benchmark": "synnodb.conversations.stage_items",
    "ValidateOn": "synnodb.conversations.stage_items",
    "ValidateOff": "synnodb.conversations.stage_items",
    "ValidateStdoutOn": "synnodb.conversations.stage_items",
    "ValidateStdoutOff": "synnodb.conversations.stage_items",
    "SupervisionHorizon": "synnodb.conversations.stage_items",
    "StageArtifact": "synnodb.results",
    "StoragePlan": "synnodb.results",
    "GeneratedEngine": "synnodb.results",
    "BaseImplementation": "synnodb.results",
    "OptimizedImplementation": "synnodb.results",
    "MultiThreadedImplementation": "synnodb.results",
    "CorrectnessReport": "synnodb.results",
}


def __getattr__(name: str):
    """Lazily resolve factory names without importing the LLM stack at import time."""
    module = _LAZY_FACTORY.get(name)
    if module is not None:
        import importlib

        return getattr(importlib.import_module(module), name)
    raise AttributeError(f"module 'synnodb' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_FACTORY))


# Faithful drop-in: ``synnodb.__version__`` mirrors DuckDB's (libs gate on it).
# SynnoDB's own package version is available as ``__synnodb_version__`` / pip metadata.
__version__ = _compat.__duckdb_version__
try:  # pragma: no cover - metadata only present when installed
    from importlib.metadata import version as _pkg_version

    __synnodb_version__ = _pkg_version("synnodb")
except Exception:  # pragma: no cover
    __synnodb_version__ = "0+unknown"


# `from synnodb import *` exposes only the *light* drop-in surface (so it works
# without the factory extra). Factory names stay accessible via explicit import.
__all__ = [*_compat.__all__, "RouterMode", "enable_debug_logging", "optimize_database"]
