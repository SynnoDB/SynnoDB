"""cgroup v2 memory ceilings for the SynnoDB engine process.

Two nested ceilings bound memory:

* **Per-runner** - a runner's ``./db`` process and its whole stage tree are placed in
  a dedicated cgroup with ``memory.max`` set, so no single generated engine can exceed
  a configured resident cap: a breach is OOM-killed as a group
  (``memory.oom.group=1``), affecting only that runner.
* **Aggregate** - those per-runner cgroups nest under a shared parent slice that itself
  carries a host-wide ``memory.max``. The kernel enforces the sum across *every* runner
  and *every* orchestrator under the slice, so concurrent runners cannot collectively
  take down the host. The parent does **not** set ``memory.oom.group=1``, so an
  aggregate breach kills exactly one runner (the victim's nearest ``oom.group`` ancestor
  is its own child cgroup) - single-victim by default, kill-all only if an operator
  deliberately sets ``oom.group=1`` on the slice.

``RLIMIT_AS`` (virtual address space) is kept separately as a cheap fast-fail; this
module provides the authoritative RSS ceilings.

Parent selection:

* If ``SYNNO_CGROUP_PARENT`` names a slice, runner cgroups nest directly under it. The
  slice must already distribute ``memory`` and hold no processes of its own (each
  orchestrator runs in its own leaf under the slice). ``SYNNO_CGROUP_PARENT_MAX`` sets
  the slice budget when the operator has not (e.g. systemd ``MemoryMax=``). A shared
  parent that is configured but unusable makes :func:`_prepare_runner_parent` raise
  rather than silently fall back to a per-orchestrator cgroup, so the aggregate
  guarantee is never quietly lost.
* Otherwise runner cgroups nest under the orchestrator's own delegated cgroup. Because
  cgroup v2 forbids a cgroup from both holding processes and distributing a controller
  to its children ("no internal processes"), the orchestrator's own processes are first
  moved into a ``synno-leader`` leaf so the delegated cgroup can hand ``memory`` to
  sibling runner cgroups - the standard leader pattern.

Both require cgroup v2 with the ``memory`` controller delegated (e.g. a systemd unit
with ``Delegate=yes`` under a ``synnodb.slice``); see ``docs/cgroup_memory_safety.md``.
When delegation is unavailable, :func:`delegation_available` returns ``False`` and
:meth:`RunnerCgroup.create` raises :class:`CgroupUnavailable`; the caller decides
whether to fail closed (production) or fall back to ``RLIMIT_AS`` only (dev/test).
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
_LEADER = "synno-leader"

# Path of a shared, machine-level parent slice under which runner cgroups nest so the
# kernel enforces one aggregate memory budget across all orchestrators. Absolute, or
# relative to CGROUP_ROOT. When set, it is the only acceptable parent (no fallback to a
# per-orchestrator cgroup).
_ENV_PARENT = "SYNNO_CGROUP_PARENT"
# Optional aggregate budget (bytes, or with a K/M/G/T suffix) written to the shared
# parent's memory.max when the operator has not set one on the slice already.
_ENV_PARENT_MAX = "SYNNO_CGROUP_PARENT_MAX"

# Cached parent cgroup under which runner cgroups are created. Set once the leader
# pattern has been established (or confirmed already in place). This assumes the
# orchestrator's own cgroup is immutable for its lifetime; a manual mid-life move of
# the orchestrator to a different cgroup would not be reflected here.
_runner_parent: Optional[Path] = None

# Memoized result of delegation_available(): capability is effectively process-static
# (delegation does not appear or vanish mid-life), so the probe runs at most once.
_delegation: Optional[bool] = None

# Reason the last delegation probe failed, for diagnostics when failing closed. None
# when delegation is available or the probe has not run.
_delegation_error: Optional[str] = None

# Signature (SYNNO_CGROUP_PARENT, SYNNO_CGROUP_PARENT_MAX) captured the first time the
# caches above were populated. The cgroup parent config is process-static; if it changes
# afterwards we refuse rather than serve a cached parent / delegation result resolved from
# a different config (which could nest runners outside the intended aggregate slice).
_cgroup_env_sig: Optional[tuple] = None

# Monotonic nonce so concurrent probe cgroups never collide on their directory name.
_probe_counter = 0


class CgroupUnavailable(RuntimeError):
    """Raised when a memory-capped cgroup cannot be created (no v2 delegation)."""


def _self_cgroup_dir() -> Optional[Path]:
    """The current process's cgroup v2 directory, or ``None`` if not on v2.

    ``/proc/self/cgroup`` on a pure cgroup v2 host has a single line ``0::/<path>``.
    """
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, _, rel = line.split(":", 2)
            if hierarchy == "0":
                return CGROUP_ROOT / rel.lstrip("/")
    except OSError:
        return None
    return None


def _cgroup_v2() -> bool:
    return (CGROUP_ROOT / "cgroup.controllers").exists()


def _read_list(path: Path) -> List[str]:
    return path.read_text().split()


def _live_procs(cg: Path) -> List[str]:
    try:
        return [p for p in (cg / "cgroup.procs").read_text().split() if p]
    except OSError:
        return []


# A byte size is ASCII digits with an optional single binary-unit suffix. Matched
# strictly (no underscores, signs, Unicode digits or internal spaces) so a typo can
# never silently parse to a different, smaller value and disable the aggregate cap.
_BYTE_SIZE_RE = re.compile(r"([0-9]+)([KMGT]?)", re.IGNORECASE)
_BYTE_SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def _parse_byte_size(text: str) -> int:
    """Parse a byte size like ``500000000000`` or ``480G`` into a positive int.

    Accepts an optional single binary-unit suffix (K/M/G/T, case-insensitive, 1024-
    based). Raises :class:`ValueError` on anything malformed (validation, not an
    ``assert``, so it holds under ``python -O`` too).
    """
    m = _BYTE_SIZE_RE.fullmatch(text.strip())
    if m is None:
        raise ValueError(f"invalid byte size: {text!r}")
    value = int(m.group(1)) * _BYTE_SIZE_UNITS[m.group(2).upper()]
    if value <= 0:
        raise ValueError(f"byte size must be positive: {text!r}")
    return value


def _read_memory_max(cg: Path) -> Optional[int]:
    """The cgroup's ``memory.max`` in bytes, or ``None`` if unbounded ("max")."""
    raw = (cg / "memory.max").read_text().strip()
    return None if raw == "max" else int(raw)


def _shared_parent_path(raw: str) -> Path:
    """Resolve the configured shared-parent path and confirm it sits under the cgroup
    v2 mount (and is not the root cgroup, which cannot carry a ``memory.max``).

    The path is normalised first so a ``..`` traversal cannot escape the mount and have
    runner cgroups created somewhere outside it.
    """
    p = Path(raw)
    parent = Path(os.path.normpath(p if p.is_absolute() else CGROUP_ROOT / raw))
    if parent == CGROUP_ROOT or CGROUP_ROOT not in parent.parents:
        raise CgroupUnavailable(
            f"{_ENV_PARENT}={raw!r} must name a cgroup under {CGROUP_ROOT}, not the root"
        )
    return parent


def _configure_parent_budget(parent: Path) -> None:
    """Ensure the shared parent carries a finite ``memory.max`` aggregate budget.

    If ``SYNNO_CGROUP_PARENT_MAX`` is set, write it; otherwise rely on a budget the
    operator already set on the slice (e.g. systemd ``MemoryMax=``). An unbounded parent
    provides no aggregate protection, so refuse it (fail closed) - the whole point of a
    shared parent is the aggregate ceiling.
    """
    requested = os.environ.get(_ENV_PARENT_MAX, "").strip()
    if requested:
        want = _parse_byte_size(requested)
        current = _read_memory_max(parent)
        # Idempotent: only write when the value actually changes, so re-running (e.g. the
        # delegation probe, or a second orchestrator) does not needlessly rewrite a
        # shared slice, and a re-budget of an existing finite limit is logged, not silent.
        if current != want:
            try:
                (parent / "memory.max").write_text(str(want))
            except OSError as exc:
                # The operator named an authoritative budget we could not apply. Only
                # tolerate it when the existing limit is already at least as tight as
                # requested (running under a stricter cap is safe). An unbounded or
                # looser existing limit would silently weaken the requested ceiling, so
                # refuse rather than run under a weaker cap than asked for.
                if current is None or current > want:
                    raise CgroupUnavailable(
                        f"cannot set {parent}/memory.max to {want} and the existing limit "
                        f"({'unbounded' if current is None else current}) is weaker than "
                        f"requested; refusing to launch under a looser aggregate cap: {exc}"
                    ) from exc
                logger.warning(
                    "could not set %s/memory.max to %d (%s); existing limit %d is already "
                    "tighter, keeping it",
                    parent,
                    want,
                    exc,
                    current,
                )
            else:
                if current is not None:
                    logger.info(
                        "changed shared parent %s memory.max %d -> %d",
                        parent,
                        current,
                        want,
                    )
    if _read_memory_max(parent) is None:
        raise CgroupUnavailable(
            f"shared parent {parent} has no memory.max budget, so the aggregate ceiling "
            f"is unenforced. Set {_ENV_PARENT_MAX} or a slice MemoryMax=."
        )


def _warn_if_parent_kills_all(parent: Path) -> None:
    """Single-victim is the default: an aggregate breach kills one runner because each
    child sets ``oom.group=1`` and the parent does not. If an operator has deliberately
    set ``oom.group=1`` on the parent, a breach kills *every* runner; surface that."""
    try:
        if (parent / "memory.oom.group").read_text().strip() == "1":
            logger.warning(
                "shared parent %s has memory.oom.group=1: an aggregate breach will kill "
                "ALL runners under it (kill-all), not a single victim",
                parent,
            )
    except OSError:
        pass


def _prepare_shared_parent(parent: Path) -> Path:
    """Validate and configure a shared, machine-level parent slice under which runner
    cgroups from every orchestrator nest, so the kernel enforces one aggregate
    ``memory.max`` across them all.

    Unlike the per-orchestrator leader bootstrap, this never moves processes: the slice
    must already hold none of its own (each orchestrator runs in its own leaf under it).
    Raises :class:`CgroupUnavailable` on any setup problem so the caller fails closed.
    """
    if not _cgroup_v2():
        raise CgroupUnavailable("not running under cgroup v2")
    if not parent.exists():
        raise CgroupUnavailable(f"shared parent cgroup {parent} does not exist")
    try:
        controllers = _read_list(parent / "cgroup.controllers")
    except OSError as exc:
        raise CgroupUnavailable(
            f"cannot read {parent}/cgroup.controllers: {exc}"
        ) from exc
    if "memory" not in controllers:
        raise CgroupUnavailable(
            f"memory controller not available under shared parent {parent}; its parent "
            f"must list memory in cgroup.subtree_control"
        )
    if _live_procs(parent):
        raise CgroupUnavailable(
            f"shared parent {parent} holds processes directly; it must only distribute "
            f"memory to child runner cgroups. Run each orchestrator in its own leaf "
            f"cgroup under the slice, not in the slice itself."
        )
    if "memory" not in _read_list(parent / "cgroup.subtree_control"):
        try:
            (parent / "cgroup.subtree_control").write_text("+memory")
        except OSError as exc:
            raise CgroupUnavailable(
                f"cannot enable +memory in {parent}/cgroup.subtree_control: {exc}"
            ) from exc
    _configure_parent_budget(parent)
    _warn_if_parent_kills_all(parent)
    logger.info("using shared parent cgroup %s for runner cgroups", parent)
    return parent


def _cgroup_env_signature() -> tuple:
    """The env that determines which parent runner cgroups nest under."""
    return (
        os.environ.get(_ENV_PARENT, "").strip(),
        os.environ.get(_ENV_PARENT_MAX, "").strip(),
    )


def _guard_env_immutable() -> None:
    """Capture the cgroup parent config on first use and refuse if it later changes.

    ``_runner_parent`` / ``_delegation`` are process-global caches that assume the parent
    config is process-static (set once at startup, e.g. systemd ``Environment=``). If the
    config changed afterwards, a cached parent or delegation result resolved from the
    *old* config could nest runners outside the intended aggregate slice. That must never
    happen silently, so fail closed instead.
    """
    global _cgroup_env_sig
    sig = _cgroup_env_signature()
    if _cgroup_env_sig is None:
        _cgroup_env_sig = sig
    elif sig != _cgroup_env_sig:
        raise CgroupUnavailable(
            f"cgroup parent config changed after first use and must be process-static: "
            f"{_ENV_PARENT}/{_ENV_PARENT_MAX} was {_cgroup_env_sig!r}, now {sig!r}"
        )


def _prepare_runner_parent() -> Path:
    """Return the cgroup under which runner cgroups are created.

    With ``SYNNO_CGROUP_PARENT`` set, that is a shared machine-level slice (one
    aggregate budget across all orchestrators); otherwise it is the orchestrator's own
    delegated cgroup, bootstrapping the leader pattern if needed.

    Idempotent and cached. Raises :class:`CgroupUnavailable` if no usable parent exists.
    When a shared parent is configured but unusable, this raises rather than silently
    nesting under the orchestrator's own cgroup, so the aggregate guarantee is never
    quietly lost.
    """
    global _runner_parent
    _guard_env_immutable()
    if _runner_parent is not None:
        return _runner_parent

    configured = os.environ.get(_ENV_PARENT, "").strip()
    if configured:
        _runner_parent = _prepare_shared_parent(_shared_parent_path(configured))
        return _runner_parent

    base = _self_cgroup_dir()
    if base is None or not _cgroup_v2():
        raise CgroupUnavailable("not running under cgroup v2")
    if "memory" not in _read_list(base / "cgroup.controllers"):
        raise CgroupUnavailable(f"memory controller not delegated under {base}")

    # If the delegated cgroup already distributes memory and holds no processes of
    # its own, it is ready to parent runner cgroups directly.
    if "memory" in _read_list(base / "cgroup.subtree_control") and not _live_procs(
        base
    ):
        _runner_parent = base
        return base

    # Otherwise establish the leader pattern: move our processes into a leaf so the
    # delegated cgroup can hand `memory` to sibling runner cgroups.
    leader = base / _LEADER
    try:
        leader.mkdir(exist_ok=True)
        for pid in _live_procs(base):
            # Moving a pid (tgid) relocates the whole process; self-move is allowed.
            (leader / "cgroup.procs").write_text(pid)
        if "memory" not in _read_list(base / "cgroup.subtree_control"):
            (base / "cgroup.subtree_control").write_text("+memory")
    except OSError as exc:
        # EACCES (not delegated) or EBUSY (could not vacate internal processes).
        raise CgroupUnavailable(
            f"cannot establish runner parent under {base}: {exc}"
        ) from exc

    _runner_parent = base
    return base


# A runner cgroup is only swept once it is at least this old, so a concurrent orchestrator that
# just created its cgroup but has not joined it yet (cgroup.procs briefly empty) is never removed.
_STALE_CGROUP_MIN_AGE_S = 60.0


def _sweep_stale_runner_cgroups(parent: Path) -> None:
    """Remove abandoned per-runner cgroups left under *parent* by an orchestrator that died without
    running its cleanup.

    On a graceful exit the pool's atexit teardown calls ``terminate()``, which removes the runner
    cgroup. But atexit does not run on SIGTERM, SIGKILL, or a hard crash, so a runner cgroup can be
    left behind. The engine process itself is already gone in those cases (PR_SET_PDEATHSIG SIGKILLs
    the whole tree when the orchestrator dies), so the leftover is an empty cgroup directory that
    would otherwise accumulate under the slice. Sweeping at launch self-heals every death mode,
    unlike a SIGTERM-only signal handler.

    Only a cgroup with no live processes (empty ``cgroup.procs``) and older than
    ``_STALE_CGROUP_MIN_AGE_S`` is removed - the age guard avoids racing a concurrent orchestrator
    mid-launch. The ``synno-runner-`` prefix scopes the sweep to per-runner cgroups only, so it
    never touches the sibling ``synno-leader`` (which holds a live orchestrator process) or any
    shared-parent structure. Best-effort: any error on an individual entry is skipped, never raised.
    """
    now = time.time()
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for child in entries:
        if not child.name.startswith("synno-runner-"):
            continue  # only per-runner cgroups are swept (never synno-leader / shared-parent)
        try:
            if not child.is_dir():
                continue
            if now - child.stat().st_mtime < _STALE_CGROUP_MIN_AGE_S:
                continue  # too fresh: a concurrent launcher may be about to join it
            if (child / "cgroup.procs").read_text().split():
                continue  # still holds live processes; not abandoned
            child.rmdir()
            logger.debug("swept abandoned runner cgroup %s", child)
        except OSError:
            continue  # racing removal, permissions, or a non-cgroup dir; leave it


class RunnerCgroup:
    """A per-runner cgroup v2 cgroup holding one engine process tree."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def create(
        cls,
        memory_max_bytes: int,
        *,
        name: str,
        oom_group: bool = True,
    ) -> "RunnerCgroup":
        """Create a runner cgroup with a hard ``memory.max`` and return it.

        Sets ``memory.swap.max=0`` (a breach must OOM, not swap) and, by default,
        ``memory.oom.group=1`` so the whole engine tree is killed as one unit rather
        than one unlucky stage surviving. Raises :class:`CgroupUnavailable` on any
        failure, leaving no partial cgroup behind.
        """
        parent = _prepare_runner_parent()
        # Reclaim cgroups abandoned by a previously-crashed/SIGTERM'd orchestrator before adding a
        # new one, so empty runner cgroups do not accumulate under the slice.
        _sweep_stale_runner_cgroups(parent)
        safe = (
            "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "runner"
        )
        # The "synno-runner-" prefix is dedicated to per-runner cgroups so the stale-cgroup sweep
        # can target them exactly, never the sibling "synno-leader" (which holds a live orchestrator
        # process) or any shared-parent structure.
        child = parent / f"synno-runner-{safe}"
        # The runner cgroup must be a direct child of the resolved parent: that is the
        # cgroup the per-runner oom.group=1 is set on (single-victim), and what nests
        # the runner under the shared parent's aggregate budget.
        assert child.parent == parent, (child, parent)
        try:
            child.mkdir(exist_ok=False)
        except OSError as exc:
            raise CgroupUnavailable(f"cannot create cgroup {child}: {exc}") from exc

        try:
            # Order matters: swap off and oom.group set before memory.max, so the cap
            # is never briefly enforced with swap available or a partial-kill OOM.
            (child / "memory.swap.max").write_text("0")
            if oom_group:
                (child / "memory.oom.group").write_text("1")
            (child / "memory.max").write_text(str(int(memory_max_bytes)))
        except OSError as exc:
            try:
                child.rmdir()
            except OSError:
                pass
            raise CgroupUnavailable(f"cannot configure cgroup {child}: {exc}") from exc
        logger.debug(
            "created runner cgroup %s (memory.max=%d)", child, memory_max_bytes
        )
        return cls(child)

    @property
    def procs_dir(self) -> str:
        """The directory a launcher writes its pid into (``<dir>/cgroup.procs``)."""
        return str(self.path)

    def memory_events(self) -> Dict[str, int]:
        """Parse ``memory.events`` (e.g. ``oom_kill``, ``oom_group_kill``, ``max``).

        Returns an empty dict if the file is gone (cgroup already removed)."""
        try:
            text = (self.path / "memory.events").read_text()
        except OSError:
            return {}
        out: Dict[str, int] = {}
        for line in text.splitlines():
            key, _, val = line.partition(" ")
            if val.strip().isdigit():
                out[key] = int(val)
        return out

    def remove(self) -> None:
        """Remove the cgroup, killing any stragglers first so the rmdir can succeed.

        Best-effort: a cgroup that cannot be removed is logged, not raised, since
        teardown must not fail a run.
        """
        kill = self.path / "cgroup.kill"
        try:
            if kill.exists():
                kill.write_text("1")
        except OSError:
            pass
        try:
            self.path.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove runner cgroup %s: %s", self.path, exc)


def delegation_available() -> bool:
    """Whether a memory-capped runner cgroup can actually be created here.

    Probes by creating and removing a real (tiny) cgroup, so it reflects true
    capability rather than guessing from mount state. The first call also performs
    one-time, idempotent preparation of the chosen parent: on the default path it
    establishes the leader pattern (relocating the orchestrator's own process); on the
    ``SYNNO_CGROUP_PARENT`` path it enables ``+memory`` on the shared slice and, when
    ``SYNNO_CGROUP_PARENT_MAX`` is set, writes the aggregate budget. Memoized: the
    (possibly bootstrapping) probe runs at most once per process.
    """
    global _delegation, _delegation_error, _probe_counter
    _guard_env_immutable()
    if _delegation is not None:
        return _delegation
    _probe_counter += 1
    try:
        cg = RunnerCgroup.create(
            memory_max_bytes=1 << 30,
            name=f"probe-{os.getpid()}-{_probe_counter}",
            oom_group=False,
        )
    except CgroupUnavailable as exc:
        _delegation = False
        _delegation_error = str(exc)
        return False
    cg.remove()
    _delegation = True
    _delegation_error = None
    return True


def delegation_failure_reason() -> Optional[str]:
    """The reason the last :func:`delegation_available` probe failed, or ``None`` if it
    has not failed. Used to surface the specific setup problem when failing closed."""
    return _delegation_error


def shared_parent_configured() -> bool:
    """Whether an explicit shared parent slice (``SYNNO_CGROUP_PARENT``) is configured.

    When True, a parent-setup failure is a hard error - the operator has demanded the
    aggregate slice, so falling back to ``RLIMIT_AS`` (which silently defeats the
    aggregate ceiling) is never acceptable, regardless of ``require_cgroup``.
    """
    return bool(os.environ.get(_ENV_PARENT, "").strip())
