"""A workload described as data.

"What is a workload" used to be an `OLAPWorkload` enum that ~9 methods switched on
(`if benchmark == TPCH / elif CEB / else raise`), so adding a workload meant editing all
of them. A `WorkloadSpec` carries the per-workload values instead, and the provider reads
from the spec; a new workload is a value passed to `register_workload(...)`.

The heavy / context-dependent parts (SQL dict, schema DDL, per-query parameter
generation) are supplied as factories, so importing a spec does not pull in the generator
modules or require SYNNO_DATA_DIR until they are actually used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.utils import ServeFrom

if TYPE_CHECKING:  # avoid an import cycle; only used for type hints
    from synnodb.workloads.query_params import ParamSpace
    from synnodb.workloads.system_factory import System
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

# Expands a query-range short name whose endpoints are NOT exact catalog ids
# (start_id, end_id, ordered_catalog) -> the list of ids in the range.
QueryRangeExpander = Callable[[str, str, "list[str]"], "list[str]"]

# A query generator: query_name like "Q1" + an RNG -> (name, sql, placeholders).
QueryGenFn = Callable[..., tuple[str, str, dict]]
# Built lazily from the provider (CEB needs its query dir + cache, etc.).
QueryGenFactory = Callable[["OLAPWorkloadProvider | None"], QueryGenFn]
# Built from the provider + a do_not_cache flag (CEB caches placeholders on disk).
PlaceholdersFactory = Callable[["OLAPWorkloadProvider", bool], Callable[..., dict]]
# Built lazily from the provider: query_name -> the typed ParamSpace (or None if the query is
# static / the workload exposes no spec). Drives live-UI input widgets from declared types.
ParamSpaceFactory = Callable[
    ["OLAPWorkloadProvider | None"], Callable[..., "ParamSpace | None"]
]


@dataclass(frozen=True)
class WorkloadSpec:
    """Everything the framework needs to drive an arbitrary OLAP workload."""

    name: str
    # dataset
    tables: tuple[str, ...]
    dataset_name: str  # parquet dir name (e.g. tpch->"tpch", ceb->"imdb")
    # query catalog
    all_query_ids: tuple[str, ...]
    # scale-factor profile per run mode (BENCHMARK uses benchmark_sf)
    benchmark_sf: float
    fast_check_sfs: tuple[float, ...]
    exhaustive_sfs: tuple[float, ...]
    ingest_sfs: tuple[float, ...]
    # planner-prompt parameterization (kept out of the prompt templates)
    example_query: str
    example_query_params: str
    schema_example_table: str  # a real table name, shown in the schema-read example
    # lazy / context-dependent providers
    sql_dict_factory: Callable[[], dict[str, str]]
    schema_factory: Callable[[], str]
    query_gen_factory: QueryGenFactory
    placeholders_factory: PlaceholdersFactory
    # Per-query typed value-space accessor (live-UI widget metadata + run-time sampling). None
    # for workloads that don't expose declarative specs (e.g. CEB, whose params come from disk).
    param_space_factory: ParamSpaceFactory | None = None
    # Absolute parquet location for bring-your-own workloads (holds sf<sf>/<table>.parquet).
    # None for built-ins, which derive the path from the data-dir + benchmark-name
    # convention. When set, the pipeline uses this directly.
    base_parquet_dir: Path | None = None
    # Cache-busting version for this workload's dataset. Participates in the LLM/snapshot
    # cache key so regenerating a dataset (or changing its scale-up code / arg syntax)
    # invalidates stale cache entries. None means "unversioned".
    dataset_version: str | None = None
    # Where this workload's queries read their tiers from. ``ServeFrom.DUCKDB`` -> each tier
    # directory holds a ``tier.duckdb`` (produced by the referential downscaler); in-memory runs
    # then serve the candidate engine over the shm plane and the DuckDB oracle from that database,
    # so no parquet touches disk (in-memory only). ``ServeFrom.PARQUET`` -> the classic
    # ``<table>.parquet`` tier layout.
    serve_from: ServeFrom = ServeFrom.PARQUET
    # Scale factor at which the multi-threading stage runs its large-scale correctness /
    # performance check. None means the framework picks a sensible default.
    large_check_sf: float | None = None
    # Reference oracle systems used to produce ground-truth/baseline results. None means
    # the framework default (DuckDB ground-truth only). A workload can request additional
    # references, e.g. (System.DUCKDB, System.UMBRA).
    reference_systems: "tuple[System, ...] | None" = None
    # Parameter instantiations generated per query for the INGEST sweep (the correctness
    # sweep and BENCHMARK mode carry their own provider-configured counts).
    ingest_instantiations: int = 3
    # Optional expander for query-range short names whose endpoints are not exact catalog
    # ids (e.g. CEB's "2-9" -> 2a..9b). None => only exact-catalog slicing is supported.
    query_range_expander: QueryRangeExpander | None = None

    def sql_dict(self) -> dict[str, str]:
        return self.sql_dict_factory()

    def schema(self) -> str:
        return self.schema_factory()

    def parquet_root(self) -> Path:
        """Absolute parquet root holding one directory per tier (``ratio<f>/<table>.parquet``
        for sampling-ratio tiers, or the legacy ``sf<N>/<table>.parquet``). Bring-your-own
        workloads carry it on the spec; built-ins derive it from the data-dir +
        workload-name convention (so this requires SYNNO_DATA_DIR to be configured)."""
        if self.base_parquet_dir is not None:
            return Path(self.base_parquet_dir)
        from synnodb import settings

        return (
            settings.get_data_dir()
            / "workloads"
            / self.name
            / f"{self.dataset_name}_parquet"
        )

    def scale_factors_for(self, run_mode: RunToolMode) -> list[float]:
        if run_mode == RunToolMode.FAST_CHECK:
            return list(self.fast_check_sfs)
        if run_mode == RunToolMode.EXHAUSTIVE:
            return list(self.exhaustive_sfs)
        if run_mode == RunToolMode.INGEST:
            return list(self.ingest_sfs)
        if run_mode == RunToolMode.BENCHMARK:
            return [self.benchmark_sf]
        raise ValueError(f"Unknown run mode: {run_mode}")


def _tier_value_spellings(value: float) -> list[str]:
    """The numeric part of a tier directory name, tolerant of int/float formatting
    (``1`` vs ``1.0``); the integer spelling is tried first so ``5`` -> ``5`` not ``5.0``."""
    spellings: list[str] = []
    try:
        if float(value).is_integer():
            spellings.append(str(int(value)))
    except (TypeError, ValueError):
        pass
    spellings.append(str(value))
    return spellings


def tier_dirname(value: float) -> str:
    """The canonical tier directory name for a sampling ratio: ``ratio<f>`` (e.g. ``ratio0.02``
    for a fraction, ``ratio1`` for the full benchmark tier). This is the name new tiers are
    *created* under; the integer spelling is preferred so it matches :func:`find_sf_dir`'s
    first-tried candidate when *reading* (which also resolves the legacy ``sf<N>`` spelling)."""
    return f"ratio{_tier_value_spellings(value)[0]}"


def find_sf_dir(base_parquet_dir: Path | str, scale_factor: float) -> Path | None:
    """The tier directory for a tier value under a parquet root.

    Resolves the sampling-ratio convention (``ratio<f>``, written by the referential
    downscaler) as well as the legacy scale-factor convention (``sf<N>``), tolerant of
    int/float name formatting (``ratio1`` vs ``ratio1.0``, ``sf1`` vs ``sf1.0``). A given root
    only ever holds one convention, so this is unambiguous. None if no spelling exists."""
    base = Path(base_parquet_dir)
    for prefix in ("ratio", "sf"):
        for spelling in _tier_value_spellings(scale_factor):
            candidate = base / f"{prefix}{spelling}"
            if candidate.exists():
                return candidate
    return None


_REGISTRY: dict[str, WorkloadSpec] = {}
_BUILTINS_LOADED = False


def _ensure_builtins() -> None:
    """Register the built-in (TPC-H, CEB) specs on first registry use.

    They are registered as an import side-effect of `workload_provider_olap`; importing
    it here (lazily, function-level) means the registry is correctly populated even when
    a caller imported only `workload_spec`. Guarded so it runs at most once and cannot
    recurse during that module's own import.
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    import synnodb.workloads.workload_provider_olap  # noqa: F401  (registers builtins)


def register_workload(spec: WorkloadSpec) -> None:
    """Register a workload so it can be driven by name. Idempotent on identical specs."""
    _REGISTRY[spec.name] = spec


def get_workload_spec(name: str) -> WorkloadSpec:
    _ensure_builtins()
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown workload '{name}'. Registered workloads: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def is_registered(name: str) -> bool:
    _ensure_builtins()
    return name in _REGISTRY


def registered_workloads() -> list[str]:
    _ensure_builtins()
    return sorted(_REGISTRY)


def resolve_workload(name: str):
    """Resolve a workload name to a Workload identity.

    Built-in names resolve to their `OLAPWorkload` enum member (preserving existing
    cache-key identity); any other registered workload resolves to a `WorkloadId`.
    Raises for unknown names. This is the single primitive a CLI/entry point should use
    instead of `OLAPWorkload(name)`, so registered bring-your-own workloads are accepted
    without an enum member.
    """
    _ensure_builtins()
    from synnodb.workloads.workload_provider import WorkloadId
    from synnodb.workloads.workload_provider_olap import OLAPWorkload

    try:
        return OLAPWorkload(name)
    except ValueError:
        pass
    if name in _REGISTRY:
        return WorkloadId(name)
    raise ValueError(
        f"Unknown workload '{name}'. Registered workloads: {sorted(_REGISTRY)}"
    )
