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

from collections.abc import Sequence
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


# The single file that holds a DuckDB-native subset inside its ``fraction<f>/`` directory.
SUBSET_DUCKDB_FILENAME = "subset.duckdb"


@dataclass(frozen=True)
class DuckDBSubsetSource:
    """Everything :meth:`OLAPWorkloadProvider.prepare` needs to lazily downscale a DuckDB-sourced
    workload's fractional subsets from the frozen source, on demand at run start. Carried on the
    spec as plain data (no downscaler import here); ``None`` for built-ins and plain BYO-parquet.

    ``frozen_source_path`` is the immutable image every subset derives from - the
    ``.source_snapshot.duckdb`` taken from a live connection, or the caller's read-only ``.duckdb``
    file for a static source. ``sql_by_id`` is the raw workload SQL (its JOINs are the primary
    signal for the FK-preserving join graph)."""

    frozen_source_path: str
    sql_by_id: dict[str, str]
    join_relationships: list | None
    whole_table_threshold: int


@dataclass(frozen=True)
class WorkloadSpec:
    """Everything the framework needs to drive an arbitrary OLAP workload."""

    name: str
    # dataset
    tables: tuple[str, ...]
    dataset_name: str  # parquet dir name (e.g. tpch->"tpch", ceb->"imdb")
    # query catalog
    all_query_ids: tuple[str, ...]
    # Subset-selector profile per run mode (BENCHMARK uses benchmark_sf). The historical name
    # ``sf`` is kept, but the value is a *polymorphic subset selector*, not always a TPC-H scale
    # factor: it is the number that picks which subset directory to use under
    # :meth:`parquet_root` (via :func:`find_sf_dir`). Its meaning depends on how the workload was
    # produced:
    #   * Built-in generated workloads (e.g. ``TPCH_SPEC``): a genuine TPC-H scale factor, >= 1
    #     (``benchmark_sf=20``, ``fast_check_sfs=(1, 2)``), materialized on disk as ``sf<N>/`` by
    #     the dbgen path.
    #   * DuckDB-sourced / bring-your-own workloads: a sampling *fraction* in ``(0, 1]`` of the
    #     frozen source (``fraction1`` = the full snapshot, ``fraction0.02`` = a 2% downscale),
    #     materialized as ``fraction<f>/`` by the referential downscaler.
    # So a smaller value is always the cheaper/smaller subset, but the numeric scale differs by
    # path. Downstream code treats it opaquely (feeds it to ``find_sf_dir``); only the built-in
    # generation path and dbgen interpret it as a true scale factor.
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
    # Where this workload's queries read their subsets from. ``ServeFrom.DUCKDB`` -> each subset
    # directory holds a ``subset.duckdb`` (produced by the referential downscaler); in-memory runs
    # then serve the candidate engine over the shm plane and the DuckDB oracle from that database,
    # so no parquet touches disk (in-memory only). ``ServeFrom.PARQUET`` -> the classic
    # ``<table>.parquet`` subset layout.
    serve_from: ServeFrom = ServeFrom.PARQUET
    # Inputs for lazily downscaling this workload's fractional subsets in
    # ``OLAPWorkloadProvider.prepare`` (from the frozen source, at run start). None for built-ins
    # and plain BYO-parquet, whose subsets are already materialized on disk.
    duckdb_source: "DuckDBSubsetSource | None" = None
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
        """Absolute parquet root holding one directory per subset (``fraction<f>/<table>.parquet``
        for sampling-fraction subsets, or the legacy ``sf<N>/<table>.parquet``). Bring-your-own
        workloads carry it on the spec; built-ins derive it from the data-dir +
        workload-name convention (so this requires SYNNO_DATA_DIR to be configured)."""
        if self.base_parquet_dir is not None:
            return Path(self.base_parquet_dir)
        return managed_parquet_root(self.name, self.dataset_name)

    def subset_files(self, subset_dir: Path) -> list[Path]:
        """The files that physically hold one subset under ``subset_dir``, per :attr:`serve_from`:
        a single ``subset.duckdb`` for a DuckDB-native workload, or one ``<table>.parquet`` per
        table for the parquet layout. The single place that knows a subset's on-disk shape."""
        if self.serve_from == ServeFrom.DUCKDB:
            return [subset_dir / SUBSET_DUCKDB_FILENAME]
        return [subset_dir / f"{table}.parquet" for table in self.tables]

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


def _subset_value_spellings(value: float) -> list[str]:
    """The numeric part of a subset directory name, tolerant of int/float formatting
    (``1`` vs ``1.0``); the integer spelling is tried first so ``5`` -> ``5`` not ``5.0``."""
    spellings: list[str] = []
    try:
        if float(value).is_integer():
            spellings.append(str(int(value)))
    except (TypeError, ValueError):
        pass
    spellings.append(str(value))
    return spellings


def subset_dirname(value: float) -> str:
    """The canonical subset directory name for a sampling fraction: ``fraction<f>`` (e.g.
    ``fraction0.02`` for a downscaled subset, ``fraction1`` for the full benchmark subset). This is
    the name new subsets are *created* under; the integer spelling is preferred so it matches
    :func:`find_sf_dir`'s first-tried candidate when *reading* (which also resolves the legacy
    ``sf<N>`` spelling)."""
    return f"fraction{_subset_value_spellings(value)[0]}"


def find_sf_dir(base_parquet_dir: Path | str, scale_factor: float) -> Path | None:
    """The subset directory for a subset value under a parquet root.

    Resolves the sampling-fraction convention (``fraction<f>``, written by the referential
    downscaler) as well as the legacy scale-factor convention (``sf<N>``), tolerant of
    int/float name formatting (``fraction1`` vs ``fraction1.0``, ``sf1`` vs ``sf1.0``). A given
    root only ever holds one convention, so this is unambiguous. None if no spelling exists."""
    base = Path(base_parquet_dir)
    for prefix in ("fraction", "sf"):
        for spelling in _subset_value_spellings(scale_factor):
            candidate = base / f"{prefix}{spelling}"
            if candidate.exists():
                return candidate
    return None


def discover_subset_values(base_parquet_dir: Path | str) -> list[float]:
    """Subset values that have a directory on disk under a parquet root, ascending.

    Reads whichever naming convention the root uses - ``fraction<f>`` (sampling fraction, written
    by the referential downscaler) or the legacy ``sf<N>`` - and normalizes integral values to
    ints so they format back to ``fraction1``/``sf50`` rather than ``fraction1.0``/``sf50.0``.
    Directory names only: use :func:`available_subsets` when the subset must also be complete."""
    values: list[float] = []
    base = Path(base_parquet_dir)
    if not base.is_dir():
        return values
    for prefix in ("fraction", "sf"):
        for child in base.glob(f"{prefix}*"):
            if not child.is_dir():
                continue
            try:
                value = float(child.name[len(prefix) :])
            except ValueError:
                continue
            values.append(int(value) if value.is_integer() else value)
    return sorted(set(values))


def available_subsets(spec: WorkloadSpec, base_parquet_dir: Path | str) -> list[float]:
    """Subset values fully materialized on disk under a parquet root, ascending.

    A subset counts as available only when every file it physically needs is present (per
    :meth:`WorkloadSpec.subset_files`: a single ``subset.duckdb`` for a DuckDB-native workload,
    one ``<table>.parquet`` per table otherwise), so a half-written or aborted subset is skipped.
    Driven by the filesystem rather than the spec's SF ladders, so a subset generated out-of-band
    (e.g. an extra dbgen scale factor) is offered too."""
    base = Path(base_parquet_dir)
    complete: list[float] = []
    for value in discover_subset_values(base):
        subset_dir = find_sf_dir(base, value)
        if subset_dir is None:
            continue
        if all(path.exists() for path in spec.subset_files(subset_dir)):
            complete.append(value)
    return complete


def format_subset_menu(
    available: Sequence[float], benchmark_sf: float, default_sf: float
) -> str:
    """The agent-facing menu of inspectable data subsets, shared by the ``query_data`` tool
    description and the planner/storage-plan prompts so the two can never drift.

    Names every subset the agent may query, marks the default (smallest) and the benchmark-scale
    one, and states the prefer-smallest rule.

    Crucially it separates what a *sample* preserves from what it does not. Measured against the
    real downscaler, a 5% subset reproduced low-cardinality domains and whole-kept dimension tables
    exactly, but understated a bounded column's max (141 vs 148.5), a key's max, and every
    medium/high-cardinality distinct count (27 vs 100; 11.6k vs 200k). Distribution shape, skew,
    null density, clustering and correlations transfer; **row counts, min/max and distinct counts
    do not**, and those three are exactly the numbers a physical design is sized from - a type
    width chosen from a sample's max overflows at full scale, and a dictionary sized from a
    sample's distinct count is orders of magnitude too small.

    It also tells the agent **not to extrapolate row counts by a ratio**. A subset is not a
    uniform shrink of the benchmark data, on either path:

    * A downscaled subset (``fraction<f>``) samples only the *anchor* (largest) table at ``f``.
      Tables at or below the small-row threshold, and tables off the join graph, are kept **whole**
      - the same rows in every subset - and every other table's size is emergent from join
      propagation, at whatever size keeps the joins non-vacuous (see
      :mod:`...dataset.custom_scaler.duckdb_downscale`).
    * A generated scale factor (``sf<N>``) is not uniform either: a generator typically scales its
      fact tables with the scale factor while holding small reference tables at a fixed size.

    So a per-table count from a small subset times the subset ratio is simply wrong. An earlier
    version of this menu said "multiply by the ratio", and a real run shows the damage: the agent
    scaled a whole-kept dimension up by 50x, could not reconcile the result with the subset it had
    asked for, and concluded the subset labels must be inverted. Shape carries over; counts must be
    measured where they matter. The values are also described as *selectors* rather than scale
    factors, so an agent does not read standard benchmark row counts into a subset's name.

    Neither the default nor the benchmark subset is guaranteed to be materialized (a built-in
    workload's ``sf<N>`` dirs come from an out-of-band generation step), so the menu only promises
    what is actually on disk: it advertises omitting ``sf`` only when the default exists, and
    points at the benchmark subset for real counts only when that subset exists."""
    if not available:
        return ""
    assert list(available) == sorted(available), (
        "available subsets must be ascending; the menu labels the first as the smallest"
    )
    assert benchmark_sf > 0 and default_sf > 0, (
        f"subset values are scale factors / sampling fractions, always > 0 "
        f"(benchmark_sf={benchmark_sf}, default_sf={default_sf})"
    )

    def _fmt(value: float) -> str:
        return _subset_value_spellings(value)[0]

    default_available = default_sf in available
    benchmark_available = benchmark_sf in available

    entries: list[str] = []
    for value in available:
        labels = []
        if value == default_sf:
            labels.append("default")
        if value == available[0]:
            labels.append("smallest")
        if value == benchmark_sf:
            labels.append("benchmark scale - what your design must actually serve")
        suffix = f" ({', '.join(labels)})" if labels else ""
        entries.append(f"`{_fmt(value)}`{suffix}")

    pick = (
        "Pass `sf` to choose one; omit it for the default."
        if default_available
        else "Pass `sf` to choose one."
    )
    # Where a trustworthy number comes from: the benchmark subset when it is on disk, otherwise
    # the largest subset there is, named as the approximation it is.
    if benchmark_available:
        verify_on = f"the benchmark subset `{_fmt(benchmark_sf)}`"
    else:
        verify_on = f"the largest available subset (`{_fmt(available[-1])}`)"

    menu = (
        "Data subsets for query_data: "
        + ", ".join(entries)
        + f". {pick} These are subset selectors, not standard scale factors - do not infer row "
        "counts from a subset's name.\n\n"
        "Default to the smallest subset that can answer the question, and only reach for a bigger "
        "one when you actually need the numbers below - a scan or join at benchmark scale is far "
        "more expensive and can hit the query time budget. Shape transfers from a small subset: "
        "distributions, skew, null density, clustering, correlations.\n\n"
        "But a subset is a *sample*, so row counts, min/max and distinct counts do NOT transfer. A "
        "sample's min/max lie inside the true range (rare extremes are missing) and its distinct "
        "counts are understated, so a type width, encoding or allocation sized from them overflows "
        "or undersizes at full scale. Row counts also shrink unevenly per table - small dimension "
        "tables are often kept whole, so an unchanged count between subsets is expected; never "
        "scale a count by a ratio. Measure any number you bake into the design on "
        f"{verify_on}: `count(*)`, `min`/`max` and `approx_count_distinct` are single-pass and "
        "cheap even at full scale."
    )
    return menu


def managed_parquet_root(name: str, dataset_name: str) -> Path:
    """The managed parquet root for a workload:
    ``<data-dir>/workloads/<name>/<dataset>_parquet``. The single source of this convention,
    shared by registration and :meth:`WorkloadSpec.parquet_root` (requires SYNNO_DATA_DIR)."""
    from synnodb import settings

    return settings.get_data_dir() / "workloads" / name / f"{dataset_name}_parquet"


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
