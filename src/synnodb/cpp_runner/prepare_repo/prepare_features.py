"""Workspace preparation as a set of orthogonal, independently switchable features.

A conversation states *what the workspace must have* (:class:`PrepareFeatures`),
not which pipeline stage it resembles. Each feature maps to exactly one
builder on ``PrepareWorkspace``:

- ``scaffold`` + ``storage`` -> ``build_scaffold_files`` (framework files for
  the storage variant, queries.md, per-query files, ``query_impl.cpp``)
- ``parallel_ready_impl`` / ``tracing`` / ``sample_trace`` -> the
  ``query_impl.cpp`` assembly inside the scaffold (three flags shaping the
  same file)
- ``storage_plan_text`` -> ``build_storage_plan_files``
- ``tracing`` additionally triggers ``build_cleanup_deletes`` once, on the
  chain step that enables it (dropping the base-impl inputs)

The interpreter (:func:`apply_prepare_features`) invokes these in one
canonical order - scaffold (with storage plan), cleanup - so the concatenated
artifacts string (a cache-key input via ``framework_code_content``) is
deterministic.

The features applied to a workspace are recorded in a git-tracked metadata file
in the workspace root (``.synnodb_prepare.json``), so every snapshot carries its
own prepare record. When a run starts from a snapshot, the delta between the
recorded and the requested features decides per feature whether it is applied
fully (newly enabled: tracked files too) or only its untracked/read-only
support files are refreshed (already present). Features are additive along a
chain; disabling a recorded feature raises, and the storage variant must stay
the same along a chain.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from synnodb.utils.utils import DBStorage

if TYPE_CHECKING:
    from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace

PREPARE_METADATA_FILENAME = ".synnodb_prepare.json"
_METADATA_FORMAT_VERSION = 4


class Parallelism(str, Enum):
    """Whether the generated engine executes queries multi-threaded.

    Recorded in the workspace prepare metadata (and thus in every snapshot), so
    chained and replayed runs know what execution mode the engine was built
    for. The ``str`` mixin makes members JSON/W&B-serializable as their value;
    never rely on truthiness - both members are truthy.
    """

    SINGLE_THREADED = "single_threaded"
    MULTI_THREADED = "multi_threaded"


def _storage_variant(db_storage: DBStorage) -> Literal["in_memory", "ssd"]:
    """The scaffold variant a storage backend uses. LABSTORE shares the SSD
    templates (both are persistent, file-backed planes)."""
    return "in_memory" if db_storage == DBStorage.IN_MEMORY else "ssd"


@dataclass(frozen=True)
class PrepareFeatures:
    """What the prepared workspace must provide, feature by feature.

    - ``scaffold``: framework files, templates, queries.md, build files -
      ``"full"``, or ``"queries_md_only"`` for the storage-plan case.
    - ``storage``: which scaffold variant to write - ``"in_memory"`` or
      ``"ssd"`` (persistent, file-backed). ``"auto"`` resolves from the run's
      storage backend.
    - ``parallel_ready_impl``: generate the query-impl scaffold in
      parallel-ready shape. ``"auto"`` resolves to True for in-memory storage.
    - ``tracing``: query_impl.cpp assembled with tracing/flush instrumentation;
      enabling it along a chain also drops the base-impl inputs (plan file,
      todo list) from the workspace.
    - ``sample_trace``: emit a sample TRACE_COUNT per query (implies the trace
      wiring in query_impl.cpp, but is recorded separately).
    - ``flush_caches_after_each_run``: assemble query_impl.cpp so the engine
      drops the OS page cache and clears its buffer pool before each query run,
      for cold-cache measurements. SSD/persistent storage only - the in-memory
      plane has no buffer pool (:meth:`resolve` rejects it otherwise). Unlike the
      capability flags above it is orthogonal to the additive chain: later stages
      do not progressively enable it, so it is freely toggled per stage.
    - ``storage_plan_text``: inject this text as the storage plan file into the
      clean workspace (per-run input, not recorded in the metadata).
    """

    scaffold: Literal["full", "queries_md_only"] = "full"
    storage: Literal["in_memory", "ssd", "auto"] = "auto"
    parallel_ready_impl: bool | Literal["auto"] = "auto"
    tracing: bool = False
    sample_trace: bool = False
    flush_caches_after_each_run: bool = False
    storage_plan_text: str | None = None

    # ---- the built-in stages' feature sets ----
    @classmethod
    def storage_plan(cls) -> "PrepareFeatures":
        return cls(scaffold="queries_md_only", parallel_ready_impl=False)

    @classmethod
    def base(cls, storage_plan_text: str | None = None) -> "PrepareFeatures":
        return cls(storage_plan_text=storage_plan_text)

    @classmethod
    def optim(cls) -> "PrepareFeatures":
        return cls(tracing=True)

    @classmethod
    def mt(cls) -> "PrepareFeatures":
        return cls(parallel_ready_impl=True, tracing=True)

    # ---- (de)serialization ----
    def to_json(self) -> str:
        payload = {k: getattr(self, k) for k in _RECORDED_FEATURES}
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "PrepareFeatures":
        payload = json.loads(s)
        unknown = set(payload) - set(_RECORDED_FEATURES)
        if unknown:
            raise ValueError(f"Unknown prepare features in JSON: {sorted(unknown)}")
        return cls(**payload)

    def resolve(self, db_storage: DBStorage) -> "PrepareFeatures":
        """Resolve the ``"auto"`` values against the run's storage backend.

        ``storage="auto"`` becomes the backend's scaffold variant; an explicit
        ``storage`` (e.g. from a replayed prepare record) must match the
        backend, so a snapshot prepared for one variant cannot silently be
        chained into a run on the other.
        """
        storage = _storage_variant(db_storage)
        if self.storage != "auto" and self.storage != storage:
            raise ValueError(
                f"Prepare features request storage {self.storage!r}, but the "
                f"run's storage backend is {db_storage.value!r} "
                f"({storage!r} scaffold)."
            )
        if self.flush_caches_after_each_run and storage != "ssd":
            raise ValueError(
                "flush_caches_after_each_run drops the OS page cache and clears the "
                "engine buffer pool between query runs, both of which only exist on "
                f"the SSD/persistent plane; it cannot be used with {db_storage.value!r} "
                "(in-memory) storage."
            )
        parallel_ready_impl = self.parallel_ready_impl
        if parallel_ready_impl == "auto":
            parallel_ready_impl = storage == "in_memory"
        return replace(self, storage=storage, parallel_ready_impl=parallel_ready_impl)


# Feature fields recorded in the workspace metadata file. storage_plan_text is
# deliberately not recorded: the injected storage_plan.txt is itself a tracked
# workspace file.
_RECORDED_FEATURES = tuple(
    f.name for f in fields(PrepareFeatures) if f.name != "storage_plan_text"
)


def assert_resolved(features: PrepareFeatures, doing: str) -> None:
    assert features.storage != "auto" and features.parallel_ready_impl != "auto", (
        f"resolve the prepare features (storage / parallel_ready_impl) before {doing}"
    )


# ---------------------------- workspace metadata ------------------------------
def prepare_metadata_content(
    features: PrepareFeatures, parallelism: Parallelism
) -> str:
    """The workspace's prepare record as a deterministic JSON string.

    The features must be resolved (no "auto") so the record states what
    actually happened. The serialization is deterministic (sorted keys,
    no timestamps, trailing newline): the file is git-tracked, so its content
    feeds the snapshot hash and thus every snapshot-keyed cache. Computing the
    content without writing lets the caller content-address the prepared-state
    snapshot and reuse an existing one *before* touching the working tree, so a
    reload is not blocked by a freshly written, still-untracked record file.
    """
    assert_resolved(features, "recording the prepare metadata")
    payload = {
        "features": {k: getattr(features, k) for k in _RECORDED_FEATURES},
        "format_version": _METADATA_FORMAT_VERSION,
        "parallelism": parallelism.value,
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def write_prepare_metadata(
    workspace_dir: Path, features: PrepareFeatures, parallelism: Parallelism
) -> None:
    """Write the workspace's prepare record (see :func:`prepare_metadata_content`)."""
    content = prepare_metadata_content(features, parallelism)
    path = workspace_dir / PREPARE_METADATA_FILENAME
    # Unlink before writing: a hard-killed prior run can leave the file at mode
    # 0444 (see prepare_workspace._write_files), which would fail write_text.
    path.unlink(missing_ok=True)
    path.write_text(content)


def read_prepare_metadata(
    workspace_dir: Path,
) -> tuple[PrepareFeatures, Parallelism]:
    """Read the workspace's prepare record: ``(features, parallelism)``.

    Raises with a clear message when the file is absent or written in an older
    format - such snapshots cannot be chained from; re-run the producing stage
    to obtain a stamped snapshot.
    """
    path = workspace_dir / PREPARE_METADATA_FILENAME
    if not path.exists():
        raise ValueError(
            f"The workspace has no prepare record ({path}). Snapshots produced "
            "before prepare metadata was introduced cannot be chained from; "
            "re-run the producing stage to obtain a stamped snapshot."
        )
    payload = json.loads(path.read_text())
    version = payload.get("format_version")
    if version != _METADATA_FORMAT_VERSION:
        raise ValueError(
            f"Unsupported prepare-metadata format_version {version!r} in {path} "
            f"(expected {_METADATA_FORMAT_VERSION})."
        )
    features = PrepareFeatures(**payload["features"])
    return features, Parallelism(payload["parallelism"])


# -------------------------------- interpreter ---------------------------------
_SCAFFOLD_RANK = {"queries_md_only": 0, "full": 1}


def _check_additive(requested: PrepareFeatures, source: PrepareFeatures) -> None:
    """Features are additive along a chain: disabling one the source snapshot
    has is not supported and raises with a clear message. The storage variant
    is not additive at all - it must stay the same along a chain."""
    if requested.storage != source.storage:
        raise ValueError(
            "The storage variant cannot change along a chain: the source "
            f"snapshot was prepared for {source.storage!r}, the run requests "
            f"{requested.storage!r}."
        )
    downgrades = []
    if _SCAFFOLD_RANK[requested.scaffold] < _SCAFFOLD_RANK[source.scaffold]:
        downgrades.append(f"scaffold: {source.scaffold} -> {requested.scaffold}")
    for flag in ("parallel_ready_impl", "tracing", "sample_trace"):
        if getattr(source, flag) and not getattr(requested, flag):
            downgrades.append(f"{flag}: True -> False")
    if downgrades:
        raise ValueError(
            "Disabling a prepare feature the source snapshot has is not "
            "supported (features are additive along a chain): " + "; ".join(downgrades)
        )


def assemble_prepare_features(
    features: PrepareFeatures,
    prepare_workspace_provider: "PrepareWorkspace",
    source_features: PrepareFeatures | None,
) -> tuple[str, tuple[object, ...]]:
    """Assemble the requested feature files and return ``(artifacts, parts)``.

    This does not write to the workspace. The caller can use the artifact string
    and metadata record to check the prepared-snapshot cache first, then either
    restore that snapshot or write the assembled parts and create it.

    ``source_features`` is the prepare record of the restored start snapshot
    (None for a fresh workspace). A newly enabled feature is applied fully,
    including tracked files; an already-present feature only refreshes its
    untracked/read-only support files. ``features`` must be resolved (no
    "auto").
    """
    assert_resolved(features, "applying prepare features")
    if source_features is not None:
        _check_additive(features, source_features)

    # scaffold - always requested (there is no scaffold-less workspace). The
    # untracked/read-only files (query_impl.cpp included) are rewritten from
    # the current features on every prepare, so the query-impl flags
    # (parallel_ready_impl / tracing / sample_trace) take effect regardless of
    # what the snapshot was prepared with.
    scaffold_present = (
        source_features is not None
        and _SCAFFOLD_RANK[source_features.scaffold]
        >= _SCAFFOLD_RANK[features.scaffold]
    )
    parts = [
        prepare_workspace_provider.assemble(
            features,
            write_non_tracked_only=scaffold_present,
        )
    ]

    # Enabling tracing marks the chain step that moves past the base impl:
    # drop its inputs (plan file, todo list) from the workspace exactly once.
    tracing_present = source_features is not None and source_features.tracing
    if features.tracing and not tracing_present:
        parts.append(prepare_workspace_provider.assemble_cleanup())

    return "".join(part.artifacts_str for part in parts), tuple(parts)


def apply_prepare_features(
    features: PrepareFeatures,
    prepare_workspace_provider: "PrepareWorkspace",
    source_features: PrepareFeatures | None,
) -> str:
    """Apply the requested features to the workspace and return the artifacts string.

    This is the write-through wrapper around
    :func:`assemble_prepare_features`. The prepared-snapshot cache path uses the
    assembly function directly so it can check the cache before touching tracked
    workspace files.
    """
    artifacts, parts = assemble_prepare_features(
        features,
        prepare_workspace_provider,
        source_features,
    )

    for part in parts:
        prepare_workspace_provider.write_prepared_files(part)

    return artifacts


def features_metadata_dict(features: PrepareFeatures) -> dict:
    """The recorded-features dict (mirrors the metadata file's "features" key)."""
    payload = asdict(features)
    return {k: payload[k] for k in _RECORDED_FEATURES}
