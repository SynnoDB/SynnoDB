"""Workspace preparation as a set of orthogonal, independently switchable features.

A conversation states *what the workspace must have* (:class:`PrepareFeatures`),
not which pipeline stage it resembles. The interpreter
(:func:`apply_prepare_features`) maps enabled features onto the
``PrepareWorkspace`` primitives in one canonical order - scaffold, tracing,
mt_helpers - so the concatenated artifacts string (a cache-key input via
``framework_code_content``) is assembled exactly as the legacy per-stage
prepare functions assembled it.

The features applied to a workspace are recorded in a git-tracked metadata file
in the workspace root (``.synnodb_prepare.json``), so every snapshot carries its
own prepare record. When a run starts from a snapshot, the delta between the
recorded and the requested features decides per feature whether it is applied
fully (newly enabled: tracked files too) or only its untracked/read-only
support files are refreshed (already present). Features are additive along a
chain; disabling a recorded feature raises.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace

PREPARE_METADATA_FILENAME = ".synnodb_prepare.json"
_METADATA_FORMAT_VERSION = 1

# Feature fields recorded in the workspace metadata file. storage_plan_text is
# deliberately not recorded: the injected storage_plan.txt is itself a tracked
# workspace file.
_RECORDED_FEATURES = (
    "scaffold",
    "parallel_ready_impl",
    "tracing",
    "mt_helpers",
    "sample_trace",
)


@dataclass(frozen=True)
class PrepareFeatures:
    """What the prepared workspace must provide, feature by feature.

    - ``scaffold``: framework files, templates, queries.md, build files -
      ``"full"``, or ``"queries_md_only"`` for the storage-plan case.
    - ``parallel_ready_impl``: generate the query-impl scaffold in
      parallel-ready shape. ``"auto"`` resolves to True for in-memory storage.
    - ``tracing``: trace.hpp plus tracing/flush instrumentation.
    - ``mt_helpers``: thread_pool.hpp / query_pool.hpp helper wiring.
    - ``sample_trace``: a sample trace file in the workspace.
    - ``storage_plan_text``: inject this text as the storage plan file into the
      clean workspace (per-run input, not recorded in the metadata).
    """

    scaffold: Literal["full", "queries_md_only"] = "full"
    parallel_ready_impl: bool | Literal["auto"] = "auto"
    tracing: bool = False
    mt_helpers: bool = False
    sample_trace: bool = False
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
        return cls(parallel_ready_impl=True, tracing=True, mt_helpers=True)

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

    def resolve(self, in_memory_storage: bool) -> "PrepareFeatures":
        """Resolve ``parallel_ready_impl="auto"`` to its concrete value."""
        if self.parallel_ready_impl != "auto":
            return self
        return replace(self, parallel_ready_impl=in_memory_storage)


# ---------------------------- workspace metadata ------------------------------
def write_prepare_metadata(
    workspace_dir: Path, features: PrepareFeatures, parallelism: bool
) -> None:
    """Write the workspace's prepare record.

    Called by the interpreter only, after all features were applied.
    ``parallel_ready_impl`` must be resolved (no "auto") so the record states
    what actually happened. The serialization is deterministic (sorted keys,
    no timestamps, trailing newline): the file is git-tracked, so its content
    feeds the snapshot hash and thus every snapshot-keyed cache.
    """
    assert features.parallel_ready_impl != "auto", (
        "resolve parallel_ready_impl before recording the prepare metadata"
    )
    payload = {
        "features": {k: getattr(features, k) for k in _RECORDED_FEATURES},
        "format_version": _METADATA_FORMAT_VERSION,
        "parallelism": parallelism,
    }
    path = workspace_dir / PREPARE_METADATA_FILENAME
    # Unlink before writing: a hard-killed prior run can leave the file at mode
    # 0444 (see prepare_workspace._write_files), which would fail write_text.
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")


def read_prepare_metadata(workspace_dir: Path) -> tuple[PrepareFeatures, bool]:
    """Read the workspace's prepare record: ``(features, parallelism)``.

    Raises with a clear message when the file is absent - snapshots produced
    before the metadata file existed are not supported; re-run the producing
    stage to obtain a stamped snapshot.
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
    return features, bool(payload["parallelism"])


# -------------------------------- interpreter ---------------------------------
_SCAFFOLD_RANK = {"queries_md_only": 0, "full": 1}


def _check_additive(requested: PrepareFeatures, source: PrepareFeatures) -> None:
    """Features are additive along a chain: disabling one the source snapshot
    has is not supported and raises with a clear message."""
    downgrades = []
    if _SCAFFOLD_RANK[requested.scaffold] < _SCAFFOLD_RANK[source.scaffold]:
        downgrades.append(f"scaffold: {source.scaffold} -> {requested.scaffold}")
    for flag in ("parallel_ready_impl", "tracing", "mt_helpers", "sample_trace"):
        if getattr(source, flag) and not getattr(requested, flag):
            downgrades.append(f"{flag}: True -> False")
    if downgrades:
        raise ValueError(
            "Disabling a prepare feature the source snapshot has is not "
            "supported (features are additive along a chain): " + "; ".join(downgrades)
        )


def apply_prepare_features(
    features: PrepareFeatures,
    prepare_workspace_provider: "PrepareWorkspace",
    source_features: PrepareFeatures | None,
    *,
    do_not_cache: bool = True,
    only_from_cache: bool = False,
) -> str:
    """Apply the requested features to the workspace and return the artifacts string.

    ``source_features`` is the prepare record of the restored start snapshot
    (None for a fresh workspace). A newly enabled feature is applied fully,
    including tracked files; an already-present feature only refreshes its
    untracked/read-only support files. ``features.parallel_ready_impl`` must be
    resolved (no "auto").
    """
    assert features.parallel_ready_impl != "auto", (
        "resolve parallel_ready_impl before applying prepare features"
    )
    if source_features is not None:
        _check_additive(features, source_features)

    # scaffold - always requested (there is no scaffold-less workspace)
    usecase_args: dict = {
        "add_thread_pool_to_query_impl": features.parallel_ready_impl,
        "add_sample_trace": features.sample_trace,
    }
    if features.storage_plan_text is not None:
        usecase_args["storage_plan"] = features.storage_plan_text
    scaffold_present = (
        source_features is not None
        and _SCAFFOLD_RANK[source_features.scaffold]
        >= _SCAFFOLD_RANK[features.scaffold]
    )
    artifacts = prepare_workspace_provider.prepare(
        only_query_md=features.scaffold == "queries_md_only",
        write_non_tracked_only=scaffold_present,
        only_from_cache=only_from_cache,
        do_not_cache=do_not_cache,
        usecase_args=usecase_args,
    )

    if features.tracing:
        # Newly enabled: upgrade the snapshot's tracked query_impl.cpp too.
        # Already present: the snapshot carries the traced query_impl.cpp; only
        # the untracked/read-only support files need to be (re)written.
        tracing_present = source_features is not None and source_features.tracing
        artifacts += prepare_workspace_provider.prepare_optim(
            write_non_tracked_only=tracing_present,
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

    if features.mt_helpers:
        artifacts += prepare_workspace_provider.prepare_mt(
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

    return artifacts


def features_metadata_dict(features: PrepareFeatures) -> dict:
    """The recorded-features dict (mirrors the metadata file's "features" key)."""
    payload = asdict(features)
    return {k: payload[k] for k in _RECORDED_FEATURES}
