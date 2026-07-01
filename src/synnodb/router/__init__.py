"""SynnoDB query router — decides bespoke-engine vs DuckDB for each statement.

Light-weight by construction: importing this package pulls in no LLM/factory code,
and ``sqlglot`` is imported lazily inside the normalizer.
"""
from .adapt import results_equal, to_synno_result
from .backend import Backend, DuckDBBackend
from .engine import BespokeEngine, LocalCallableEngine
from .guards import GuardContext, evaluate
from .manifest import (
    EngineManifest,
    QueryTemplate,
    build_manifest_from_dir,
    check_compatibility,
    content_engine_id,
    infer_duckdb_type,
    register_manifest,
    write_manifest_for_engine,
)
from .observe import RouteTrace, enable_debug_logging, logger
from .policy import RouterMode, RouterPolicy
from .registration import make_binding, register_engine
from .process_engine import ProcessEngine, ShmHotLoadEngine
from .registry import ColumnSpec, EngineBinding, PlaceholderSpec, TemplateRegistry
from .router import QueryRouter, RouteDecision
from .shm_transport import SegmentRef, ShmWriter, read_table, sweep_orphans

__all__ = [
    "QueryRouter",
    "RouteDecision",
    "RouterPolicy",
    "RouterMode",
    "TemplateRegistry",
    "EngineBinding",
    "ColumnSpec",
    "PlaceholderSpec",
    "RouteTrace",
    "GuardContext",
    "evaluate",
    "logger",
    "enable_debug_logging",
    "BespokeEngine",
    "LocalCallableEngine",
    "Backend",
    "DuckDBBackend",
    "results_equal",
    "to_synno_result",
    "make_binding",
    "register_engine",
    "EngineManifest",
    "QueryTemplate",
    "register_manifest",
    "check_compatibility",
    "build_manifest_from_dir",
    "content_engine_id",
    "write_manifest_for_engine",
    "infer_duckdb_type",
    "ProcessEngine",
    "ShmHotLoadEngine",
    "SegmentRef",
    "ShmWriter",
    "read_table",
    "sweep_orphans",
]
