"""A3: abandoned per-runner cgroups are reclaimed at launch.

On a graceful exit the pool teardown removes a runner's cgroup, but atexit does not run on SIGTERM
/ SIGKILL / a crash, so an empty runner cgroup can be left behind (the engine itself is already
gone via PR_SET_PDEATHSIG). _sweep_stale_runner_cgroups reclaims those at the next launch, for every
death mode. The sweep operates on the cgroup filesystem layout, so it is exercised here with plain
directories - no real cgroup controller needed.
"""
import os
import pathlib
import shutil
import time

import pytest

from synnodb.cpp_runner.hotpatch.cgroup import (
    _STALE_CGROUP_MIN_AGE_S,
    _sweep_stale_runner_cgroups,
)


@pytest.fixture
def cgroupfs_rmdir(monkeypatch):
    """Make Path.rmdir behave like cgroupfs: a process-free cgroup is removed together with its
    virtual control files. A plain tmpdir dir holds real files (cgroup.procs, ...), so a literal
    rmdir would raise ENOTEMPTY - the kernel does not, and the production sweep relies on that."""
    def _rmdir(self):
        shutil.rmtree(self)

    monkeypatch.setattr(pathlib.Path, "rmdir", _rmdir)


def _runner_cgroup(parent: pathlib.Path, name: str, *, pids: str = "", age_s: float = 0.0):
    d = parent / name
    d.mkdir()
    (d / "cgroup.procs").write_text(pids)
    if age_s:
        old = time.time() - age_s
        os.utime(d, (old, old))
    return d


def test_sweeps_only_stale_empty_runner_cgroups(tmp_path, cgroupfs_rmdir):
    old = _STALE_CGROUP_MIN_AGE_S + 60.0

    abandoned = _runner_cgroup(tmp_path, "synno-runner-A", pids="", age_s=old)     # empty + old
    live = _runner_cgroup(tmp_path, "synno-runner-B", pids="1234\n", age_s=old)    # still has procs
    fresh = _runner_cgroup(tmp_path, "synno-runner-C", pids="", age_s=0.0)         # just created
    # The leader (holds a live orchestrator process) is a sibling of the runner cgroups; even when
    # empty and old it must never be swept - the runner prefix excludes it.
    leader = _runner_cgroup(tmp_path, "synno-leader", pids="", age_s=old)
    other = _runner_cgroup(tmp_path, "some-slice", pids="", age_s=old)             # unrelated dir

    _sweep_stale_runner_cgroups(tmp_path)

    assert not abandoned.exists(), "an empty, old runner cgroup must be reclaimed"
    assert live.exists(), "a cgroup with live processes must never be removed"
    assert fresh.exists(), "a freshly-created cgroup must be left for its launcher to join"
    assert leader.exists(), "the leader cgroup must never be swept, even when empty and old"
    assert other.exists(), "only synno-runner-* cgroups are swept"


def test_sweep_is_best_effort_on_missing_parent(tmp_path):
    # Must never raise, even if the parent does not exist.
    _sweep_stale_runner_cgroups(tmp_path / "does-not-exist")
