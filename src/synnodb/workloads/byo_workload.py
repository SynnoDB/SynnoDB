"""Register a workload from data: queries plus existing parquet, with no source edits.

Two input forms, both bringing their parameter values explicitly (no inference):

  * a single ``queries.json`` mapping each query id to ``{"sql": ..., "params": {PH: [v,...]}}``
    (a plain SQL string is shorthand for a static query with no params);
  * a directory of ``*.sql`` files (filename stem = query id) plus an optional sidecar
    ``params.json`` of the form ``{id: {PH: [values]}}``.

The schema shown to the planner is derived from the parquet via DuckDB DESCRIBE; table names
are inferred from the parquet directory. A templated query's per-placeholder value lists are
expanded into instantiations by :func:`query_params.expand_param_grid`; static (parameterless)
queries get an identity generator.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING

from synnodb.workloads.query_params import (
    expand_param_grid,
    find_placeholders,
    substitute,
)
from synnodb.workloads.workload_spec import WorkloadSpec, register_workload

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
    assignments: dict[str, list[dict]],
) -> None:
    """Registration-time self-check, plus a debug dump under SYNNODB_BYO_DEBUG=1.

    Catches the args/parser quoting mismatch ('failed to parse <PH>') at registration
    rather than mid-run, and logs what each query's engine will receive (sample SQL +
    args line).
    """
    from synnodb.workloads.workload_provider import format_args_element

    logger.debug("BYO workload %r: tables=%s queries=%s", name, resolved_tables, ids)
    for qid in ids:
        qn = f"Q{qid}"
        if qid not in assignments:
            logger.debug("  %s: static query (no placeholders)", qn)
            continue
        sample = assignments[qid][0]
        # Every placeholder value must be a string (or an IN-list). The args line quotes
        # all values and the generated parser reads std::quoted into a std::string field,
        # so a non-string value fails at runtime with "Q<id>: failed to parse <name>".
        for pname, pval in sample.items():
            is_in_list = isinstance(pval, str) and pval.startswith("(")
            if not is_in_list and not isinstance(pval, str):
                raise ValueError(
                    f"{qn} placeholder '{pname}'={pval!r} is {type(pval).__name__}; "
                    f"placeholder values must be strings (the args line is quoted and the "
                    f"generated C++ parser reads std::quoted into a std::string field). "
                    f"Otherwise the engine fails with 'failed to parse {pname}'."
                )
        inst_sql = " ".join(substitute(sql_dict[qn], sample).split())
        args_line = format_args_element(qid, sample)
        logger.debug("  %s: %d instantiations, sample params=%s", qn, len(assignments[qid]), sample)
        logger.debug("  %s: engine args line -> %s", qn, args_line)
        logger.debug("  %s: instantiated SQL -> %s", qn, inst_sql)


def _normalize_params(raw: dict, source: str) -> dict[str, dict]:
    """Normalize the keys of a params mapping (``{qid: {PH: [values]}}``) to the canonical
    bare query ids, reusing the same key parsing as the queries so ``19``/``q19``/``Q19`` all
    resolve."""
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


def _load_template_assignments(
    sql_by_id: dict[str, str],
    params_by_id: dict[str, dict],
    params_source: str = "params",
) -> dict[str, list[dict]]:
    """Expand each templated query's per-placeholder value lists into instantiations.

    Every query with ``[PLACEHOLDER]`` holes must have a matching ``params`` entry; one that
    does not is an error (we do not invent values). Static queries are skipped (no entry).
    """
    templated = {qid: sql for qid, sql in sql_by_id.items() if find_placeholders(sql)}
    unknown = set(params_by_id) - set(templated)
    if unknown:
        logger.warning(
            "%s has entries for queries that are not templated/known and were ignored: %s",
            params_source, sorted(unknown),
        )
    missing = sorted(qid for qid in templated if qid not in params_by_id)
    if missing:
        raise ValueError(
            f"Templated queries {missing} have no parameter values. Provide them in "
            f'{params_source}, e.g. "{missing[0]}": {{"params": {{"<PLACEHOLDER>": '
            f'["<value>", ...]}}}}.'
        )
    out: dict[str, list[dict]] = {}
    for qid, tmpl in templated.items():
        try:
            out[qid] = expand_param_grid(tmpl, params_by_id[qid])
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
    # framework convention: <parquet_dir>/sf<sf>/<table>.parquet
    return parquet_dir / f"sf{sf}"


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


def schema_ddl_from_parquet(
    parquet_dir: str | Path, tables: list[str], sf: float
) -> str:
    """Derive a CREATE TABLE DDL string for the planner from the parquet files
    themselves, so there is no hand-written schema to keep in sync."""
    import duckdb

    base = _sf_dir(Path(parquet_dir), sf)
    con = duckdb.connect()
    parts: list[str] = []
    for t in tables:
        path = base / f"{t}.parquet"
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
        ).fetchall()
        cols = ",\n    ".join(f"{r[0]} {r[1]}" for r in rows)
        parts.append(f"CREATE TABLE {t} (\n    {cols}\n);")
    con.close()
    return "\n\n".join(parts)


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

    Templated queries take their values from a sidecar ``<sql_dir>/params.json`` of the form
    ``{id: {PLACEHOLDER: [values]}}``.
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
        params_by_id = _normalize_params(json.loads(params_path.read_text()), str(params_path))
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

      * an object ``{"sql": <str>, "params": {PLACEHOLDER: [values, ...]}}`` - the per-
        placeholder value lists are expanded (index-zipped, length-1 broadcast) into the
        query's instantiations; ``params`` may be omitted for a static query; or
      * a plain SQL string - shorthand for a static query with no params (an error if it
        actually contains ``[PLACEHOLDER]`` holes).

    One self-contained input artifact: SQL and parameter values live together, the shape a
    dashboard would populate.
    """
    queries_json = Path(queries_json)
    raw = json.loads(queries_json.read_text())
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{queries_json} must be a non-empty JSON object mapping query-id -> "
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
                f'{queries_json}: query {qid!r} must be a SQL string or an object with a '
                f'"sql" key, got {type(entry).__name__}.'
            )
        sql_by_id[qid] = str(entry["sql"])
        if entry.get("params"):
            params_by_id[qid] = entry["params"]

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
) -> WorkloadSpec:
    """Shared builder: turn an ``{id: sql}`` map + parquet into a registered workload.
    Schema is derived from the parquet; tables inferred if not given. Templated queries are
    filled from ``params_by_id`` (per-placeholder value lists); static queries get an
    identity generator."""
    parquet_dir = Path(parquet_dir)
    # Normalize keys (q1/Q1/query1/1/2b) to canonical bare ids, reliably + reported.
    key_to_id = _normalize_query_keys(list(sql_by_id))
    sql_by_norm = {key_to_id[k]: v for k, v in sql_by_id.items()}
    ids = _natural_sort(list(sql_by_norm))
    sql_dict = {f"Q{qid}": sql_by_norm[qid] for qid in ids}

    resolved_tables = tables or infer_tables_from_parquet(parquet_dir, scale_factors[0])

    def schema_factory() -> str:
        return schema_ddl_from_parquet(parquet_dir, resolved_tables, scale_factors[0])

    # Templated queries (those with [PLACEHOLDER] holes) are filled from the user-supplied
    # per-placeholder value lists; static queries use an identity generator. Params keys were
    # already normalized to bare ids (same _QID_RE) by the callers.
    assignments = _load_template_assignments(sql_by_norm, params_by_id or {}, params_source)
    # Validate the placeholder values and, under SYNNODB_BYO_DEBUG, log the SQL + args
    # line per query.
    _preflight_workload(name, sql_dict, ids, resolved_tables, assignments)

    def _bare(query_name: str) -> str:
        return query_name[1:] if query_name.startswith("Q") else query_name

    def query_gen_factory(provider: "OLAPWorkloadProvider"):
        def gen(query_name: str, rnd=None):
            qid = _bare(query_name)
            template = sql_dict[query_name]
            if qid in assignments:
                r = rnd or random.Random(0)
                assign = r.choice(assignments[qid])
                return (query_name, substitute(template, assign), assign)
            return (query_name, template, {})

        return gen

    def placeholders_factory(provider: "OLAPWorkloadProvider", do_not_cache: bool = False):
        def gen(query_name: str, **_):
            qid = _bare(query_name)
            # representative placeholder set for arg-parser / engine generation
            return assignments[qid][0] if qid in assignments else {}

        return gen

    spec = WorkloadSpec(
        name=name,
        tables=tuple(resolved_tables),
        dataset_name=dataset_name or name,
        all_query_ids=tuple(ids),
        benchmark_sf=scale_factors[-1],
        fast_check_sfs=scale_factors,
        exhaustive_sfs=scale_factors,
        ingest_sfs=(scale_factors[-1],),
        example_query=f"Q{ids[0]}",
        example_query_params=ids[0],
        schema_example_table=schema_example_table or resolved_tables[0],
        sql_dict_factory=lambda: sql_dict,
        schema_factory=schema_factory,
        query_gen_factory=query_gen_factory,
        placeholders_factory=placeholders_factory,
        base_parquet_dir=parquet_dir,
    )
    register_workload(spec)
    return spec
