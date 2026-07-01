"""Aggregate / shared-parent cgroup tests (A2 second half).

Two layers:

* **Discovery / validation / nesting** - run anywhere by driving ``cgroup.py`` against a
  fake cgroup tree on a tmpdir (no kernel delegation needed). These pin the production
  invariants: a shared parent must be bounded and hold no processes, runner cgroups nest
  *under* it, the parent never gets ``oom.group=1`` (single-victim), and a configured but
  unusable shared parent fails closed instead of silently nesting per-orchestrator.
* **Single-victim under a real kernel** - gated on actual cgroup v2 delegation. It first
  reproduces the aggregate gap (two capped runners whose sum exceeds a budget both
  survive when there is no shared parent), then proves the fix (under a capped shared
  parent the kernel OOM-kills exactly one runner, the other lives). Skipped where
  delegation is unavailable; verified in development under a sudo-delegated cgroup.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from synnodb.cpp_runner.hotpatch import cgroup
from synnodb.cpp_runner.hotpatch.cgroup import (
    CgroupUnavailable,
    RunnerCgroup,
    delegation_available,
)
from synnodb.cpp_runner.hotpatch.db_launch import db_launch_binary


# --------------------------------------------------------------------------------------
# Layer 1: discovery / validation / nesting against a fake cgroup tree (no delegation).
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_cgroup_caches():
    """Reset the module-level parent/delegation caches around every test so a discovery
    from one test (or an early error before a manual reset) never leaks into the next."""
    cgroup._runner_parent = None
    cgroup._delegation = None
    cgroup._delegation_error = None
    cgroup._cgroup_env_sig = None
    yield
    cgroup._runner_parent = None
    cgroup._delegation = None
    cgroup._delegation_error = None
    cgroup._cgroup_env_sig = None


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """A tmpdir posing as the cgroup v2 mount.

    cgroup.py only reads/writes plain text control files, so a directory tree mirrors a
    real hierarchy closely enough to exercise discovery, validation and nesting without
    the kernel.
    """
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("memory\n")  # marks the mount as v2
    monkeypatch.setattr(cgroup, "CGROUP_ROOT", root)
    return root


def _make_parent(
    root: Path,
    name: str = "synnodb.slice",
    *,
    controllers: str = "memory",
    subtree: str = "",
    procs: str = "",
    memory_max: str = "max",
    oom_group: str | None = None,
) -> Path:
    p = root / name
    p.mkdir()
    (p / "cgroup.controllers").write_text(controllers + "\n")
    (p / "cgroup.subtree_control").write_text(subtree + "\n")
    (p / "cgroup.procs").write_text(procs)
    (p / "memory.max").write_text(memory_max + "\n")
    if oom_group is not None:
        (p / "memory.oom.group").write_text(oom_group + "\n")
    return p


@pytest.mark.parametrize(
    "text,expected",
    [
        ("500000000000", 500_000_000_000),
        ("480G", 480 * (1 << 30)),
        ("64M", 64 * (1 << 20)),
        ("2T", 2 * (1 << 40)),
        ("1024k", 1024 * (1 << 10)),
    ],
)
def test_parse_byte_size_ok(text, expected):
    assert cgroup._parse_byte_size(text) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "", "  ", "G", "12x", "-5", "0", "1.5G", "abc",
        "1_000",   # int() accepts underscores; the byte parser must not
        "+5G",     # a sign is not a byte count
        "5 G",     # internal whitespace
        "٤٨٠",  # Unicode digits (480) - ASCII only
    ],
)
def test_parse_byte_size_rejects_malformed(bad):
    """A malformed budget must raise ValueError, never silently parse to something that
    disables the aggregate cap. ValueError (not assert) so it holds under python -O."""
    with pytest.raises(ValueError):
        cgroup._parse_byte_size(bad)


def test_shared_parent_path_must_be_under_root(fake_root):
    with pytest.raises(CgroupUnavailable, match="under"):
        cgroup._shared_parent_path("/etc/passwd")
    with pytest.raises(CgroupUnavailable, match="not the root"):
        cgroup._shared_parent_path(str(fake_root))


def test_shared_parent_path_rejects_traversal(fake_root):
    """A `..` traversal must not escape the cgroup mount (it is normalised first)."""
    with pytest.raises(CgroupUnavailable):
        cgroup._shared_parent_path("../../etc")
    with pytest.raises(CgroupUnavailable):
        cgroup._shared_parent_path(str(fake_root / ".." / ".." / "etc"))


def test_shared_parent_path_accepts_relative(fake_root):
    assert cgroup._shared_parent_path("synnodb.slice") == fake_root / "synnodb.slice"


def test_prepare_shared_parent_missing(fake_root):
    with pytest.raises(CgroupUnavailable, match="does not exist"):
        cgroup._prepare_shared_parent(fake_root / "absent.slice")


def test_prepare_shared_parent_without_memory_controller(fake_root):
    parent = _make_parent(fake_root, controllers="cpu", memory_max="1000000")
    with pytest.raises(CgroupUnavailable, match="memory controller not available"):
        cgroup._prepare_shared_parent(parent)


def test_prepare_shared_parent_rejects_internal_processes(fake_root):
    parent = _make_parent(fake_root, procs="4242\n", memory_max="1000000")
    with pytest.raises(CgroupUnavailable, match="holds processes directly"):
        cgroup._prepare_shared_parent(parent)


def test_prepare_shared_parent_unbounded_without_budget(fake_root, monkeypatch):
    """A shared parent with no memory.max budget gives no aggregate protection, so it
    must be refused (fail closed) rather than used."""
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    parent = _make_parent(fake_root, memory_max="max")
    with pytest.raises(CgroupUnavailable, match="no memory.max budget"):
        cgroup._prepare_shared_parent(parent)


def test_prepare_shared_parent_enables_subtree_and_keeps_existing_budget(fake_root, monkeypatch):
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    parent = _make_parent(fake_root, subtree="", memory_max="700000000")
    out = cgroup._prepare_shared_parent(parent)
    assert out == parent
    # The code enables the memory controller for children (the kernel echoes the
    # "+memory" write back as "memory"; the fake tree records the literal write).
    assert "memory" in (parent / "cgroup.subtree_control").read_text()
    assert (parent / "memory.max").read_text().strip() == "700000000"  # left untouched


def test_prepare_shared_parent_applies_budget_env(fake_root, monkeypatch):
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "512M")
    parent = _make_parent(fake_root, memory_max="max")
    cgroup._prepare_shared_parent(parent)
    assert (parent / "memory.max").read_text().strip() == str(512 * (1 << 20))


def _fail_memory_max_writes(monkeypatch):
    """Make writes to any ``memory.max`` raise EACCES, as if the slice's memory.max is
    owned by root while only the subtree is delegated."""
    orig = Path.write_text

    def _maybe_fail(self, *args, **kwargs):
        if self.name == "memory.max":
            raise PermissionError("read-only memory.max")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _maybe_fail)


def test_parent_max_unwritable_refuses_to_weaken(fake_root, monkeypatch):
    """An explicit SYNNO_CGROUP_PARENT_MAX is authoritative: if it cannot be written and
    the existing limit is looser (current > want), refuse rather than run under a weaker
    aggregate cap than the operator asked for."""
    parent = _make_parent(fake_root, memory_max="900000000")  # looser than the 500M ask
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "500M")
    _fail_memory_max_writes(monkeypatch)
    with pytest.raises(CgroupUnavailable, match="weaker|looser"):
        cgroup._configure_parent_budget(parent)


def test_parent_max_unwritable_refuses_when_unbounded(fake_root, monkeypatch):
    parent = _make_parent(fake_root, memory_max="max")  # unbounded == weakest
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "500M")
    _fail_memory_max_writes(monkeypatch)
    with pytest.raises(CgroupUnavailable):
        cgroup._configure_parent_budget(parent)


def test_parent_max_unwritable_keeps_tighter_existing(fake_root, monkeypatch, caplog):
    """If the write fails but the existing limit is already at least as tight as
    requested, running under the stricter cap is safe - keep it with a warning."""
    parent = _make_parent(fake_root, memory_max="100000000")  # tighter than the 500M ask
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "500M")
    _fail_memory_max_writes(monkeypatch)
    with caplog.at_level("WARNING", logger=cgroup.logger.name):
        cgroup._configure_parent_budget(parent)  # must not raise
    assert (parent / "memory.max").read_text().strip() == "100000000"
    assert "tighter" in caplog.text


def test_prepare_shared_parent_warns_on_kill_all(fake_root, monkeypatch, caplog):
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    parent = _make_parent(fake_root, memory_max="1000000", oom_group="1")
    with caplog.at_level("WARNING", logger=cgroup.logger.name):
        cgroup._prepare_shared_parent(parent)
    assert "kill-all" in caplog.text


def test_runner_cgroup_nests_under_shared_parent_single_victim(fake_root, monkeypatch):
    """The core nesting + single-victim layout: with SYNNO_CGROUP_PARENT set, a runner
    cgroup is created directly under the shared parent, the *child* gets oom.group=1, and
    the *parent* is never given oom.group=1 by us (so a breach kills one runner)."""
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    parent = _make_parent(fake_root, memory_max="1000000000")
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", str(parent))

    runner = RunnerCgroup.create(256 << 20, name="runner-a")
    assert runner.path.parent == parent  # nested directly under the shared parent
    assert (runner.path / "memory.oom.group").read_text().strip() == "1"  # child kills as a group
    assert (runner.path / "memory.max").read_text().strip() == str(256 << 20)
    # We must never stamp oom.group=1 on the shared parent (that would be kill-all).
    assert not (parent / "memory.oom.group").exists()


def test_shared_parent_required_failure_does_not_fall_back(fake_root, monkeypatch):
    """When SYNNO_CGROUP_PARENT is set but unusable, _prepare_runner_parent must raise -
    never silently nest under the orchestrator's own cgroup (which would drop the
    aggregate guarantee)."""
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", str(fake_root / "absent.slice"))

    def _boom():
        raise AssertionError("must not consult the orchestrator's own cgroup")

    monkeypatch.setattr(cgroup, "_self_cgroup_dir", _boom)
    with pytest.raises(CgroupUnavailable):
        cgroup._prepare_runner_parent()


def test_parent_config_change_after_probe_fails_closed(fake_root, monkeypatch):
    """The process-global parent/delegation caches are keyed to the parent config: if the
    process first resolves without a shared parent and a parent is set later, both cached
    entry points must refuse (process-static) rather than serve the stale per-orchestrator
    parent - which would nest runners outside the aggregate slice."""
    monkeypatch.delenv("SYNNO_CGROUP_PARENT", raising=False)
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    cgroup._guard_env_immutable()  # captures the ("", "") signature, as a first probe would
    assert cgroup._cgroup_env_sig == ("", "")

    parent = _make_parent(fake_root, memory_max="1000000000")
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", str(parent))
    with pytest.raises(CgroupUnavailable, match="process-static"):
        cgroup._prepare_runner_parent()
    with pytest.raises(CgroupUnavailable, match="process-static"):
        cgroup.delegation_available()


def test_parent_max_change_after_probe_fails_closed(monkeypatch):
    """Changing only the aggregate budget mid-process is also refused (the signature
    includes SYNNO_CGROUP_PARENT_MAX)."""
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", "/sys/fs/cgroup/synnodb.slice")
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "500G")
    cgroup._guard_env_immutable()
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "400G")
    with pytest.raises(CgroupUnavailable, match="process-static"):
        cgroup._guard_env_immutable()


# --------------------------------------------------------------------------------------
# Layer 2: real-kernel single-victim proof (privileged; gated on delegation).
# --------------------------------------------------------------------------------------

# The per-runner cap is set far above one hog so a single runner can never die on its
# own cap: the only way a runner is killed is the shared-parent aggregate breach. The
# parent budget sits between one hog and two, so two hogs must breach it.
_PARENT_MAX = 240 << 20
_RUNNER_MAX = 1 << 30
_HOG_MB = 128


def _spawn_hog(runner: RunnerCgroup) -> subprocess.Popen:
    """Launch, into ``runner``'s cgroup via db_launch, a process that pins ~_HOG_MB and
    sleeps. db_launch joins the cgroup before exec, so the memory is charged there from
    the first instruction."""
    hog = [
        sys.executable,
        "-c",
        f"import time; _b = bytearray({_HOG_MB} * 1024 * 1024); time.sleep(60)",
    ]
    argv = [str(db_launch_binary()), "--cgroup", runner.procs_dir, "--", *hog]
    return subprocess.Popen(argv)


def _oom_group_killed(runner: RunnerCgroup) -> bool:
    return runner.memory_events().get("oom_group_kill", 0) >= 1


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


@pytest.mark.skipif(
    not delegation_available(),
    reason="cgroup v2 memory delegation unavailable in this environment",
)
def test_aggregate_gap_without_shared_parent_both_survive():
    """Reproduce the gap: with no shared parent, two runners that each fit their own cap
    but whose sum exceeds a notional budget are *not* policed together - both survive.
    This is exactly what the aggregate parent slice is needed to prevent."""
    os.environ.pop("SYNNO_CGROUP_PARENT", None)
    cgroup._runner_parent = None
    cgroup._delegation = None
    cgroup._cgroup_env_sig = None
    runners = [RunnerCgroup.create(_RUNNER_MAX, name=f"gap-{i}") for i in range(2)]
    procs = []
    try:
        procs = [_spawn_hog(r) for r in runners]
        time.sleep(4)  # let both pin their memory
        assert all(p.poll() is None for p in procs), "a runner died without a shared cap"
        assert not any(_oom_group_killed(r) for r in runners)
    finally:
        for p in procs:
            _kill(p)
        for r in runners:
            r.remove()


@pytest.mark.skipif(
    not delegation_available(),
    reason="cgroup v2 memory delegation unavailable in this environment",
)
def test_shared_parent_aggregate_kills_single_victim(monkeypatch):
    """Prove the fix: under a capped shared parent, two runners whose sum exceeds the
    parent budget trigger an aggregate OOM that kills exactly one runner's tree (the
    other survives), because each child has oom.group=1 and the parent does not."""
    cgroup._runner_parent = None
    cgroup._delegation = None
    cgroup._cgroup_env_sig = None
    os.environ.pop("SYNNO_CGROUP_PARENT", None)
    base = cgroup._prepare_runner_parent()  # orchestrator's delegated cgroup (leader set up)

    parent = base / f"synno-agg-test-{os.getpid()}"
    parent.mkdir()
    (parent / "memory.max").write_text(str(_PARENT_MAX))
    # The parent must remain single-victim: we never set oom.group=1 on it.
    assert (parent / "memory.oom.group").read_text().strip() == "0"

    # This test deliberately reconfigures the parent mid-process to stage the two setups,
    # so it must clear the process-static config guard along with the caches.
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", str(parent))
    cgroup._runner_parent = None
    cgroup._delegation = None
    cgroup._cgroup_env_sig = None

    runners = [RunnerCgroup.create(_RUNNER_MAX, name=f"agg-{i}") for i in range(2)]
    for r in runners:
        assert r.path.parent == parent
    procs = []
    try:
        procs = [_spawn_hog(r) for r in runners]

        deadline = time.time() + 25
        while time.time() < deadline and not any(_oom_group_killed(r) for r in runners):
            time.sleep(0.25)
        time.sleep(0.5)  # let the kill settle

        victims = [i for i, r in enumerate(runners) if _oom_group_killed(r)]
        assert len(victims) == 1, [r.memory_events() for r in runners]
        (v,) = victims
        s = 1 - v
        assert procs[v].poll() is not None, "victim process should be dead"
        assert procs[s].poll() is None, "survivor process should still be alive"
        # The aggregate breach was accounted at the parent, not a per-runner cap.
        assert cgroup._read_memory_max(parent) == _PARENT_MAX
        parent_events = {}
        for line in (parent / "memory.events").read_text().splitlines():
            k, _, val = line.partition(" ")
            if val.strip().isdigit():
                parent_events[k] = int(val)
        assert parent_events.get("oom_kill", 0) >= 1, parent_events
    finally:
        for p in procs:
            _kill(p)
        for r in runners:
            r.remove()
        cgroup._runner_parent = None
        cgroup._delegation = None
        try:
            parent.rmdir()
        except OSError:
            pass
