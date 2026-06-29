"""Tests for ProcTreeTimeoutKiller's build- vs query-stage timeout handling.

Regression context: the killer only armed once a query-stage descendant at
`min_descendant_depth` existed, so a livelock inside the build/loader stage
(which never spawns that leaf — e.g. a buggy parallel sort in db_loader) ran
completely unbounded. The build stage now has its own (larger) deadline.

The killer is driven purely off `_rightmost_descendant` (depth) and a monotonic
clock, so we stub the descendant lookup + the kill, and fake the clock.
"""

import synnodb.cpp_runner.utils.proc_utils as proc_utils
from synnodb.cpp_runner.utils.proc_utils import ProcTreeTimeoutKiller


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make_killer(monkeypatch, *, depth_holder, timeout, build_timeout):
    clock = _Clock()
    monkeypatch.setattr(proc_utils.time, "monotonic", clock)

    killer = ProcTreeTimeoutKiller(
        root_pid=1000,
        timeout=timeout,
        min_descendant_depth=3,
        build_timeout=build_timeout,
    )
    killed: list[int] = []
    # rightmost descendant: a fixed victim pid at the depth the test dictates.
    killer._rightmost_descendant = lambda _pid: (4242, depth_holder["d"])  # type: ignore[assignment]
    killer._kill = lambda pid: killed.append(pid)  # type: ignore[assignment]
    return killer, clock, killed


def test_build_stage_livelock_killed_at_build_deadline(monkeypatch):
    # Builder hangs at depth 2 (below the query depth of 3). It must NOT be
    # killed at the short query timeout, but MUST be killed at build_timeout.
    depth = {"d": 2}
    killer, clock, killed = _make_killer(
        monkeypatch, depth_holder=depth, timeout=120, build_timeout=600
    )

    clock.t = 0.0
    killer.enforce()  # arms the build-stage timer
    clock.t = 130.0  # past the 120s query timeout, but this is the build stage
    killer.enforce()
    assert killed == [], "build stage must not be killed at the query timeout"

    clock.t = 601.0  # past the build deadline
    killer.enforce()
    assert killed == [4242]

    # idempotent: only one kill ever
    clock.t = 5000.0
    killer.enforce()
    assert killed == [4242]


def test_query_stage_uses_query_timeout(monkeypatch):
    depth = {"d": 3}
    killer, clock, killed = _make_killer(
        monkeypatch, depth_holder=depth, timeout=120, build_timeout=600
    )

    clock.t = 0.0
    killer.enforce()  # arms query-stage timer
    clock.t = 119.0
    killer.enforce()
    assert killed == []
    clock.t = 121.0
    killer.enforce()
    assert killed == [4242]


def test_stage_transition_resets_timer(monkeypatch):
    # Time spent in the build stage must not be charged against the query budget.
    depth = {"d": 2}
    killer, clock, killed = _make_killer(
        monkeypatch, depth_holder=depth, timeout=10, build_timeout=10_000
    )

    clock.t = 0.0
    killer.enforce()  # arm build
    clock.t = 100.0  # 100s in the build stage (well past the 10s query timeout)
    killer.enforce()
    assert killed == []

    # query leaf appears -> stage changes to query, timer restarts at t=100
    depth["d"] = 3
    killer.enforce()
    clock.t = 109.0  # only 9s into the query stage
    killer.enforce()
    assert killed == [], "build time must not count toward the query deadline"
    clock.t = 111.0  # 11s into the query stage -> over the 10s query timeout
    killer.enforce()
    assert killed == [4242]


def test_build_timeout_disabled_preserves_query_only_behaviour(monkeypatch):
    # build_timeout=0 -> a build-stage descendant is never killed (original behaviour).
    depth = {"d": 2}
    killer, clock, killed = _make_killer(
        monkeypatch, depth_holder=depth, timeout=120, build_timeout=0
    )
    clock.t = 0.0
    killer.enforce()
    clock.t = 100_000.0
    killer.enforce()
    assert killed == []
    assert killer.start is None  # never armed


def test_no_descendant_never_arms(monkeypatch):
    depth = {"d": 0}
    killer, clock, killed = _make_killer(
        monkeypatch, depth_holder=depth, timeout=120, build_timeout=600
    )
    clock.t = 0.0
    killer.enforce()
    clock.t = 100_000.0
    killer.enforce()
    assert killed == []
    assert killer.start is None
