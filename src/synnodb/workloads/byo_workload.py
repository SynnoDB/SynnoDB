"""Register a workload from data: queries plus existing parquet, with no source edits.

Two input forms, both declaring their parameter value *spaces* as typed specs (see
:mod:`synnodb.workloads.query_params`):

  * a single ``queries.json`` mapping each query id to
    ``{"sql": ..., "params": {PH: <spec>}, "param_groups": [<group spec>, ...]}``
    (a plain SQL string is shorthand for a static query with no params);
  * a directory of ``*.sql`` files (filename stem = query id) plus an optional sidecar
    ``params.json`` of the form ``{id: {"params": {...}, "param_groups": [...]}}``.

The schema shown to the planner is derived from the parquet via DuckDB DESCRIBE; table names
are inferred from the parquet directory. A templated query's specs are parsed into a
:class:`~synnodb.workloads.query_params.ParamSpace` and sampled symbolically at run time (with
the run's seeded RNG), exactly as the built-in TPC-H generator draws its values; static
(parameterless) queries get an identity generator.
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from synnodb.utils.utils import ServeFrom
from synnodb.workloads.query_params import (
    ParamSpace,
    find_placeholders,
    parse_param_space,
    substitute,
)
from synnodb.workloads.workload_spec import (
    WorkloadSpec,
    register_workload,
    subset_dirname,
)

if TYPE_CHECKING:
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

logger = logging.getLogger(__name__)

# A query-id key, tolerant of common prefixes: "1", "q1", "Q1", "query1", "2b", "11b".
# The canonical bare id (the part from the first digit on) is what the framework uses
# everywhere (query{id}.cpp, sql_dict["Q{id}"]).
_QID_RE = re.compile(r"^\s*(?P<prefix>query|q)?\s*(?P<id>\d\w*?)\s*$", re.IGNORECASE)


def _normalize_query_keys(raw_keys: list[str]) -> dict[str, str]:
    """Map each user-supplied key to its bare query id.

    Accepts ``1`` / ``q1`` / ``Q1`` / ``query1`` / ``2b`` and strips the prefix. Logs the
    detected key style, and raises on a key it cannot parse or on two keys that map to the
    same id. Returns ``{original_key: bare_id}`` in input order.
    """
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}
    prefixes: set[str] = set()
    for key in raw_keys:
        m = _QID_RE.match(str(key))
        if not m:
            raise ValueError(
                f"Cannot parse a query id from key {key!r}. Expected forms like "
                f"'1', 'q1', 'Q1', 'query1', '2b'."
            )
        qid = m.group("id")
        if qid in seen:
            raise ValueError(
                f"Query-id collision: keys {seen[qid]!r} and {key!r} both normalize to "
                f"'{qid}'. Use one unique key per query."
            )
        seen[qid] = str(key)
        prefixes.add((m.group("prefix") or "").lower())
        mapping[str(key)] = qid

    styles = sorted(p or "<bare>" for p in prefixes)
    logger.info(
        "Query input: detected key style %s; using bare ids %s",
        styles,
        list(mapping.values()),
    )
    if len(prefixes) > 1:
        logger.warning(
            "Mixed query-id key styles %s in one input; normalized each independently, "
            "but prefer a single consistent style.",
            styles,
        )
    return mapping


def _preflight_workload(
    name: str,
    sql_dict: dict[str, str],
    ids: list[str],
    resolved_tables: list[str],
    spaces: dict[str, ParamSpace],
) -> None:
    """Registration-time self-check, plus a debug dump under SYNNODB_BYO_DEBUG=1.

    Samples one representative assignment per templated query and confirms every value is a
    string (or an IN-list) - the shape the args line + generated C++ parser require - and
    logs the SQL + args line the engine will receive.
    """
    from synnodb.workloads.workload_provider import format_args_element

    logger.debug("BYO workload %r: tables=%s queries=%s", name, resolved_tables, ids)
    for qid in ids:
        qn = f"Q{qid}"
        if qid not in spaces:
            logger.debug("  %s: static query (no placeholders)", qn)
            continue
        sample = spaces[qid].sample(random.Random(0))
        # Every placeholder value must be a string (or an IN-list). The args line quotes all
        # values and the generated parser reads std::quoted into a std::string field. Sampling
        # already coerces to strings; this guards the invariant defensively.
        for pname, pval in sample.items():
            is_in_list = isinstance(pval, str) and pval.startswith("(")
            if not is_in_list and not isinstance(pval, str):
                raise ValueError(
                    f"{qn} placeholder '{pname}'={pval!r} is {type(pval).__name__}; "
                    f"placeholder values must render to strings (the args line is quoted and "
                    f"the generated C++ parser reads std::quoted into a std::string field). "
                    f"Otherwise the engine fails with 'failed to parse {pname}'."
                )
        inst_sql = " ".join(substitute(sql_dict[qn], sample).split())
        args_line = format_args_element(qid, sample)
        logger.debug("  %s: sample params=%s", qn, sample)
        logger.debug("  %s: engine args line -> %s", qn, args_line)
        logger.debug("  %s: instantiated SQL -> %s", qn, inst_sql)


def _normalize_params(raw: dict, source: str) -> dict[str, dict]:
    """Normalize the keys of a params mapping (``{qid: {"params": {...}, "param_groups":
    [...]}}``) to the canonical bare query ids, reusing the same key parsing as the queries so
    ``19``/``q19``/``Q19`` all resolve."""
    if not isinstance(raw, dict):
        raise ValueError(
            f"{source} must be a JSON object mapping query-id -> {{placeholder: [values]}}."
        )
    out: dict[str, dict] = {}
    for key, val in raw.items():
        m = _QID_RE.match(str(key))
        if not m:
            raise ValueError(
                f"{source}: cannot parse a query id from key {key!r}. Expected forms like "
                f"'1', 'q1', 'Q1', 'query1', '2b'."
            )
        out[m.group("id")] = val
    return out


def _build_param_spaces(
    sql_by_id: dict[str, str],
    params_by_id: dict[str, dict],
    params_source: str = "params",
) -> dict[str, ParamSpace]:
    """Parse each templated query's typed specs into a :class:`ParamSpace`.

    Every query with ``[PLACEHOLDER]`` holes must have a matching parameter section (``params``
    and/or ``param_groups``); one that does not is an error (we do not invent values). Static
    queries are skipped (no entry).
    """
    templated = {qid: sql for qid, sql in sql_by_id.items() if find_placeholders(sql)}
    unknown = set(params_by_id) - set(templated)
    if unknown:
        logger.warning(
            "%s has entries for queries that are not templated/known and were ignored: %s",
            params_source,
            sorted(unknown),
        )
    missing = sorted(qid for qid in templated if qid not in params_by_id)
    if missing:
        raise ValueError(
            f"Templated queries {missing} have no parameter values. Provide them in "
            f'{params_source}, e.g. "{missing[0]}": {{"params": {{"<PLACEHOLDER>": '
            f'{{"type": "int", "min": 1, "max": 50}}}}}}.'
        )
    out: dict[str, ParamSpace] = {}
    for qid, tmpl in templated.items():
        section = params_by_id[qid] or {}
        try:
            out[qid] = parse_param_space(
                section.get("params"), section.get("param_groups"), tmpl
            )
        except ValueError as e:
            raise ValueError(f"Q{qid}: {e}") from e
    return out


def _natural_sort(ids: list[str]) -> list[str]:
    """Order query ids numerically when all-digit, else lexically, so the catalog order
    does not depend on filesystem listing order."""
    if all(q.isdigit() for q in ids):
        return sorted(ids, key=int)
    return sorted(ids)


def _sf_dir(parquet_dir: Path, sf: float) -> Path:
    """The existing subset directory for a subset value under a parquet root - the
    sampling-fraction ``fraction<f>`` convention (written by the referential downscaler) or the
    legacy ``sf<N>`` one. Falls back to the ``sf<sf>`` spelling for error messages when nothing
    exists yet."""
    from synnodb.workloads.workload_spec import find_sf_dir

    resolved = find_sf_dir(parquet_dir, sf)
    return resolved if resolved is not None else parquet_dir / f"sf{sf}"


# Number of the smallest available scale factors to use as fast validation rungs when only a
# target SF is supplied. Two cheap rungs (e.g. sf1, sf2) catch the vast majority of bugs in
# seconds while still surfacing scale-sensitive ones before the expensive target-SF run.
_FAST_RUNG_COUNT = 2


def _discover_available_sfs(parquet_dir: Path) -> list[float]:
    """Subset values that actually have data on disk, ascending.

    Convention is ``<parquet_dir>/<subset>/<table>.parquet`` where ``<subset>`` is ``fraction<f>``
    (sampling fraction) or the legacy ``sf<N>``; integral values are returned as ints so they
    format back to ``fraction1``/``sf50`` rather than ``fraction1.0``/``sf50.0``."""
    sfs: list[float] = []
    if not parquet_dir.is_dir():
        return sfs
    for prefix in ("fraction", "sf"):
        for child in parquet_dir.glob(f"{prefix}*"):
            if not child.is_dir():
                continue
            try:
                value = float(child.name[len(prefix) :])
            except ValueError:
                continue
            sfs.append(int(value) if value.is_integer() else value)
    return sorted(set(sfs))


def _derive_sf_ladder(
    scale_factors: tuple[float, ...], parquet_dir: Path
) -> tuple[tuple[float, ...], tuple[float, ...], float, tuple[float, ...]]:
    """Derive the validation scale-factor ladder: always small-first, target last.

    The target (the SF the user actually wants the engine for) is the last element of
    ``scale_factors``. Correctness is validated cheapest-first so a bug is caught in seconds
    at a small SF rather than after a multi-minute load at the target - the exact failure
    that cost the SF50 Q10 run hours. When the caller passes an explicit multi-SF ladder we
    honour it; when only the target is given we augment it with the smallest scale factors
    that exist on disk. Returns ``(fast_check_sfs, exhaustive_sfs, benchmark_sf, ingest_sfs)``.

    Note: for a workload sourced from a DuckDB connection this scanning is moot -
    :func:`register_workload_from_duckdb` derives the fast rungs itself by FK-preserving
    downscaling and hands us an explicit ``(fraction, …, 1.0)`` ladder, so the branch below that
    honours an explicit multi-subset ladder is taken and no ``sf*`` scan happens. This function
    still scans for the bring-your-own **parquet** entries, which supply their own subsets on disk;
    those keep TPC-H-shaped inputs fast and fail loudly (the warning below) otherwise.
    """
    target = scale_factors[-1]

    if len(scale_factors) > 1:
        # Caller gave an explicit ladder (e.g. (1, 2, 50)); honour it verbatim.
        rungs = sorted(set(scale_factors))
    else:
        available_small = [
            sf for sf in _discover_available_sfs(parquet_dir) if sf < target
        ]
        rungs = sorted(set(available_small[:_FAST_RUNG_COUNT] + [target]))

    fast_check = tuple(sf for sf in rungs if sf < target)
    if not fast_check:
        logger.warning(
            "No scale factor smaller than the target SF=%s is available under %s, so every "
            "validation iteration must load the full target-size dataset (slow, and the "
            "reason a one-line bug can burn hours). Provide small SFs (e.g. sf1/, sf2/) or "
            "pass scale_factors=(1, 2, %s) so correctness is validated cheaply first.",
            target,
            parquet_dir,
            target,
        )
        fast_check = (target,)

    return fast_check, tuple(rungs), target, (target,)


def infer_tables_from_parquet(parquet_dir: str | Path, sf: float) -> list[str]:
    base = _sf_dir(Path(parquet_dir), sf)
    if not base.is_dir():
        raise FileNotFoundError(
            f"Cannot infer tables: parquet directory '{base}' does not exist."
        )
    tables = sorted(p.stem for p in base.glob("*.parquet"))
    if not tables:
        raise FileNotFoundError(f"No .parquet files found in '{base}'.")
    return tables


def _ddl_from_describe(
    con: Any, tables: list[str], from_sql: Callable[[str], str]
) -> str:
    """Build ``CREATE TABLE`` DDL by DESCRIBEing ``SELECT * FROM <from_sql(table)>`` for each
    table, so the schema is derived from the data itself with nothing to hand-maintain."""
    parts: list[str] = []
    for t in tables:
        rows = con.execute(f"DESCRIBE SELECT * FROM {from_sql(t)}").fetchall()
        cols = ",\n    ".join(f"{r[0]} {r[1]}" for r in rows)
        parts.append(f"CREATE TABLE {t} (\n    {cols}\n);")
    return "\n\n".join(parts)


def schema_ddl_from_parquet(
    parquet_dir: str | Path, tables: list[str], sf: float
) -> str:
    """Derive a CREATE TABLE DDL string for the planner from the parquet files
    themselves, so there is no hand-written schema to keep in sync."""
    import duckdb

    base = _sf_dir(Path(parquet_dir), sf)
    con = duckdb.connect()
    try:
        return _ddl_from_describe(
            con,
            tables,
            lambda t: f"read_parquet('{(base / f'{t}.parquet').as_posix()}')",
        )
    finally:
        con.close()


def schema_ddl_from_duckdb(subset_db_path: str | Path, tables: list[str]) -> str:
    """Derive a CREATE TABLE DDL string for the planner from a DuckDB-native ``subset.duckdb``,
    the DuckDB-native analogue of :func:`schema_ddl_from_parquet` (no parquet to describe)."""
    import duckdb

    con = duckdb.connect(str(subset_db_path), read_only=True)
    try:
        return _ddl_from_describe(con, tables, lambda t: f'"{t}"')
    finally:
        con.close()


def register_workload_from_dir(
    name: str,
    sql_dir: str | Path,
    parquet_dir: str | Path,
    *,
    tables: list[str] | None = None,
    dataset_name: str | None = None,
    scale_factors: tuple[float, ...] = (1,),
    schema_example_table: str | None = None,
) -> WorkloadSpec:
    """Build + register a WorkloadSpec from a SQL directory and existing parquet.

    Args:
        name: workload id (used as the benchmark name / WorkloadId).
        sql_dir: directory of `*.sql` files; each file's stem is a query id, its text
            the SQL (static, or a `[PLACEHOLDER]` template).
        parquet_dir: directory holding `sf<sf>/<table>.parquet`; used to infer tables
            and derive the schema.
        tables: explicit table list; inferred from parquet when omitted.
        dataset_name: parquet dir name (defaults to `name`).
        scale_factors: the scale factors that exist on disk (defaults to (1,)).
        schema_example_table: table shown in the planner's schema-read example.

    Templated queries take their value spaces from a sidecar ``<sql_dir>/params.json`` of the
    form ``{id: {"params": {PLACEHOLDER: <spec>}, "param_groups": [<group spec>, ...]}}``.
    """
    sql_dir = Path(sql_dir)
    sql_files = sorted(sql_dir.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No .sql files found in '{sql_dir}'.")

    sql_by_id = {f.stem: f.read_text() for f in sql_files}

    params_by_id: dict[str, dict] = {}
    params_source = "params.json"
    params_path = sql_dir / "params.json"
    if params_path.is_file():
        params_by_id = _normalize_params(
            json.loads(params_path.read_text()), str(params_path)
        )
        params_source = str(params_path)

    return _register_static_workload(
        name=name,
        sql_by_id=sql_by_id,
        parquet_dir=parquet_dir,
        tables=tables,
        dataset_name=dataset_name,
        scale_factors=scale_factors,
        schema_example_table=schema_example_table,
        params_by_id=params_by_id,
        params_source=params_source,
    )


def _parse_queries_json(
    raw: object, source: str
) -> tuple[dict[str, str], dict[str, dict]]:
    """Split a ``queries.json`` mapping into ``{qid: sql}`` and ``{qid: {"params"/"param_groups"}}``.

    Each entry is either a plain SQL string (a static query) or an object with a ``"sql"`` key
    plus optional ``params`` / ``param_groups``. Query ids are kept as written here; the shared
    builder normalizes them (q1/Q1/query1/1) downstream.
    """
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{source} must be a non-empty JSON object mapping query-id -> "
            f'{{"sql": ..., "params": ...}} (or a plain SQL string).'
        )
    sql_by_id: dict[str, str] = {}
    params_by_id: dict[str, dict] = {}
    for qid, entry in raw.items():
        qid = str(qid)
        if isinstance(entry, str):
            sql_by_id[qid] = entry
            continue
        if not isinstance(entry, dict) or "sql" not in entry:
            raise ValueError(
                f"{source}: query {qid!r} must be a SQL string or an object with a "
                f'"sql" key, got {type(entry).__name__}.'
            )
        sql_by_id[qid] = str(entry["sql"])
        section: dict = {}
        if entry.get("params"):
            section["params"] = entry["params"]
        if entry.get("param_groups"):
            section["param_groups"] = entry["param_groups"]
        if section:
            params_by_id[qid] = section
    return sql_by_id, params_by_id


def register_workload_from_json(
    name: str,
    queries_json: str | Path,
    parquet_dir: str | Path,
    *,
    tables: list[str] | None = None,
    dataset_name: str | None = None,
    scale_factors: tuple[float, ...] = (1,),
    schema_example_table: str | None = None,
) -> WorkloadSpec:
    """Build + register a WorkloadSpec from a single self-describing ``queries.json``.

    Each entry is keyed by query id and is either:

      * an object ``{"sql": <str>, "params": {PLACEHOLDER: <spec>}, "param_groups": [...]}``
        - the typed specs declare each placeholder's value space and are sampled at run time;
        ``params``/``param_groups`` may be omitted for a static query; or
      * a plain SQL string - shorthand for a static query with no params (an error if it
        actually contains ``[PLACEHOLDER]`` holes).

    One self-contained input artifact: SQL and parameter value spaces live together, the shape
    a dashboard would populate.
    """
    queries_json = Path(queries_json)
    raw = json.loads(queries_json.read_text())
    sql_by_id, params_by_id = _parse_queries_json(raw, str(queries_json))

    return _register_static_workload(
        name=name,
        sql_by_id=sql_by_id,
        parquet_dir=parquet_dir,
        tables=tables,
        dataset_name=dataset_name,
        scale_factors=scale_factors,
        schema_example_table=schema_example_table,
        params_by_id=_normalize_params(params_by_id, str(queries_json)),
        params_source=str(queries_json),
    )


def _register_static_workload(
    name: str,
    sql_by_id: dict[str, str],
    parquet_dir: str | Path,
    *,
    tables: list[str] | None,
    dataset_name: str | None,
    scale_factors: tuple[float, ...],
    schema_example_table: str | None,
    params_by_id: dict[str, dict] | None = None,
    params_source: str = "params",
    dataset_version: str | None = None,
    schema_factory: "Callable[[], str] | None" = None,
    serve_from: ServeFrom = ServeFrom.PARQUET,
) -> WorkloadSpec:
    """Shared builder: turn an ``{id: sql}`` map + a subset root into a registered workload.
    Schema is derived from the parquet subset (or supplied via ``schema_factory`` for DuckDB-native
    subsets); tables inferred if not given. Templated queries are filled by sampling their typed
    :class:`ParamSpace` at run time; static queries get an identity generator."""
    parquet_dir = Path(parquet_dir)
    # Normalize keys (q1/Q1/query1/1/2b) to canonical bare ids, reliably + reported.
    key_to_id = _normalize_query_keys(list(sql_by_id))
    sql_by_norm = {key_to_id[k]: v for k, v in sql_by_id.items()}
    ids = _natural_sort(list(sql_by_norm))
    sql_dict = {f"Q{qid}": sql_by_norm[qid] for qid in ids}

    resolved_tables = tables or infer_tables_from_parquet(parquet_dir, scale_factors[0])

    if schema_factory is None:

        def schema_factory() -> str:
            return schema_ddl_from_parquet(
                parquet_dir, resolved_tables, scale_factors[0]
            )

    # Templated queries (those with [PLACEHOLDER] holes) carry a typed ParamSpace sampled at
    # run time; static queries use an identity generator. Params keys were already normalized
    # to bare ids (same _QID_RE) by the callers.
    spaces = _build_param_spaces(sql_by_norm, params_by_id or {}, params_source)
    # Validate a representative sample and, under SYNNODB_BYO_DEBUG, log the SQL + args
    # line per query.
    _preflight_workload(name, sql_dict, ids, resolved_tables, spaces)

    def _bare(query_name: str) -> str:
        return query_name[1:] if query_name.startswith("Q") else query_name

    def query_gen_factory(provider: "OLAPWorkloadProvider|None"):
        def gen(query_name: str, rnd=None):
            qid = _bare(query_name)
            template = sql_dict[query_name]
            if qid in spaces:
                r = rnd or random.Random(0)
                assign = spaces[qid].sample(r)
                return (query_name, substitute(template, assign), assign)
            return (query_name, template, {})

        return gen

    def placeholders_factory(
        provider: "OLAPWorkloadProvider", do_not_cache: bool = False
    ):
        def gen(query_name: str, **_):
            qid = _bare(query_name)
            # representative placeholder set for arg-parser / engine generation
            return spaces[qid].sample(random.Random(0)) if qid in spaces else {}

        return gen

    def param_space_factory(provider: "OLAPWorkloadProvider|None"):
        def get(query_name: str) -> ParamSpace | None:
            return spaces.get(_bare(query_name))

        return get

    # Always validate small-SF-first (target last) so a crash like the SF50 Q10 segfault
    # is caught in seconds at SF1 instead of after a six-minute load at the target SF.
    fast_check_sfs, exhaustive_sfs, benchmark_sf, ingest_sfs = _derive_sf_ladder(
        scale_factors, parquet_dir
    )

    spec = WorkloadSpec(
        name=name,
        tables=tuple(resolved_tables),
        dataset_name=dataset_name or name,
        all_query_ids=tuple(ids),
        benchmark_sf=benchmark_sf,
        fast_check_sfs=fast_check_sfs,
        exhaustive_sfs=exhaustive_sfs,
        ingest_sfs=ingest_sfs,
        example_query=f"Q{ids[0]}",
        example_query_params=ids[0],
        schema_example_table=schema_example_table or resolved_tables[0],
        sql_dict_factory=lambda: sql_dict,
        schema_factory=schema_factory,
        query_gen_factory=query_gen_factory,
        placeholders_factory=placeholders_factory,
        param_space_factory=param_space_factory,
        base_parquet_dir=parquet_dir,
        dataset_version=dataset_version,
        serve_from=serve_from,
    )
    register_workload(spec)
    return spec


# The referential downscaler's version. Bumped when its algorithm changes so a stale materialized
# subset (and any LLM/snapshot cache keyed on ``dataset_version``) is invalidated.
_DOWNSCALER_VERSION = "1"


def _duckdb_dataset_version(
    downscaler,
    subsets: tuple[float, ...],
    whole_table_threshold: int,
    serve_from: ServeFrom,
) -> str:
    """A cache-busting fingerprint of the *derived* dataset - everything that changes which rows
    a subset contains: source table names + row counts, the inferred join relationships (they
    drive which rows are kept), the subset fractions, the whole-table threshold, the subset
    storage format (duckdb vs parquet), the installed DuckDB version (its ``hash()`` defines the
    deterministic anchor sample and is not guaranteed stable across versions), and the downscaler
    version. Any change re-extracts and invalidates stale LLM/snapshot cache entries (§5.2)."""
    import hashlib

    import duckdb

    relationships = sorted(
        (r.table_a, r.table_b, r.pairs) for r in downscaler.relationships
    )
    payload = repr(
        {
            "tables": sorted(downscaler.schema.row_counts.items()),
            "relationships": relationships,
            "subsets": sorted(subsets),
            "whole_table_threshold": whole_table_threshold,
            "serve_from": serve_from.value,
            "duckdb": duckdb.__version__,
            "downscaler": _DOWNSCALER_VERSION,
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# Manifest written next to the materialized subsets, recording the ``dataset_version`` they were
# built from. Reused only when it still matches, so a changed source/params/algorithm rebuilds.
_SUBSET_MANIFEST = ".synno_dataset.json"


def _subset_artifacts(
    managed_root: Path, fraction: float, serve_from: ServeFrom, tables: list[str]
) -> list[Path]:
    """The files that must all exist for ``fraction``'s subset to count as materialized."""
    out_dir = managed_root / subset_dirname(fraction)
    if serve_from == ServeFrom.DUCKDB:
        return [out_dir / "subset.duckdb"]
    return [out_dir / f"{t}.parquet" for t in tables]


def _materialization_is_current(
    managed_root: Path,
    dataset_version: str,
    scale_factors: tuple[float, ...],
    serve_from: ServeFrom,
    tables: list[str],
) -> bool:
    """True iff a prior materialization under ``managed_root`` was built from ``dataset_version``
    and every subset artifact it should have is present - so it can be reused verbatim. A missing
    manifest, a mismatched version (source/params/algorithm changed), or any missing artifact (a
    partial or interrupted build) all return False so the caller rebuilds from scratch."""
    manifest = managed_root / _SUBSET_MANIFEST
    if not manifest.exists():
        return False
    try:
        recorded = json.loads(manifest.read_text()).get("dataset_version")
    except (OSError, ValueError):
        return False
    if recorded != dataset_version:
        return False
    return all(
        art.exists()
        for f in scale_factors
        for art in _subset_artifacts(managed_root, f, serve_from, tables)
    )


def _clear_managed_subsets(managed_root: Path) -> None:
    """Remove every ``fraction*``/``sf*`` subset directory under ``managed_root`` (and the
    manifest), so a rebuild never mixes stale rows from an earlier source/algorithm with fresh
    ones or leaves orphaned subset dirs for fractions no longer requested."""
    import shutil

    if managed_root.is_dir():
        for child in managed_root.iterdir():
            if child.is_dir() and (
                child.name.startswith("fraction") or child.name.startswith("sf")
            ):
                shutil.rmtree(child, ignore_errors=True)
    (managed_root / _SUBSET_MANIFEST).unlink(missing_ok=True)


def register_workload_from_duckdb(
    name: str,
    con,
    queries_json: "str | Path | dict",
    *,
    managed_root: str | Path,
    downscale_fractions: tuple[float, ...] = (0.02, 0.1),
    join_relationships: list | None = None,
    tables: list[str] | None = None,
    dataset_name: str | None = None,
    schema_example_table: str | None = None,
    whole_table_threshold: int = 10_000,
    serve_from: "ServeFrom | str" = ServeFrom.DUCKDB,
    source_db_path: str | Path | None = None,
    allow_benchmark_symlink: bool = False,
) -> WorkloadSpec:
    """Register a workload sourced from a live DuckDB connection, deriving the subsets by
    FK-preserving downscaling instead of taking pre-scaled parquet (the connection-sourced
    sibling of :func:`register_workload_from_json`).

    Two subset representations, selected by ``serve_from``:

    * ``ServeFrom.DUCKDB`` (the default): the referential downscaler
      (:mod:`synnodb.workloads.dataset.custom_scaler.duckdb_downscale`) materializes each
      downscaled subset as ``<managed_root>/fraction<f>/subset.duckdb``. The full ``fraction1``
      benchmark subset is an independent materialized copy by default; it is a zero-copy symlink
      to ``source_db_path`` only when ``allow_benchmark_symlink`` is set, which the caller may do
      only when no read-write connection to the source stays open (else the framework's later
      read-only opens of the symlinked file collide with it). No parquet touches disk: the
      candidate engine ingests the subset over the shm plane and the DuckDB oracle materializes
      flat tables from it.
    * ``ServeFrom.PARQUET``: the same downscaler writes
      ``<managed_root>/fraction<f>/<table>.parquet`` and the workload is registered exactly like a
      bring-your-own parquet workload, so the whole factory + oracle run against it unchanged.

    The caller's database is only read; nothing is written back.

    Args:
        name: workload id (used as the benchmark name / WorkloadId).
        con: a live ``duckdb.DuckDBPyConnection`` to source schema + data from.
        queries_json: a ``queries.json`` path, or an already-parsed ``{qid: entry}`` dict; its
            JOINs are the primary signal for the FK-preserving join graph.
        managed_root: directory the materialized ``fraction<f>/`` subsets are written under.
        downscale_fractions: sampling fractions of the anchor for the fast-check rungs.
        join_relationships: optional explicit ``(table_a.col, table_b.col)`` equi-join pairs,
            unioned into the inferred join graph for anything inference misses.
        tables / dataset_name / schema_example_table: as in the parquet entry points.
        whole_table_threshold: tables at or below this row count are kept whole in a subset.
        serve_from: an :class:`ServeFrom` (or its string value) selecting the subset
            representation - ``DUCKDB`` (``subset.duckdb``) vs. ``PARQUET`` (parquet files).
        source_db_path: the source ``.duckdb`` path, used only to symlink the DuckDB benchmark
            subset zero-copy; None (or ``allow_benchmark_symlink=False``) materializes a full
            independent copy instead.
        allow_benchmark_symlink: permit the zero-copy ``fraction1`` symlink to ``source_db_path``.
            Safe only when the caller holds no read-write connection to the source for the
            lifetime of the workload (e.g. it was opened from a path SynnoDB itself owns and
            closes). Defaults to False - an independent copy is always safe.
    """
    from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
        ReferentialDownscaler,
    )

    serve_from = ServeFrom.coerce(serve_from)

    if isinstance(queries_json, dict):
        raw: object = queries_json
        source = f"{name} queries"
    else:
        raw = json.loads(Path(queries_json).read_text())
        source = str(queries_json)
    sql_by_id, params_by_id = _parse_queries_json(raw, source)

    downscaler = ReferentialDownscaler(
        con,
        sql_by_id=sql_by_id,
        join_relationships=join_relationships,
        whole_table_threshold=whole_table_threshold,
    )

    # Subset ladder = the downscale fractions (fast-check rungs) plus the full ``1.0`` benchmark subset
    # last (the shared builder treats the last scale factor as the benchmark/target).
    subsets = tuple(sorted(set(downscale_fractions)))
    if any(not (0.0 < t < 1.0) for t in subsets):
        raise ValueError(
            f"downscale_fractions must be fractions in (0, 1); got {downscale_fractions}."
        )
    scale_factors = subsets + (1.0,)
    resolved_tables = tables or list(downscaler.schema.tables)
    managed_root = Path(managed_root)

    dataset_version = _duckdb_dataset_version(
        downscaler, subsets, whole_table_threshold, serve_from
    )

    # Reuse the on-disk subsets only when they were built from this exact dataset_version and are
    # all present; otherwise rebuild every subset from scratch (a changed source, params, join
    # graph, threshold, or DuckDB version, or a partial/interrupted prior run).
    if _materialization_is_current(
        managed_root, dataset_version, scale_factors, serve_from, resolved_tables
    ):
        logger.info(
            "Subsets under %s already current (dataset_version=%s); skipping materialization",
            managed_root,
            dataset_version,
        )
    else:
        _clear_managed_subsets(managed_root)
        managed_root.mkdir(parents=True, exist_ok=True)
        for fraction in scale_factors:
            out_dir = managed_root / subset_dirname(fraction)
            logger.info(
                "Creating %s",
                "benchmark subset (full data)"
                if fraction >= 1.0
                else f"downscale subset fraction={fraction:g}",
            )
            if serve_from == ServeFrom.DUCKDB:
                subset_db = out_dir / "subset.duckdb"
                if (
                    fraction >= 1.0
                    and allow_benchmark_symlink
                    and source_db_path is not None
                ):
                    # Zero-copy benchmark subset: safe only because the caller vouched no
                    # read-write connection to the source stays open (allow_benchmark_symlink).
                    out_dir.mkdir(parents=True, exist_ok=True)
                    subset_db.symlink_to(Path(source_db_path).resolve())
                else:
                    downscaler.copy_subset_to_duckdb(fraction, subset_db)
            else:
                downscaler.copy_subset_to_parquet(fraction, out_dir)
        (managed_root / _SUBSET_MANIFEST).write_text(
            json.dumps(
                {"dataset_version": dataset_version, "serve_from": serve_from.value}
            )
        )

    schema_factory: "Callable[[], str] | None" = None
    if serve_from == ServeFrom.DUCKDB:
        smallest_subset_db = (
            managed_root / subset_dirname(scale_factors[0]) / "subset.duckdb"
        )

        def schema_factory() -> str:
            return schema_ddl_from_duckdb(smallest_subset_db, resolved_tables)

    return _register_static_workload(
        name=name,
        sql_by_id=sql_by_id,
        parquet_dir=managed_root,
        tables=resolved_tables,
        dataset_name=dataset_name,
        scale_factors=scale_factors,
        schema_example_table=schema_example_table,
        params_by_id=_normalize_params(params_by_id, source),
        params_source=source,
        dataset_version=dataset_version,
        schema_factory=schema_factory,
        serve_from=serve_from,
    )
