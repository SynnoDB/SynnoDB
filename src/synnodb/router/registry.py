"""The template registry: the router's *only* source of routable capability.

A query is bespoke-served only if its normalized structure is present here, mapped
to a healthy, non-quarantined ``EngineBinding`` built from an engine manifest. This
is the primary control surface for "what the router is allowed to do": nothing
outside the registry is ever routed.

The registry also tracks table *dirtiness* (a write/register/read_* touched a bound
table → its templates fall back until re-ingest) and *quarantine* (a failed engine
or a cross-check mismatch sidelines a template).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ColumnSpec:
    """One output column, named and typed to the canonical DuckDB schema."""

    name: str
    type: str  # DuckDB type string, e.g. "BIGINT", "DECIMAL(15,2)", "VARCHAR"


@dataclass(frozen=True)
class PlaceholderSpec:
    """One typed query input (mirrors the engine's per-query ``Q<id>Args``)."""

    name: str
    type: str


@dataclass(frozen=True)
class EngineBinding:
    """A registered template bound to a bespoke engine.

    Built from an engine ``manifest.json`` at registration. ``engine`` is the live
    worker handle (attached in Phase 2); ``None`` means "registered but not yet
    runnable", which the guards treat as a fallback cause.
    """

    template_id: str                         # stable id (engine_id + query_id)
    normalized_sql: str                      # the structural match key
    query_id: str                            # engine-local query id, e.g. "1"
    engine_id: str                           # content-addressed engine identity
    placeholders: Tuple[PlaceholderSpec, ...]
    output_schema: Tuple[ColumnSpec, ...]
    tables: FrozenSet[str]                   # source tables this query reads
    schema_fingerprint: str                  # fingerprint of the tables' schema at build
    scale_factor: Optional[float] = None
    storage_mode: str = "flat"               # "bespoke" | "flat"
    engine: Any = None                       # EngineWorker handle (Phase 2)
    template_sql: Optional[str] = None       # original parameterized template, for
    #                                          structural binding of an inline query's
    #                                          values to the engine's placeholders

    @property
    def runnable(self) -> bool:
        return self.engine is not None


class TemplateRegistry:
    """Thread-safe map of normalized SQL → ``EngineBinding`` plus dirty/quarantine state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_norm: Dict[str, EngineBinding] = {}
        self._dirty_tables: set[str] = set()
        self._quarantined: set[str] = set()

    # ---- population -----------------------------------------------------
    def register(self, binding: EngineBinding) -> None:
        with self._lock:
            self._by_norm[binding.normalized_sql] = binding

    def unregister(self, normalized_sql: str) -> None:
        with self._lock:
            self._by_norm.pop(normalized_sql, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_norm)

    def bindings(self) -> Tuple[EngineBinding, ...]:
        with self._lock:
            return tuple(self._by_norm.values())

    # ---- matching -------------------------------------------------------
    def match(self, normalized_sql: str) -> Optional[EngineBinding]:
        """Return a binding only if present and not quarantined."""
        with self._lock:
            binding = self._by_norm.get(normalized_sql)
            if binding is None or binding.template_id in self._quarantined:
                return None
            return binding

    def quarantined_binding(self, normalized_sql: str) -> Optional[EngineBinding]:
        """A binding for *normalized_sql* that exists but is quarantined (else None). For
        diagnostics: it lets ``why()`` explain that an engine is present but sidelined, rather
        than misreporting "no template match"."""
        with self._lock:
            binding = self._by_norm.get(normalized_sql)
            if binding is not None and binding.template_id in self._quarantined:
                return binding
            return None

    # ---- dirtiness (data immutability) ---------------------------------
    def mark_tables_dirty(self, tables: Iterable[str]) -> None:
        with self._lock:
            self._dirty_tables.update(t.lower() for t in tables)

    def clear_dirty(self, tables: Optional[Iterable[str]] = None) -> None:
        with self._lock:
            if tables is None:
                self._dirty_tables.clear()
            else:
                for t in tables:
                    self._dirty_tables.discard(t.lower())

    def is_dirty(self, binding: EngineBinding) -> bool:
        with self._lock:
            return any(t.lower() in self._dirty_tables for t in binding.tables)

    # ---- quarantine (resilience) ---------------------------------------
    def quarantine(self, template_id: str) -> None:
        with self._lock:
            self._quarantined.add(template_id)

    def is_quarantined(self, template_id: str) -> bool:
        with self._lock:
            return template_id in self._quarantined

    def reset_quarantine(self, template_id: Optional[str] = None) -> None:
        with self._lock:
            if template_id is None:
                self._quarantined.clear()
            else:
                self._quarantined.discard(template_id)

    def stats(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "templates": len(self._by_norm),
                "dirty_tables": sorted(self._dirty_tables),
                "quarantined": sorted(self._quarantined),
            }
