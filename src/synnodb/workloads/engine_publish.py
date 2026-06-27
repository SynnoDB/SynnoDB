"""Factory-side publishing: make a generated engine discoverable by the runtime.

After a base/optimized implementation finishes, the engine workspace holds a compiled ``db``
binary and its sources, but nothing the router can read. This module derives router templates
from the workload's ``[NAME]`` query templates, self-validates each against a concrete
instantiation (so only a template that provably matches and binds its own query is shipped),
writes a ``manifest.json``, and atomically copies the engine into the engines directory the
runtime auto-discovers.

A self-check is the safety net: deriving a marker template from arbitrary SQL is heuristic, so
each candidate is verified to (1) normalize to the same structural key as a real instantiation
and (2) bind that instantiation's values back. A query that does not validate is skipped (it
just falls back to DuckDB) rather than shipped wrong.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from ..duckdb_compat.discovery import resolve_engines_dir
from ..router.manifest import EngineManifest, QueryTemplate, build_manifest_from_dir, infer_duckdb_type
from ..router.normalize import normalize_sql, unify_and_bind
from ..router.registry import PlaceholderSpec
from .param_infer import render_value, substitute

log = logging.getLogger("synnodb.engine_publish")

# A placeholder marker, with optional surrounding single quotes and an optional leading type
# keyword, so `date '[DATE]'`, `'[DELTA]'` and a bare `[DISCOUNT]` all collapse to one marker.
_MARKER = re.compile(r"(?:\b(?:date|timestamp|time)\b\s*)?'?\[([A-Za-z_]\w*)\]'?", re.IGNORECASE)
_BRACKET = re.compile(r"\[([A-Za-z_]\w*)\]")


def _ordered_names(bracket_sql: str) -> List[str]:
    """Placeholder names in source order, with repeats (e.g. ['DATE','DATE','DISCOUNT',...])."""
    return [m.group(1) for m in _BRACKET.finditer(bracket_sql)]


def _distinct_names(bracket_sql: str) -> List[str]:
    """Distinct placeholder names in first-seen order."""
    seen: dict[str, None] = {}
    for m in _BRACKET.finditer(bracket_sql):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _to_named(bracket_sql: str) -> str:
    """`date '[DATE]'` / `'[DELTA]'` / `[DISCOUNT]` -> `$DATE` / `$DELTA` / `$DISCOUNT`."""
    return _MARKER.sub(lambda m: f"${m.group(1)}", bracket_sql)


def _to_anon(bracket_sql: str) -> str:
    """Same, but to anonymous `?` markers."""
    return _MARKER.sub("?", bracket_sql)


def _binds_match(bound: Mapping[str, object], assignment: Mapping[str, object]) -> bool:
    """The values unify_and_bind recovered equal the values that were substituted in."""
    for name, value in assignment.items():
        if name not in bound:
            return False
        want = render_value(value)
        got = str(bound[name]).strip("'")
        if got != want and got != str(value):
            return False
    return True


def _validate(marker_sql: str, names: Sequence[str], bracket_sql: str,
              assignments: Sequence[Mapping[str, object]]) -> bool:
    """A derived template is valid if, for every sample assignment, it shares the structural
    key of the concrete query and binds that query's values back."""
    key = normalize_sql(marker_sql)
    if key is None:
        return False
    for assignment in assignments:
        concrete = substitute(bracket_sql, assignment)
        if normalize_sql(concrete) != key:
            return False
        bound = unify_and_bind(marker_sql, concrete, list(names))
        if bound is None or not _binds_match(bound, assignment):
            return False
    return True


def derive_template(
    bracket_sql: str, assignments: Sequence[Mapping[str, object]]
) -> Optional[Tuple[str, Tuple[PlaceholderSpec, ...]]]:
    """Derive a router template (marker SQL + typed placeholders) from a ``[NAME]`` template.

    Tries the named-marker form first, then the anonymous form, and returns the first that
    self-validates against the sample *assignments*. Returns ``None`` if neither validates or
    the template has no placeholders (a constant query needs no template to route as itself).
    Placeholder types are inferred from the sample values.
    """
    distinct = _distinct_names(bracket_sql)
    if not distinct:
        return None
    sample = assignments[0]

    def specs(names: Sequence[str]) -> Tuple[PlaceholderSpec, ...]:
        return tuple(PlaceholderSpec(n, infer_duckdb_type(sample.get(n))) for n in names)

    # Named markers carry their own name, so distinct placeholders suffice. Anonymous `?`
    # markers are bound and typed by position, so they need one placeholder per occurrence
    # in source order (a name that repeats, like Q6's [DATE], appears more than once).
    named = _to_named(bracket_sql)
    if _validate(named, distinct, bracket_sql, assignments):
        return named, specs(distinct)
    ordered = _ordered_names(bracket_sql)
    anon = _to_anon(bracket_sql)
    if _validate(anon, ordered, bracket_sql, assignments):
        return anon, specs(ordered)
    return None


def build_query_templates(
    templates_by_qid: Mapping[str, str],
    assignments_by_qid: Mapping[str, Sequence[Mapping[str, object]]],
) -> List[QueryTemplate]:
    """Derive a validated :class:`QueryTemplate` per query id. Queries whose template does not
    validate are skipped (logged); they simply keep falling back to DuckDB."""
    out: List[QueryTemplate] = []
    for qid, bracket_sql in templates_by_qid.items():
        if not _distinct_names(bracket_sql):
            # No placeholders: a constant query routes as itself (every literal matched
            # exactly). Ship it as-is when it parses.
            if normalize_sql(bracket_sql) is not None:
                out.append(QueryTemplate(str(qid), bracket_sql, ()))
            else:
                log.info("publish: query %s is not parseable, skipping", qid)
            continue
        assignments = assignments_by_qid.get(qid)
        if not assignments:
            log.debug("publish: no sample assignment for query %s, skipping", qid)
            continue
        derived = derive_template(bracket_sql, assignments)
        if derived is None:
            log.info("publish: could not derive a routable template for query %s, skipping", qid)
            continue
        marker_sql, placeholders = derived
        out.append(QueryTemplate(str(qid), marker_sql, placeholders))
    return out


# Build intermediates and per-run scratch are not needed by a published engine; the binary
# finds its build/*.so relative to its own location, so build/ (minus obj/) must come along.
_PUBLISH_IGNORE = shutil.ignore_patterns("obj", "results", "debug_logs", "__pycache__", ".git")


def _atomic_publish(workspace: Path, manifest: EngineManifest, engines_dir: Path) -> Path:
    """Copy the engine into ``engines_dir/<engine_id>`` via a staging directory renamed into
    place, so a partially-written engine is never discovered. The published engine is a
    self-contained copy (binary + build/*.so + sources + manifest), so it survives workspace
    cleanup; compile intermediates and per-run scratch are left behind."""
    engines_dir.mkdir(parents=True, exist_ok=True)
    dest = engines_dir / manifest.engine_id
    if dest.exists():
        return dest  # content-addressed id: an identical engine is already published
    staging = Path(tempfile.mkdtemp(prefix=f".tmp-{manifest.engine_id}-", dir=engines_dir))
    try:
        shutil.copytree(workspace, staging, ignore=_PUBLISH_IGNORE, dirs_exist_ok=True)
        manifest.write(staging)
        os.replace(staging, dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def publish_engine(
    workspace: "str | Path",
    *,
    query_templates: Sequence[QueryTemplate],
    parquet_dir: "str | Path",
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    storage_mode: str = "flat",
    expected_tables: Optional[Mapping[str, Sequence]] = None,
    engines_dir: "str | Path | None" = None,
) -> Optional[Path]:
    """Write a manifest for the engine in *workspace* and publish it for auto-discovery.

    Returns the published directory, or ``None`` when there is nothing to publish (no
    validated templates) or no engines directory is configured. Best-effort by contract: the
    caller (a generation stage) wraps this so a publish failure never fails the run.
    """
    workspace = Path(workspace)
    target = resolve_engines_dir(engines_dir)
    if target is None:
        log.info("publish: no engines directory configured (set SYNNO_ENGINES_DIR), skipping")
        return None
    if not query_templates:
        log.info("publish: no routable templates for the engine in %s, skipping", workspace)
        return None
    manifest = build_manifest_from_dir(
        workspace,
        query_templates,
        storage_mode=storage_mode,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        expected_tables={t: tuple(c) for t, c in (expected_tables or {}).items()},
        parquet_dir=str(parquet_dir),
        write=False,
    )
    dest = _atomic_publish(workspace, manifest, target)
    log.info(
        "published engine %s (%d queries) -> %s",
        manifest.engine_id, len(manifest.queries), dest,
    )
    return dest


def _lookup_template(sql_dict: Mapping[str, str], qid: str) -> Optional[str]:
    """Find a query's template across the key forms workloads use ("1" / "Q1" / "query1")."""
    if not hasattr(sql_dict, "get"):
        return None
    for key in (qid, f"Q{qid}", f"q{qid}", f"query{qid}"):
        if key in sql_dict:
            return sql_dict[key]
    return None


def _sample_assignments(provider: object, query_id: str, n: int) -> List[Mapping[str, object]]:
    """A few placeholder assignments for a query, from the workload's own generator (which
    needs no live data: TPC-H draws from fixed ranges, bring-your-own from pre-inferred
    values). Best-effort: returns what it can, possibly empty."""
    import random

    gen = provider._get_query_gen_fn()  # type: ignore[attr-defined]
    rnd = random.Random(0)
    samples: List[Mapping[str, object]] = []
    for _ in range(n):
        try:
            _, _, placeholders = gen(query_name=f"Q{query_id}", rnd=rnd)
        except Exception as exc:
            log.debug("publish: generator failed for query %s: %s", query_id, exc)
            break
        if placeholders:
            samples.append(dict(placeholders))
    return samples


def publish_from_provider(
    workspace: "str | Path",
    provider: object,
    query_ids: Iterable[str],
    *,
    parquet_dir: "str | Path",
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    storage_mode: str = "flat",
    engines_dir: "str | Path | None" = None,
    num_samples: int = 3,
) -> Optional[Path]:
    """Publish the engine in *workspace*, taking query templates from *provider*'s workload.

    Pulls each query's ``[NAME]`` template from ``provider.sql_dict`` and a few sample
    assignments from the workload generator, then derives, validates, and publishes. Designed
    to be called best-effort at the end of a generation run.
    """
    sql_dict = getattr(provider, "sql_dict", {})
    templates_by_qid: dict[str, str] = {}
    assignments_by_qid: dict[str, List[Mapping[str, object]]] = {}
    for qid in query_ids:
        bracket = _lookup_template(sql_dict, str(qid))
        if not bracket:
            log.debug("publish: no template for query %s in the workload, skipping", qid)
            continue
        # The manifest query id is the bare id the engine binary dispatches on ("1"), even
        # though the workload keys its templates as "Q1".
        templates_by_qid[str(qid)] = bracket
        assignments_by_qid[str(qid)] = _sample_assignments(provider, str(qid), num_samples)
    query_templates = build_query_templates(templates_by_qid, assignments_by_qid)
    return publish_engine(
        workspace,
        query_templates=query_templates,
        parquet_dir=parquet_dir,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        storage_mode=storage_mode,
        engines_dir=engines_dir,
    )
