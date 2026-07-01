"""The publish gate's structured proof that an engine was validated live.

The incident this guards against: a generation run published an engine whose every query
validated ``incorrect`` (the box had OOM'd, so the binary no longer loaded), because the only
publish gate was "does any query yield a routable template" - a check derived from workload SQL
that never runs the binary. A plain validation call could also be answered from the pickled
validation cache and bless a now-broken engine with an earlier cached success.

The fix makes broken publishing impossible *by API shape*: :func:`publish_engine` /
:func:`publish_from_provider` require a :class:`ValidationReceipt`, and refuse to write anything
unless it proves a cache-bypassed live execution of the very build being published. The receipt
records what was executed (snapshot + per-artifact build-ids), what was validated (the concrete
parameter bindings, stated coverage policy, data planes, dataset/scale factors), and the verdict;
:func:`verify_receipt_for_publish` re-derives the on-disk build-ids and rejects any mismatch, so a
validate-one-build / publish-another mismatch cannot slip through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

PASS = "pass"
FAIL = "fail"

# Data planes a published engine can serve. "parquet" is the standalone snapshot plane;
# "shm" is the zero-copy /dev/shm Arrow hot-load plane (the serving-path optimization).
PLANE_PARQUET = "parquet"
PLANE_SHM = "shm"


class ReceiptRejected(Exception):
    """The publish gate refused a receipt. Raising (rather than returning) keeps the failure
    impossible to ignore: a caller that does not handle it does not publish."""


@dataclass(frozen=True)
class ValidatedQuery:
    """A query proven correct for the concrete parameter bindings listed.

    ``bindings`` is the actual placeholder assignments that were executed, not just the query id:
    "Q1 passed for these bindings" is a stronger, auditable claim than "Q1 passed". An empty
    ``bindings`` tuple means a constant query (no placeholders) validated as itself.
    """

    query_id: str
    bindings: Tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class ValidationReceipt:
    """Proof that a specific engine build was validated by a live, cache-bypassed run.

    Every field is an honest record of what the producing validation actually did; the publish
    gate verifies the verifiable ones (build-ids, query coverage, scale factor, live flag,
    verdict, plane coverage) against the engine being published.
    """

    # --- identity of the exact artifact that was executed ---
    # The git snapshot the validator ran against, when a snapshotter was in play (the main
    # generation path). None for paths without one (the optimizer cross-check); build_ids are the
    # load-bearing identity check in that case.
    snapshot_id: Optional[str]
    # {workspace-relative path -> NT_GNU_BUILD_ID} of the db binary and every build/*.so executed.
    # The publish gate re-reads these from disk and refuses on any difference, so the thing
    # published is provably the thing validated.
    build_ids: Mapping[str, str]

    # --- what was validated ---
    validated_queries: Tuple[ValidatedQuery, ...]
    # How the bindings were chosen, stated honestly. The receipt proves the listed bindings, NOT
    # every possible value of a template; this records the sampling policy so it cannot overclaim.
    coverage_policy: str
    # Data plane(s) the validation actually exercised (PLANE_PARQUET / PLANE_SHM). A published
    # serving plane that is not in here was never validated and must not ride along.
    data_planes: Tuple[str, ...]
    dataset: str
    # Scale factors the live run covered; the gate refuses to publish at a scale factor absent here.
    validated_scale_factors: Tuple[float, ...] = ()

    # --- how it ran, and the result ---
    mode: str = ""
    # True only for a cache-bypassed live execution. The gate refuses a receipt that is not live,
    # because a cached verdict can bless a since-broken build.
    live_run: bool = False
    verdict: str = FAIL

    def covers_plane(self, plane: str) -> bool:
        return plane in self.data_planes

    def to_dict(self) -> Dict[str, object]:
        """A JSON-friendly view, for logging and diagnostics."""
        return {
            "snapshot_id": self.snapshot_id,
            "build_ids": dict(self.build_ids),
            "validated_queries": [
                {"query_id": vq.query_id, "bindings": [dict(b) for b in vq.bindings]}
                for vq in self.validated_queries
            ],
            "coverage_policy": self.coverage_policy,
            "data_planes": list(self.data_planes),
            "dataset": self.dataset,
            "validated_scale_factors": list(self.validated_scale_factors),
            "mode": self.mode,
            "live_run": self.live_run,
            "verdict": self.verdict,
        }


def engine_build_ids(workspace: "str | Path") -> Dict[str, str]:
    """The build-ids of the executable artifacts in *workspace*: the ``db`` binary and every
    ``build/*.so`` plugin that has an NT_GNU_BUILD_ID. Keyed by workspace-relative path so the
    same dict is reproducible at validation time and at publish time. Artifacts without a build-id
    (e.g. a stripped binary) are omitted; the comparison is set-and-value equality of what remains.

    A real engine always carries build-ids: the plugins are linked with an explicit
    ``-Wl,--build-id=sha1`` and the ``db`` binary with the same flag, so this is non-empty (and the
    publish gate's identity check non-vacuous) for any genuinely compiled workspace. An empty result
    means a non-engine workspace (e.g. a synthetic test fixture whose ``db`` is a stand-in file).
    """
    from synnodb.cpp_runner.hotpatch.elf_build_id import read_build_id

    workspace = Path(workspace)
    out: Dict[str, str] = {}
    candidates = []
    db = workspace / "db"
    if db.exists():
        candidates.append(db)
    build = workspace / "build"
    if build.is_dir():
        candidates.extend(sorted(build.glob("*.so")))
    for path in candidates:
        build_id = read_build_id(str(path))
        if build_id:
            out[path.relative_to(workspace).as_posix()] = build_id
    return out


def _sf_equal(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 1e-9


def verify_receipt_for_publish(
    receipt: ValidationReceipt,
    *,
    workspace: "str | Path",
    published_query_ids: Sequence[str],
    scale_factor: Optional[float],
) -> None:
    """Refuse the publish unless the receipt proves a live validation of this exact build over the
    queries being published. Raises :class:`ReceiptRejected` (writing nothing) on any failure:

    - not a receipt, or not from a live run, or a non-pass verdict;
    - the engine on disk carries no build-id (an unidentifiable build);
    - the on-disk build-ids differ from the validated ones (a different build than was validated);
    - a published query the receipt does not cover;
    - a publish scale factor the live run did not validate.

    This enforces the *falsifiable* invariants - the ones that, mismatched, mean "published something
    other than what was validated." The receipt's ``dataset``, ``coverage_policy``, and per-query
    ``bindings`` are recorded for audit (so a published engine's proof is inspectable), not
    re-derived here: the build-id identity is what binds the proof to the artifact, and the query +
    scale-factor coverage is what binds it to the routed surface. Plane reconciliation (refusing an
    unvalidated *serving* plane) is the caller's, via :meth:`ValidationReceipt.covers_plane`, because
    the caller decides refuse-vs-downgrade.
    """
    if not isinstance(receipt, ValidationReceipt):
        raise ReceiptRejected(
            f"publish requires a ValidationReceipt, got {type(receipt).__name__}"
        )
    if not receipt.live_run:
        raise ReceiptRejected(
            "receipt is not from a live run (live_run is False); refusing to publish on a "
            "possibly-cached verdict"
        )
    if receipt.verdict != PASS:
        raise ReceiptRejected(
            f"validation verdict is {receipt.verdict!r}, not {PASS!r}; refusing to publish a "
            "build that did not pass live validation"
        )
    on_disk = engine_build_ids(workspace)
    if not on_disk:
        # No build-id on any artifact means the executable cannot be tied to what was validated;
        # an empty-equals-empty match would make the identity check vacuous. Refuse rather than
        # publish a build we cannot identify. (A real engine always carries plugin + db build-ids.)
        raise ReceiptRejected(
            "no build-id could be read from the engine on disk; refusing to publish a build that "
            "cannot be identified against the validated one"
        )
    if dict(receipt.build_ids) != on_disk:
        raise ReceiptRejected(
            "the engine on disk does not match the validated build-ids - refusing to publish a "
            f"different build than was validated (validated {sorted(receipt.build_ids)}, "
            f"on disk {sorted(on_disk)})"
        )
    validated = {vq.query_id for vq in receipt.validated_queries}
    missing = [str(q) for q in published_query_ids if str(q) not in validated]
    if missing:
        raise ReceiptRejected(
            f"the receipt does not cover published queries {missing}; refusing to publish "
            "unvalidated queries"
        )
    if scale_factor is not None:
        # If publish targets a scale factor, the receipt must contain it. An empty
        # validated_scale_factors is not a wildcard - it means nothing was proven at this SF.
        if not any(_sf_equal(scale_factor, s) for s in receipt.validated_scale_factors):
            raise ReceiptRejected(
                f"publish scale factor {scale_factor} was not validated "
                f"(validated {list(receipt.validated_scale_factors)})"
            )
