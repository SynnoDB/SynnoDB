"""Engine manifest: the serializable factory→runtime contract.

A ``manifest.json`` ships next to a generated engine and describes everything the
runtime needs to *bind* it — without importing the factory. The factory writes it
(Phase 0); the runtime reads it here and turns it into ``EngineBinding``s against a
live connection (recomputing the output schema and fingerprint from the user's own
DuckDB, the source of truth).

Schema (v1):

    {
      "schema_version": 1,
      "engine_id": "<content-addressed id>",
      "storage_mode": "flat" | "bespoke",
      "scale_factor": 10.0 | null,
      "source_run_id": "<wandb run or null>",
      "expected_tables": { "lineitem": [["l_orderkey","BIGINT"], ...], ... },
      "queries": [
        {"query_id": "1",
         "sql_template": "SELECT ... WHERE l_shipdate <= ?",
         "placeholders": [["DELTA","DATE"]]}
      ]
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..errors import SynnoError
from .registry import ColumnSpec, PlaceholderSpec

# v2 adds the optional ``parquet_dir`` (the data the engine ingested), so the runtime can
# bring up the engine with no extra inputs. v3 adds ``shm_capable`` (the binary can ingest
# its tables zero-copy from /dev/shm Arrow, the hot-load plane). v4 adds ``source_db`` (the
# database an optimize_database engine was built for). v5 adds ``threads`` (the degree of
# parallelism the engine was built/validated for; the runtime serves it at this thread count).
# Older manifests still load.
SCHEMA_VERSION = 6
_SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6)


def content_engine_id(source_files: Mapping[str, str], *, prefix: str = "eng") -> str:
    """A stable, content-addressed engine id from its generated source files.

    Deterministic in file name + content, independent of path/order/timestamps — so
    the same generated engine always gets the same id, and any source change yields a
    new id (cache- and registry-safe).
    """
    digest = hashlib.sha256()
    for name in sorted(source_files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_files[name].encode("utf-8"))
        digest.update(b"\0")
    return f"{prefix}-{digest.hexdigest()[:16]}"


@dataclass(frozen=True)
class QueryTemplate:
    query_id: str
    sql_template: str
    placeholders: Tuple[PlaceholderSpec, ...] = ()

    def to_dict(self) -> dict:
        # A placeholder embedded in a string literal (a LIKE affix, or one of several packed in
        # one literal) carries its constant prefix/suffix and group id; an ordinary whole-literal
        # placeholder omits them.
        def row(p: PlaceholderSpec) -> list:
            if p.prefix or p.suffix or p.group != -1:
                return [p.name, p.type, p.prefix, p.suffix, p.group]
            return [p.name, p.type]

        return {
            "query_id": self.query_id,
            "sql_template": self.sql_template,
            "placeholders": [row(p) for p in self.placeholders],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "QueryTemplate":
        return cls(
            query_id=str(d["query_id"]),
            sql_template=d["sql_template"],
            placeholders=tuple(
                PlaceholderSpec(*row) for row in d.get("placeholders", [])
            ),
        )


@dataclass(frozen=True)
class EngineManifest:
    engine_id: str
    queries: Tuple[QueryTemplate, ...]
    storage_mode: str = "flat"
    scale_factor: Optional[float] = None
    source_run_id: Optional[str] = None
    # Schema the engine was built against; used to verify a candidate DB is compatible.
    expected_tables: Mapping[str, Tuple[ColumnSpec, ...]] = field(default_factory=dict)
    # Path to the parquet the engine ingested - the self-contained standalone plane. May be
    # relative to the engine dir (a portable bundled snapshot) or absolute; the runtime
    # resolves it and points a ProcessEngine at it. None when the engine bundles no snapshot.
    parquet_dir: Optional[str] = None
    # Whether the binary can ingest its tables zero-copy from /dev/shm Arrow (the hot-load
    # plane, served over a live connection's in-memory data). False for older parquet engines.
    shm_capable: bool = False
    # The database this engine was built for (absolute path), when published by optimize_database.
    # Used to refuse silently clobbering the engine of a *different* database that happens to share
    # a friendly name (e.g. two ``tpch.db`` files in different directories -> both ``synno-tpch``).
    source_db: Optional[str] = None
    # The degree of parallelism the engine was generated, validated, and is served at (the
    # DuckDB ``config={'threads': N}``). The runtime sets the engine's CORE_IDS from this so it
    # runs at the same thread count it was built for. None = unknown (older engines): the runtime
    # leaves the thread count to the engine's own default.
    threads: Optional[int] = None
    # The language the engine was generated in ("cpp" | "rust"). The runtime does not
    # branch on it -- a published engine is a `db` binary behind the same protocol
    # whatever it was written in -- but the manifest is the engine's identity record
    # and must say what it is. Older engines predate the language axis and are C++.
    language: str = "cpp"

    # ---- serialization --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine_id": self.engine_id,
            "storage_mode": self.storage_mode,
            "scale_factor": self.scale_factor,
            "source_run_id": self.source_run_id,
            "parquet_dir": self.parquet_dir,
            "shm_capable": self.shm_capable,
            "source_db": self.source_db,
            "threads": self.threads,
            "language": self.language,
            "expected_tables": {
                table: [[c.name, c.type] for c in cols]
                for table, cols in self.expected_tables.items()
            },
            "queries": [q.to_dict() for q in self.queries],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EngineManifest":
        version = d.get("schema_version", 1)
        if version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported manifest schema_version {version!r} "
                f"(supported: {_SUPPORTED_SCHEMA_VERSIONS})"
            )
        return cls(
            engine_id=d["engine_id"],
            queries=tuple(QueryTemplate.from_dict(q) for q in d.get("queries", [])),
            storage_mode=d.get("storage_mode", "flat"),
            scale_factor=d.get("scale_factor"),
            source_run_id=d.get("source_run_id"),
            parquet_dir=d.get("parquet_dir"),
            shm_capable=bool(d.get("shm_capable", False)),
            source_db=d.get("source_db"),
            threads=d.get("threads"),
            # Engines published before the language axis carry no field and are C++.
            language=d.get("language", "cpp"),
            expected_tables={
                table: tuple(ColumnSpec(n, t) for n, t in cols)
                for table, cols in d.get("expected_tables", {}).items()
            },
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def write(self, path: "str | Path") -> Path:
        path = Path(path)
        if path.is_dir():
            path = path / "manifest.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: "str | Path") -> "EngineManifest":
        path = Path(path)
        if path.is_dir():
            path = path / "manifest.json"
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def engine_source_files(engine_dir: "str | Path") -> Dict[str, str]:
    """Read the generated C++ sources of an engine (``*.cpp`` / ``*.hpp``) by name."""
    engine_dir = Path(engine_dir)
    files: Dict[str, str] = {}
    for path in sorted(engine_dir.glob("*.cpp")) + sorted(engine_dir.glob("*.hpp")):
        try:
            files[path.name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return files


def build_manifest_from_dir(
    engine_dir: "str | Path",
    queries: Sequence[QueryTemplate],
    *,
    storage_mode: str = "flat",
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    expected_tables: Optional[Mapping[str, Sequence[ColumnSpec]]] = None,
    parquet_dir: Optional[str] = None,
    shm_capable: bool = False,
    source_db: Optional[str] = None,
    threads: Optional[int] = None,
    write: bool = True,
) -> EngineManifest:
    """Assemble (and optionally write) an :class:`EngineManifest` for a generated engine.

    ``engine_id`` is content-addressed from the engine's source files, so it is stable
    across rebuilds of identical code. This is the factory-side helper a stage calls
    after generating a base/optimized implementation; the runtime then reads the
    written ``manifest.json``.
    """
    engine_dir = Path(engine_dir)
    engine_id = content_engine_id(engine_source_files(engine_dir))
    manifest = EngineManifest(
        engine_id=engine_id,
        queries=tuple(queries),
        storage_mode=storage_mode,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        parquet_dir=str(parquet_dir) if parquet_dir is not None else None,
        shm_capable=shm_capable,
        source_db=source_db,
        threads=threads,
        expected_tables={t: tuple(cols) for t, cols in (expected_tables or {}).items()},
    )
    if write:
        manifest.write(engine_dir)
    return manifest


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")


def infer_duckdb_type(sample: Any) -> str:
    """Infer a DuckDB type string from a placeholder's sample value.

    Mirrors the factory's existing sample-typing (run_generated_code_service), so a
    manifest built from the generator's ``{name: sample_value}`` placeholders is typed
    consistently with how queries are validated.
    """
    if isinstance(sample, bool):
        return "BOOLEAN"
    if isinstance(sample, int):
        return "INTEGER"
    if isinstance(sample, float):
        return "DOUBLE"
    if isinstance(sample, str):
        if _DATE_RE.fullmatch(sample):
            return "DATE"
        if _INT_RE.fullmatch(sample):
            return "INTEGER"
        if _FLOAT_RE.fullmatch(sample):
            return "DOUBLE"
    return "VARCHAR"


def write_manifest_for_engine(
    engine_dir: "str | Path",
    query_metadata: Iterable[Tuple[str, str, Any]],
    *,
    storage_mode: str = "flat",
    scale_factor: Optional[float] = None,
    source_run_id: Optional[str] = None,
    expected_tables: Optional[Mapping[str, Sequence[ColumnSpec]]] = None,
    parquet_dir: Optional[str] = None,
    shm_capable: bool = False,
    threads: Optional[int] = None,
    write: bool = True,
) -> EngineManifest:
    """The factory-side writer: build & write ``manifest.json`` for a generated engine.

    ``query_metadata`` is an iterable of ``(query_id, sql_template, placeholders)``
    where ``placeholders`` is either the generator's ``{name: sample_value}`` dict
    (types are inferred) or an explicit sequence of :class:`PlaceholderSpec`.

    The factory calls this at base/optimized-impl finalization, e.g.::

        write_manifest_for_engine(workspace, [
            (qid, sql_template, placeholders)
            for qid, (sql_template, _, placeholders) in discovered.items()
        ], storage_mode=storage_mode, scale_factor=sf, source_run_id=run_id)
    """
    queries: List[QueryTemplate] = []
    for query_id, sql_template, placeholders in query_metadata:
        if isinstance(placeholders, Mapping):
            specs = tuple(
                PlaceholderSpec(name, infer_duckdb_type(val))
                for name, val in placeholders.items()
            )
        else:
            specs = tuple(placeholders)
        queries.append(QueryTemplate(str(query_id), sql_template, specs))
    return build_manifest_from_dir(
        engine_dir,
        queries,
        storage_mode=storage_mode,
        scale_factor=scale_factor,
        source_run_id=source_run_id,
        expected_tables=expected_tables,
        parquet_dir=parquet_dir,
        shm_capable=shm_capable,
        threads=threads,
        write=write,
    )


def check_compatibility(conn: Any, manifest: EngineManifest) -> List[str]:
    """Return a list of human-readable incompatibilities between *manifest* and the
    live schema on *conn* (empty list = compatible).

    An engine built for, say, TPC-H ``lineitem`` can only serve a DB whose
    ``lineitem`` has the same columns/types. This is the registration-time gate that
    keeps an engine off an incompatible database.
    """

    duck = getattr(conn, "duckdb", conn)
    problems: List[str] = []
    for table, expected in manifest.expected_tables.items():
        try:
            rows = duck.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE lower(table_name) = ? ORDER BY ordinal_position",
                [table.lower()],
            ).fetchall()
        except Exception as exc:  # pragma: no cover - defensive
            problems.append(f"{table}: schema introspection failed ({exc})")
            continue
        if not rows:
            problems.append(f"{table}: missing from the database")
            continue
        # Compare tolerantly: column names case-insensitively and types whitespace/case-normalized,
        # so cosmetic spelling drift ("DECIMAL(10, 2)" vs "DECIMAL(10,2)") between the build and the
        # live DB is not mistaken for a real schema change.
        live = tuple((str(name).lower(), _norm_type(dtype)) for name, dtype in rows)
        want = tuple((c.name.lower(), _norm_type(c.type)) for c in expected)
        if live != want:
            problems.append(f"{table}: schema differs from engine build")
    return problems


def _norm_type(t: Any) -> str:
    """A DuckDB type spelling normalized for comparison: upper-cased with all whitespace removed."""
    return "".join(str(t).upper().split())


def register_manifest(
    conn: Any, manifest: EngineManifest, engine: Any, *, strict: bool = True
) -> list:
    """Register every query in *manifest* against *conn*'s registry, bound to *engine*.

    Recomputes each query's output schema and table fingerprint from the live DuckDB
    (the source of truth). With ``strict`` (default), refuses to register if the live
    schema is incompatible with the engine's ``expected_tables``.
    """
    from .registration import make_binding  # local import: avoids cycle at import

    if strict and manifest.expected_tables:
        problems = check_compatibility(conn, manifest)
        if problems:
            # A typed error (not a bare ValueError) so discovery surfaces this at WARNING and stops
            # retrying it: by the time we register, the tables are present, so a schema mismatch is
            # a real, non-transient incompatibility the operator should see - not "data not loaded".
            raise SynnoError(
                "engine incompatible with database: " + "; ".join(problems)
            )

    registry = conn.router.registry
    bindings = []
    for query in manifest.queries:
        binding = make_binding(
            conn,
            template_sql=query.sql_template,
            engine=engine,
            query_id=query.query_id,
            engine_id=manifest.engine_id,
            placeholders=query.placeholders,
            scale_factor=manifest.scale_factor,
            storage_mode=manifest.storage_mode,
        )
        registry.register(binding)
        registry.clear_dirty(binding.tables)
        bindings.append(binding)
    return bindings
