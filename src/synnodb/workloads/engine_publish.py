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

import contextlib
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..duckdb_compat.discovery import resolve_engines_dir

if TYPE_CHECKING:
    from .validation_receipt import ValidationReceipt
from ..router.manifest import (
    EngineManifest,
    QueryTemplate,
    build_manifest_from_dir,
    infer_duckdb_type,
)
from ..router.normalize import bind_template, normalize_sql, scan_string_literals
from ..router.registry import PlaceholderSpec
from .query_params import render_value, substitute

log = logging.getLogger("synnodb.engine_publish")

# A placeholder marker, with optional surrounding single quotes and an optional leading type
# keyword, so `date '[DATE]'`, `'[DELTA]'` and a bare `[DISCOUNT]` all collapse to one marker.
_MARKER = re.compile(
    r"(?:\b(?:date|timestamp|time)\b\s*)?'?\[([A-Za-z_]\w*)\]'?", re.IGNORECASE
)
_BRACKET = re.compile(r"\[([A-Za-z_]\w*)\]")

# One embedded-parameter string literal: its ``(start, close)`` span in the source and the
# ``names``/``consts`` its inner text splits into (``len(consts) == len(names) + 1``).
_Embedded = Tuple[int, int, List[str], List[str]]


def _parse_embedded(inner: str) -> Optional[Tuple[List[str], List[str]]]:
    """Split a literal's inner text into (names, consts), where ``consts`` are the ``len(names)+1``
    constant fragments around the markers. ``'%[W1]%[W2]%'`` -> (['W1','W2'], ['%','%','%']).
    Returns ``None`` for a literal with no marker (a plain constant like ``'SM CASE'``)."""
    parts = _BRACKET.split(inner)  # ['%', 'W1', '%', 'W2', '%']
    if len(parts) < 3:
        return None
    return parts[1::2], parts[0::2]


def _embedded_literals(bracket_sql: str) -> List[_Embedded]:
    """Every string literal that embeds parameter(s) in constant text - a LIKE affix or Q13's
    two-word ``'%[W1]%[W2]%'`` - scanned once. A bare ``'[REGION]'`` (one marker, no surrounding
    constants) is excluded: it is an ordinary whole-literal parameter handled by the plain marker
    path, as is a plain constant like ``'SM CASE'``."""
    out: List[_Embedded] = []
    for start, close, inner in scan_string_literals(bracket_sql):
        parsed = _parse_embedded(inner)
        if parsed is None:
            continue
        names, consts = parsed
        if len(names) == 1 and not consts[0] and not consts[1]:
            continue
        out.append((start, close, names, consts))
    return out


def _embedded_info(literals: Sequence[_Embedded]) -> dict[str, Tuple[str, str, int]]:
    """``{name: (prefix, suffix, group)}`` for every embedded parameter. ``prefix``/``suffix`` are
    the constants immediately before/after the parameter within the literal (``suffix`` is the
    delimiter to the next parameter, or the tail). Parameters packed in one literal share a
    ``group`` id (>= 0) so binding recovers them together; a lone affix gets ``-1``."""
    out: dict[str, Tuple[str, str, int]] = {}
    gid = 0
    for _, _, names, consts in literals:
        multi = len(names) > 1
        group = gid if multi else -1
        if multi:
            gid += 1
        for i, name in enumerate(names):
            out[name] = (consts[i], consts[i + 1], group)
    return out


def _collapse_embedded(bracket_sql: str, literals: Sequence[_Embedded]) -> str:
    """Replace each embedded-parameter literal with a single anonymous ``?`` marker (the whole
    literal is one bind value, split back into its parameters at bind time); everything else is
    left for ``_MARKER``."""
    out: List[str] = []
    last = 0
    for start, close, _, _ in literals:
        out.append(bracket_sql[last:start])
        out.append("?")
        last = close + 1
    out.append(bracket_sql[last:])
    return "".join(out)


def _ordered_names(bracket_sql: str) -> List[str]:
    """Placeholder names in source order, with repeats (e.g. ['DATE','DATE','DISCOUNT',...])."""
    return [m.group(1) for m in _BRACKET.finditer(bracket_sql)]


def _distinct_names(bracket_sql: str) -> List[str]:
    """Distinct placeholder names in first-seen order."""
    seen: dict[str, None] = {}
    for m in _BRACKET.finditer(bracket_sql):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _to_anon(bracket_sql: str, literals: Sequence[_Embedded]) -> str:
    """`date '[DATE]'` / `'[DELTA]'` / `[DISCOUNT]` -> `?`, and an affixed literal `'%[TYPE]'`
    (or a multi-parameter one like `'%[W1]%[W2]%'`) -> a single `?` for the whole literal."""
    s = _collapse_embedded(bracket_sql, literals)
    return _MARKER.sub("?", s)


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


def _validate(
    marker_sql: str,
    specs: Sequence[PlaceholderSpec],
    bracket_sql: str,
    assignments: Sequence[Mapping[str, object]],
) -> bool:
    """A derived template is valid if, for every sample assignment, it shares the structural
    key of the concrete query and binds that query's values back.

    Binding goes through :func:`bind_template` (not raw ``unify_and_bind``) so a LIKE affix is
    peeled exactly as the runtime router will peel it - the self-check then compares the stripped
    parameter (``BRASS``) against the generator's value, guaranteeing publish and runtime agree."""
    key = normalize_sql(marker_sql)
    if key is None:
        return False
    for assignment in assignments:
        concrete = substitute(bracket_sql, assignment)
        if normalize_sql(concrete) != key:
            return False
        bound = bind_template(marker_sql, concrete, specs)
        if bound is None or not _binds_match(bound, assignment):
            return False
    return True


def derive_template(
    bracket_sql: str, assignments: Sequence[Mapping[str, object]]
) -> Optional[Tuple[str, Tuple[PlaceholderSpec, ...]]]:
    """Derive a router template (marker SQL + typed placeholders) from a ``[NAME]`` template.

    Every marker becomes an anonymous ``?``, and the result ships only if it self-validates
    against the sample *assignments*. Returns ``None`` if it does not validate or the template
    has no placeholders (a constant query needs no template to route as itself). Placeholder
    types are inferred from the sample values.
    """
    distinct = _distinct_names(bracket_sql)
    if not distinct:
        return None
    # A workload generator may hand back keys that are not template placeholders (e.g. the
    # TPC-H generator tacks a `STREAM_ID` onto Q15). Keep only the values the template actually
    # binds, so the self-check compares placeholders and not incidental generator metadata.
    keep = set(distinct)
    assignments = [
        {k: v for k, v in a.items() if k in keep} for a in assignments
    ]
    sample = assignments[0]
    literals = _embedded_literals(bracket_sql)  # one scan, shared by info + collapse below
    embedded = _embedded_info(literals)

    def specs(names: Sequence[str]) -> Tuple[PlaceholderSpec, ...]:
        return tuple(
            PlaceholderSpec(
                n, infer_duckdb_type(sample.get(n)), *embedded.get(n, ("", "", -1))
            )
            for n in names
        )

    # Anonymous `?` markers are bound and typed by position, so they need one placeholder per
    # occurrence in source order (a name that repeats, like Q6's [DATE], appears more than once;
    # bind time enforces that its occurrences resolve to one consistent value).
    anon = _to_anon(bracket_sql, literals)
    anon_specs = specs(_ordered_names(bracket_sql))
    if _validate(anon, anon_specs, bracket_sql, assignments):
        return anon, anon_specs
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
            log.info(
                "publish: could not derive a routable template for query %s, skipping",
                qid,
            )
            continue
        marker_sql, placeholders = derived
        out.append(QueryTemplate(str(qid), marker_sql, placeholders))
    return out


# Build intermediates and per-run scratch are not needed by a published engine; the binary
# finds its build/*.so relative to its own location, so build/ (minus obj/) must come along.
_PUBLISH_IGNORE = shutil.ignore_patterns(
    "obj", "results", "debug_logs", "__pycache__", ".git"
)


def _populate(
    workspace: Path,
    staging: Path,
    manifest: EngineManifest,
    bundle_parquet_dir: "str | Path | None",
) -> None:
    """Fill *staging* with the self-contained engine: a copy of the workspace (binary + build/*.so
    + sources, minus scratch), an optional bundled parquet snapshot under ``data/``, and the
    manifest. The caller renames *staging* into its final place atomically."""
    shutil.copytree(workspace, staging, ignore=_PUBLISH_IGNORE, dirs_exist_ok=True)
    if bundle_parquet_dir is not None:
        data_dir = staging / "data"
        data_dir.mkdir(exist_ok=True)
        for pf in sorted(Path(bundle_parquet_dir).glob("*.parquet")):
            shutil.copy2(pf, data_dir / pf.name)
    manifest.write(staging)


@contextlib.contextmanager
def _publish_lock(engines_dir: Path, name: str):
    """Serialize publishers of the same *name*, so two concurrent republishes cannot interleave
    their swaps (which previously raised ``OSError: Directory not empty`` and leaked dirs). A
    coarse per-name file lock, held only for the brief materialize + symlink flip."""
    import fcntl  # POSIX; the publish/runtime stack is Linux

    locks = engines_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "engine"
    handle = open(locks / f"{safe}.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _version_prefix(name: str) -> str:
    # The "@" separator keeps prefixes unambiguous: "synno-tpch@..." is not a version of
    # "synno-tp", and "synno-tpch2@..." is not a version of "synno-tpch".
    return f"{name}@"


def _gc_versions(versions_dir: Path, name: str, keep: Path) -> None:
    """Remove superseded versions of *name* (every ``<name>@*`` under ``.versions`` except the one
    now linked). Scoped to this name and run under its lock, so it never races another name's
    in-flight version."""
    prefix = _version_prefix(name)
    keep_resolved = keep.resolve()
    for v in versions_dir.iterdir():
        if v.name.startswith(prefix) and v.resolve() != keep_resolved:
            shutil.rmtree(v, ignore_errors=True)


def _publish_content_addressed(
    workspace: Path,
    manifest: EngineManifest,
    engines_dir: Path,
    bundle_parquet_dir: "str | Path | None",
) -> Path:
    """The unnamed path: ``engines_dir/<engine_id>``. The id is a content hash, so an identical
    engine is already correct - dedup if present, otherwise stage and rename in atomically. A
    concurrent publisher of the same content is harmless (whoever lands first wins)."""
    dest = engines_dir / manifest.engine_id
    if dest.exists():
        return dest
    staging = Path(
        tempfile.mkdtemp(prefix=f".tmp-{manifest.engine_id}-", dir=engines_dir)
    )
    try:
        _populate(workspace, staging, manifest, bundle_parquet_dir)
        try:
            os.replace(staging, dest)
        except OSError:
            if (
                dest.exists()
            ):  # someone published the identical engine first; defer to it
                shutil.rmtree(staging, ignore_errors=True)
                return dest
            raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def _publish_named(
    workspace: Path,
    manifest: EngineManifest,
    engines_dir: Path,
    name: str,
    bundle_parquet_dir: "str | Path | None",
) -> Path:
    """Publish ``engines_dir/<name>`` (e.g. ``synno-tpch``) as an atomic symlink to an immutable
    version directory under ``.versions``.

    A directory cannot be replaced by ``rename(2)`` while non-empty, so the previous code did a
    two-step ``os.replace(dest->trash); os.replace(staging->dest)`` that was neither crash-atomic
    (a crash between the two deleted the only copy) nor concurrency-safe (two publishers raced to
    ``OSError``). Instead each publish writes a fresh immutable version and atomically *flips a
    symlink* at ``<name>`` to it: the flip is a single ``rename`` over a symlink, so a crash leaves
    either the old or the new version fully linked, never a missing engine, and a per-name lock
    serializes concurrent republishes.
    """
    versions = engines_dir / ".versions"
    versions.mkdir(parents=True, exist_ok=True)
    dest = engines_dir / name
    with _publish_lock(engines_dir, name):
        # 1. Materialize the new version: staging dir -> atomic rename into .versions/<name>@<id>.
        staging = Path(
            tempfile.mkdtemp(prefix=f".tmp-{manifest.engine_id}-", dir=engines_dir)
        )
        try:
            _populate(workspace, staging, manifest, bundle_parquet_dir)
            version = versions / f"{_version_prefix(name)}{uuid.uuid4().hex}"
            os.replace(staging, version)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # 2. Flip <name> to the new version atomically (a relative symlink keeps the dir portable).
        tmp_link = engines_dir / f".link-{uuid.uuid4().hex}"
        os.symlink(os.path.join(versions.name, version.name), tmp_link)
        try:
            if dest.exists() and not dest.is_symlink():
                # One-time migration of a legacy real directory: preserve the old copy under
                # .versions (never delete the only copy), then flip. The brief absence is under
                # the lock and discovery just retries on its next scan.
                os.replace(
                    dest, versions / f"{_version_prefix(name)}legacy-{uuid.uuid4().hex}"
                )
            os.replace(tmp_link, dest)  # atomic: dest is now absent or a symlink
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_link)
            raise
        # 3. Drop superseded versions of this name (keep the one just linked).
        _gc_versions(versions, name, version)
    return dest


def _atomic_publish(
    workspace: Path,
    manifest: EngineManifest,
    engines_dir: Path,
    *,
    name: Optional[str] = None,
    bundle_parquet_dir: "str | Path | None" = None,
) -> Path:
    """Publish the engine for auto-discovery. The published engine is a self-contained copy
    (binary + build/*.so + sources + manifest), so it survives workspace cleanup.

    Unnamed: content-addressed ``engines_dir/<engine_id>`` (dedups an identical engine). Named:
    ``engines_dir/<name>`` (e.g. ``synno-tpch``), published as an atomic, crash-safe,
    concurrency-safe symlink flip onto an immutable version directory. With *bundle_parquet_dir*,
    its ``*.parquet`` are copied into ``<version>/data/`` and recorded as the relative
    ``parquet_dir="data"`` so the package is portable.
    """
    engines_dir.mkdir(parents=True, exist_ok=True)
    if name is None:
        return _publish_content_addressed(
            workspace, manifest, engines_dir, bundle_parquet_dir
        )
    return _publish_named(workspace, manifest, engines_dir, name, bundle_parquet_dir)


def publish_engine(
    workspace: "str | Path",
    *,
    query_templates: Sequence[QueryTemplate],
    receipt: "ValidationReceipt",
    parquet_dir: "str | Path | None" = None,
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    storage_mode: str = "flat",
    expected_tables: Optional[Mapping[str, Sequence]] = None,
    engines_dir: "str | Path | None" = None,
    name: Optional[str] = None,
    shm_capable: bool = False,
    bundle_parquet_dir: "str | Path | None" = None,
    source_db: Optional[str] = None,
    threads: Optional[int] = None,
) -> Optional[Path]:
    """Write a manifest for the engine in *workspace* and publish it for auto-discovery.

    Returns the published directory, or ``None`` when there is nothing to publish (no
    validated templates) or no engines directory is configured. Best-effort by contract: the
    caller (a generation stage) wraps this so a publish failure never fails the run.

    *name* publishes under a friendly directory (e.g. ``synno-tpch``). *shm_capable* marks the
    engine as able to hot-load its tables from Arrow over shm. *bundle_parquet_dir* copies a
    parquet snapshot into the package (recorded as the relative ``parquet_dir="data"``), making
    a self-contained, standalone engine. *source_db* records the database the engine was built for.

    *receipt* is a :class:`ValidationReceipt` proving a live, cache-bypassed validation of this
    exact build. It is required: publish refuses (raising :class:`ReceiptRejected`, writing
    nothing) unless the receipt matches the engine on disk and covers the queries and scale factor
    being published. A ``shm_capable`` engine whose receipt did not validate the shm plane is
    downgraded to parquet-only rather than shipped with an unverified serving plane.
    """
    from .validation_receipt import (
        PLANE_PARQUET,
        PLANE_SHM,
        ReceiptRejected,
        verify_receipt_for_publish,
    )

    workspace = Path(workspace)
    target = resolve_engines_dir(engines_dir)
    if target is None:
        log.info(
            "publish: no engines directory configured (set SYNNO_ENGINES_DIR), skipping"
        )
        return None
    if not query_templates:
        log.info(
            "publish: no routable templates for the engine in %s, skipping", workspace
        )
        return None
    # Gate: refuse to publish anything the receipt does not prove. Raises ReceiptRejected (so the
    # caller's best-effort wrapper logs and ships nothing) on a mismatched/non-live/failed receipt.
    verify_receipt_for_publish(
        receipt,
        workspace=workspace,
        published_query_ids=[t.query_id for t in query_templates],
        scale_factor=scale_factor,
    )
    # Reconcile the served planes against what the receipt actually validated. Every plane the
    # engine will serve must have been validated.
    #
    # The parquet plane is the standalone/fallback serving plane (the router points a ProcessEngine
    # at it). If the engine ships a parquet snapshot but the receipt never validated parquet, there
    # is no safe fallback - refuse, since parquet cannot be "downgraded" away.
    serves_parquet = parquet_dir is not None or bundle_parquet_dir is not None
    if serves_parquet and not receipt.covers_plane(PLANE_PARQUET):
        raise ReceiptRejected(
            "publishing a parquet-serving engine but the receipt did not validate the parquet "
            f"plane (receipt covered {list(receipt.data_planes)}); refusing to ship an "
            "unvalidated serving plane"
        )
    # The shm hot-load plane is an optional optimization; never ship it on a receipt that only
    # exercised parquet. Downgrade to parquet-only (the engine still serves) instead of refusing.
    if shm_capable and not receipt.covers_plane(PLANE_SHM):
        log.warning(
            "publish: receipt covers planes %s but not the shm plane; publishing %s as "
            "parquet-only (shm hot-load withheld until the shm plane is validated)",
            list(receipt.data_planes),
            workspace,
        )
        shm_capable = False
    # A bundled snapshot is referenced by the portable relative path; otherwise record the
    # caller's path (or None for a pure shm engine).
    manifest_parquet_dir = (
        "data"
        if bundle_parquet_dir is not None
        else (str(parquet_dir) if parquet_dir is not None else None)
    )
    manifest = build_manifest_from_dir(
        workspace,
        query_templates,
        storage_mode=storage_mode,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        expected_tables={t: tuple(c) for t, c in (expected_tables or {}).items()},
        parquet_dir=manifest_parquet_dir,
        shm_capable=shm_capable,
        source_db=source_db,
        threads=threads,
        write=False,
    )
    dest = _atomic_publish(
        workspace, manifest, target, name=name, bundle_parquet_dir=bundle_parquet_dir
    )
    log.info(
        "published engine %s (%d queries, shm=%s, snapshot=%s) -> %s",
        manifest.engine_id,
        len(manifest.queries),
        shm_capable,
        bundle_parquet_dir is not None or parquet_dir is not None,
        dest,
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


def _sample_assignments(
    provider: object, query_id: str, n: int
) -> List[Mapping[str, object]]:
    """A few placeholder assignments for a query, from the workload's own generator (which
    needs no live data: TPC-H draws from fixed ranges, bring-your-own from user-supplied
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
    receipt: "ValidationReceipt",
    parquet_dir: "str | Path | None" = None,
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    storage_mode: str = "flat",
    engines_dir: "str | Path | None" = None,
    num_samples: int = 3,
    name: Optional[str] = None,
    shm_capable: bool = False,
    bundle_parquet_dir: "str | Path | None" = None,
    expected_tables: Optional[Mapping[str, Sequence]] = None,
    source_db: Optional[str] = None,
    threads: Optional[int] = None,
) -> Optional[Path]:
    """Publish the engine in *workspace*, taking query templates from *provider*'s workload.

    Pulls each query's ``[NAME]`` template from ``provider.sql_dict`` and a few sample
    assignments from the workload generator, then derives, validates, and publishes. Designed
    to be called best-effort at the end of a generation run. *receipt* (required) and *name* /
    *shm_capable* / *bundle_parquet_dir* / *expected_tables* / *source_db* are passed straight
    through to :func:`publish_engine` (see there); the receipt gates the publish.
    """
    sql_dict = getattr(provider, "sql_dict", {})
    templates_by_qid: dict[str, str] = {}
    assignments_by_qid: dict[str, List[Mapping[str, object]]] = {}
    for qid in query_ids:
        bracket = _lookup_template(sql_dict, str(qid))
        if not bracket:
            log.debug(
                "publish: no template for query %s in the workload, skipping", qid
            )
            continue
        # The manifest query id is the bare id the engine binary dispatches on ("1"), even
        # though the workload keys its templates as "Q1".
        templates_by_qid[str(qid)] = bracket
        assignments_by_qid[str(qid)] = _sample_assignments(
            provider, str(qid), num_samples
        )
    query_templates = build_query_templates(templates_by_qid, assignments_by_qid)
    return publish_engine(
        workspace,
        query_templates=query_templates,
        receipt=receipt,
        parquet_dir=parquet_dir,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        storage_mode=storage_mode,
        engines_dir=engines_dir,
        name=name,
        shm_capable=shm_capable,
        bundle_parquet_dir=bundle_parquet_dir,
        expected_tables=expected_tables,
        source_db=source_db,
        threads=threads,
    )
