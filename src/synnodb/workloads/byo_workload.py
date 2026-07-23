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
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from synnodb.utils.utils import ServeFrom
from synnodb.workloads.query_params import (
    ParamSpace,
    find_placeholders,
    parse_param_space,
    substitute,
)
from synnodb.workloads.workload_spec import (
    DuckDBSubsetSource,
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
            if (
                not is_in_list
                and len(pval) >= 2
                and pval.startswith("'")
                and pval.endswith("'")
            ):
                logger.warning(
                    "%s placeholder '%s' sample value %r looks like a quoted SQL literal. "
                    "The args line double-quotes it and the generated C++ parser strips only "
                    "those wire quotes, so the single quotes reach the engine inside the "
                    "string field and lookups will miss. Put the quotes in the template "
                    "('[%s]') and supply bare values (see "
                    "synnodb.workloads.query_params.hoist_literal_quotes).",
                    qn,
                    pname,
                    pval,
                    pname,
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
    """Order query ids in natural (human) order so the catalog does not depend on filesystem
    listing order and a range like ``1a-11b`` spans the ids one expects.

    Digit runs compare as numbers, so ``1a < 2a < 10a < 11a < 11b`` (not the lexical ``10a <
    1a``) and pure-numeric ids keep TPC-H's ``1..22`` order."""
    return sorted(
        ids,
        key=lambda s: [int(t) if t.isdigit() else t for t in re.findall(r"\d+|\D+", s)],
    )


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
    """Subset values that actually have data on disk, ascending. See
    :func:`~synnodb.workloads.workload_spec.discover_subset_values`, the single scanner for both
    naming conventions (``fraction<f>`` and the legacy ``sf<N>``)."""
    from synnodb.workloads.workload_spec import discover_subset_values

    return discover_subset_values(parquet_dir)


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
    duckdb_source: "DuckDBSubsetSource | None" = None,
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
        duckdb_source=duckdb_source,
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


def read_manifest_dataset_version(managed_root: str | Path) -> str | None:
    """The ``dataset_version`` recorded in the subset manifest under ``managed_root``, or None if
    there is no readable manifest. Lets the lazy downscaler (:meth:`OLAPWorkloadProvider.prepare`)
    treat fractional subsets left from a different source version as stale and rebuild them."""
    manifest = Path(managed_root) / _SUBSET_MANIFEST
    try:
        return json.loads(manifest.read_text()).get("dataset_version")
    except (OSError, ValueError):
        return None


# The frozen point-in-time copy of a live source connection, kept under ``managed_root``. Every
# subset is derived from it, and a DuckDB-native benchmark subset symlinks it, so the caller may
# keep writing to their own database in parallel without perturbing the run.
_SOURCE_SNAPSHOT = ".source_snapshot.duckdb"


@contextmanager
def _static_source(
    con,
    source_db_path: "str | Path | None",
    source_is_static: bool,
    managed_root: Path,
) -> "Iterator[tuple[str, Any]]":
    """Yield ``(static_source_path, read_only_connection)`` for a source guaranteed not to change
    under us while the subsets are materialized.

    * A SynnoDB-owned read-only path (``source_is_static``) is already frozen - nothing writes it -
      so it is read in place through the handle the caller opened.
    * A caller-supplied live connection may still be written to in parallel, so a consistent
      point-in-time snapshot is copied to ``<managed_root>/.source_snapshot.duckdb`` first and that
      frozen file, opened read-only, becomes the source everything downstream derives from.
    """
    import duckdb

    from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
        snapshot_source_database,
    )

    if source_is_static:
        if source_db_path is None:
            raise ValueError(
                "a static DuckDB source requires its file path (source_db_path)."
            )
        yield str(Path(source_db_path).resolve()), con
        return

    managed_root.mkdir(parents=True, exist_ok=True)
    snapshot_path = managed_root / _SOURCE_SNAPSHOT
    snapshot_source_database(con, snapshot_path)
    snap_con = duckdb.connect(str(snapshot_path), read_only=True)
    try:
        yield str(snapshot_path.resolve()), snap_con
    finally:
        snap_con.close()


def _subset_artifacts(
    managed_root: Path, fraction: float, serve_from: ServeFrom, tables: list[str]
) -> list[Path]:
    """The files that must all exist for ``fraction``'s subset to count as materialized."""
    out_dir = managed_root / subset_dirname(fraction)
    if serve_from == ServeFrom.DUCKDB:
        return [out_dir / "subset.duckdb"]
    return [out_dir / f"{t}.parquet" for t in tables]


def _benchmark_is_current(
    managed_root: Path,
    dataset_version: str,
    serve_from: ServeFrom,
    tables: list[str],
) -> bool:
    """True iff the benchmark subset (``fraction1``, the full data) under ``managed_root`` was
    built from ``dataset_version`` and all its artifacts are present - so it can be reused verbatim
    (no re-snapshot, no rebuild). A missing manifest, a mismatched version (source/params/algorithm
    changed), or a missing/partial benchmark artifact all return False so the caller rebuilds. Only
    the benchmark subset is materialized eagerly at sync; the fractional rungs are built lazily by
    :meth:`OLAPWorkloadProvider.prepare`, so this checks ``fraction1`` alone rather than every
    fraction."""
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
        art.exists() for art in _subset_artifacts(managed_root, 1.0, serve_from, tables)
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


def materialize_duckdb_subsets(
    source: DuckDBSubsetSource,
    fractions: list[float],
    managed_root: Path,
    serve_from: ServeFrom,
) -> None:
    """Downscale ``fractions`` of the frozen source into ``managed_root/fraction<f>/`` on demand -
    the lazy counterpart to the benchmark subset materialized at sync time. Invoked from
    :meth:`OLAPWorkloadProvider.prepare`, never from the registration/ingestion path (so a plain
    re-ingest never re-downscales).

    Each requested fraction's directory is cleared first, so a leftover ``.partial`` or a
    half-written subset from an interrupted build cannot trip ``copy_subset_to_duckdb``'s
    exists-guard; the downscaler's own ``.partial`` + atomic rename then makes each final artifact
    all-or-nothing. The frozen source is opened read-only - the caller may still be writing their
    own database in parallel."""
    import shutil

    import duckdb

    from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
        ReferentialDownscaler,
    )

    if not fractions:
        return
    con = duckdb.connect(source.frozen_source_path, read_only=True)
    try:
        downscaler = ReferentialDownscaler(
            con,
            sql_by_id=source.sql_by_id,
            join_relationships=source.join_relationships,
            whole_table_threshold=source.whole_table_threshold,
        )
        for fraction in fractions:
            out_dir = managed_root / subset_dirname(fraction)
            shutil.rmtree(out_dir, ignore_errors=True)
            logger.info("Downscaling subset fraction=%g on demand", fraction)
            if serve_from == ServeFrom.DUCKDB:
                downscaler.copy_subset_to_duckdb(fraction, out_dir / "subset.duckdb")
            else:
                downscaler.copy_subset_to_parquet(fraction, out_dir)
    finally:
        con.close()


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
    source_is_static: bool = False,
    always_resample: bool = False,
) -> WorkloadSpec:
    """Register a workload sourced from a DuckDB connection, deriving the fast-check subsets by
    FK-preserving downscaling instead of taking pre-scaled parquet (the connection-sourced sibling
    of :func:`register_workload_from_json`).

    Registration only **snapshots** the source and materializes the full ``fraction1`` benchmark
    subset. The fractional downscaled rungs (``downscale_fractions``) are **not** built here - they
    are downscaled lazily from the retained snapshot at the first synthesis run
    (:meth:`OLAPWorkloadProvider.prepare`, driven by the :class:`DuckDBSubsetSource` carried on the
    spec). So a plain re-ingest never re-downscales; it only re-snapshots.

    The source is frozen before anything is read from it. A caller-supplied *live* connection may
    be written to in parallel (the notebook flow: ``conn = duckdb.connect(path)`` stays open and
    the user keeps working), so a consistent point-in-time snapshot is copied into
    ``<managed_root>/.source_snapshot.duckdb`` first (:func:`snapshot_source_database`) and the
    benchmark subset plus every lazily-built rung are derived from that immutable image - the
    caller's ongoing writes never perturb the run. The snapshot is **retained** for the workload's
    lifetime (both storage formats) because the lazy downscaler reads from it. A SynnoDB-owned
    read-only path (``source_is_static``) is already frozen and read in place, with no snapshot copy;
    the lazy downscaler reads that file directly.

    Two subset representations, selected by ``serve_from``:

    * ``ServeFrom.DUCKDB`` (the default): the referential downscaler
      (:mod:`synnodb.workloads.dataset.custom_scaler.duckdb_downscale`) materializes each
      downscaled subset as ``<managed_root>/fraction<f>/subset.duckdb`` (lazily). The full
      ``fraction1`` benchmark subset is a zero-copy symlink to the frozen source (the caller's
      read-only file, or the snapshot we own) - never the caller's live database - so the
      framework's later read-only opens never collide with a read-write handle the caller may still
      hold. No parquet touches disk: the candidate engine ingests the subset over the shm plane and
      the DuckDB oracle materializes flat tables from it.
    * ``ServeFrom.PARQUET``: the same downscaler writes
      ``<managed_root>/fraction<f>/<table>.parquet`` (lazily) and the workload is registered exactly
      like a bring-your-own parquet workload, so the whole factory + oracle run against it unchanged.

    The caller's database is only read; nothing is written back.

    Args:
        name: workload id (used as the benchmark name / WorkloadId).
        con: a ``duckdb.DuckDBPyConnection`` to source schema + data from.
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
        source_db_path: the source ``.duckdb`` path. Required when ``source_is_static`` (it is the
            symlink target for the DuckDB benchmark subset); ignored for a live source, which is
            snapshotted instead.
        source_is_static: True when ``con`` is a SynnoDB-owned read-only handle to a file nothing
            writes for the workload's lifetime - the source is read in place and a prior benchmark
            subset may be reused. False (the default) treats ``con`` as a live connection the caller
            may keep writing to: it is snapshotted to a frozen image first, and re-snapshotted only
            when its fingerprint moved (or ``always_resample`` forces it).
        always_resample: force a fresh snapshot + benchmark subset (and, lazily, fractional rungs)
            on every call, bypassing the fingerprint-reuse default. By default the source is
            fingerprinted in place - a cheap read-only introspection of its schema, per-table row
            counts and inferred join graph - and the snapshot + benchmark subset are reused whenever
            that fingerprint matches the one they were built from; any change that alters which rows a
            subset contains (a new/renamed table or column, a different row count, a changed join
            graph, fractions, threshold, storage format, or DuckDB version) moves the fingerprint and
            re-snapshots on its own. The fingerprint does not read row *values*, so a source mutated
            in place without changing counts or schema keeps the same fingerprint and would be reused
            stale; set ``always_resample=True`` for such a source to guarantee fresh data every run.
            Governs snapshot reuse only, not downscaling (always lazy).
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

    # Subset ladder = the downscale fractions (fast-check rungs) plus the full ``1.0`` benchmark subset
    # last (the shared builder treats the last scale factor as the benchmark/target).
    subsets = tuple(sorted(set(downscale_fractions)))
    if any(not (0.0 < t < 1.0) for t in subsets):
        raise ValueError(
            f"downscale_fractions must be fractions in (0, 1); got {downscale_fractions}."
        )
    scale_factors = subsets + (1.0,)
    managed_root = Path(managed_root)
    managed_root.mkdir(parents=True, exist_ok=True)

    if source_is_static and source_db_path is None:
        raise ValueError(
            "a static DuckDB source requires its file path (source_db_path)."
        )

    # Probe the source in place (a cheap read-only introspection of schema, row counts and the
    # inferred join graph) to resolve the tables and the ``dataset_version`` fingerprint. Needed in
    # every branch: it drives the reuse decision and is recorded on the spec + manifest.
    probe = ReferentialDownscaler(
        con,
        sql_by_id=sql_by_id,
        join_relationships=join_relationships,
        whole_table_threshold=whole_table_threshold,
    )
    resolved_tables: list[str] = tables or list(probe.schema.tables)
    dataset_version: str = _duckdb_dataset_version(
        probe, subsets, whole_table_threshold, serve_from
    )

    # Reuse the materialized subsets verbatim (no re-snapshot, no rebuild) whenever they are current
    # for the source, so re-running the same notebook against unchanged data is cheap. A rebuild
    # clears any already-materialized fractional subsets so ``prepare`` re-derives them from the new
    # snapshot (downscaling always stays lazy); reuse keeps them.
    benchmark_current = _benchmark_is_current(
        managed_root, dataset_version, serve_from, resolved_tables
    )
    if always_resample:
        reused = False
    elif source_is_static:
        # A static source is read in place with no snapshot copy; its benchmark subset is a symlink,
        # so a current fingerprint alone is enough to reuse.
        reused = benchmark_current
    else:
        # A live source's lazy downscaler reads from the retained snapshot, so reuse also requires
        # that snapshot to still exist.
        reused = benchmark_current and (managed_root / _SOURCE_SNAPSHOT).exists()

    if reused:
        logger.info(
            "Benchmark subset under %s already current (dataset_version=%s); skipping snapshot + "
            "materialization",
            managed_root,
            dataset_version,
        )
    else:
        # This is a resync: the source data is being swapped out. Any warm engine process from a
        # previous run still holds the old snapshot resident (the loader ingests once per process and
        # never re-reads its input), so retire the whole warm runtime now - the next run spawns fresh
        # processes that load the rebuilt subsets instead of serving stale rows from RAM.
        from synnodb.cpp_runner.runtime_reset import reset_warm_runtime

        reset_warm_runtime()

        # Freeze the source, then read the schema/join graph and materialize the benchmark subset
        # from that frozen image only - never from a database the caller might still be mutating. A
        # live source that changed between the in-place probe and this freeze is re-fingerprinted
        # here, so the manifest always describes exactly what was built.
        with _static_source(con, source_db_path, source_is_static, managed_root) as (
            static_path,
            static_con,
        ):
            downscaler = ReferentialDownscaler(
                static_con,
                sql_by_id=sql_by_id,
                join_relationships=join_relationships,
                whole_table_threshold=whole_table_threshold,
            )
            resolved_tables = tables or list(downscaler.schema.tables)
            dataset_version = _duckdb_dataset_version(
                downscaler, subsets, whole_table_threshold, serve_from
            )

            # Clear every fraction dir (including any stale fractional subsets) and rebuild only the
            # benchmark subset; the fractional rungs are rebuilt lazily from the fresh snapshot.
            _clear_managed_subsets(managed_root)
            out_dir = managed_root / subset_dirname(1.0)
            logger.info("Creating benchmark subset (full data)")
            if serve_from == ServeFrom.DUCKDB:
                # The benchmark subset is the full frozen source itself - a zero-copy symlink to it.
                # ``static_path`` is the caller's read-only file (static source) or the snapshot we
                # own (live source); both are immutable for the workload's lifetime, so read-only
                # opens of the symlink never collide with a handle the caller may still hold.
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "subset.duckdb").symlink_to(Path(static_path).resolve())
            else:
                downscaler.copy_subset_to_parquet(1.0, out_dir)
            (managed_root / _SUBSET_MANIFEST).write_text(
                json.dumps(
                    {"dataset_version": dataset_version, "serve_from": serve_from.value}
                )
            )

    # The frozen source the lazy downscaler reads from: the snapshot we own for a live source (kept
    # for the workload's lifetime, unlike before - ``prepare`` derives fractional subsets from it),
    # or the caller's read-only file for a static source.
    frozen_source_path = (
        str(Path(source_db_path).resolve())
        if source_is_static
        else str((managed_root / _SOURCE_SNAPSHOT).resolve())
    )
    duckdb_source = DuckDBSubsetSource(
        frozen_source_path=frozen_source_path,
        sql_by_id=sql_by_id,
        join_relationships=join_relationships,
        whole_table_threshold=whole_table_threshold,
    )

    # Schema is derived from the ``fraction1`` benchmark subset, which always exists after sync
    # (the fractional subsets are lazy and may not exist yet). The schema is identical across
    # subsets - downscaling preserves it - so this is exact.
    if serve_from == ServeFrom.DUCKDB:
        benchmark_subset_db = managed_root / subset_dirname(1.0) / "subset.duckdb"

        def schema_factory() -> str:
            return schema_ddl_from_duckdb(benchmark_subset_db, resolved_tables)

    else:

        def schema_factory() -> str:
            return schema_ddl_from_parquet(managed_root, resolved_tables, 1.0)

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
        duckdb_source=duckdb_source,
    )
