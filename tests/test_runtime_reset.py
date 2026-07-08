"""A resync swaps the source data out, so the warm runtime built against the old data must be
retired: every warm hotpatch process terminated and every staged ``/dev/shm`` segment dropped. A
warm process left alive would keep serving the previous snapshot (the loader ingests once per
process and never re-reads its input) and its orphaned shm segment would keep occupying RAM. These
tests pin that teardown with fakes (no real engine launched, no real shm needed)."""

from pathlib import Path

import synnodb.cpp_runner.shm_stage as shm_stage
from synnodb.cpp_runner.hotpatch.pool import HotpatchPool
from synnodb.cpp_runner.runtime_reset import reset_warm_runtime


class _FakeRunner:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def test_terminate_all_retires_every_warm_runner():
    runners = {k: _FakeRunner() for k in ("a", "b", "c")}
    for key, runner in runners.items():
        HotpatchPool.get(key, factory=lambda r=runner: r, fingerprint="id")
    try:
        HotpatchPool.terminate_all()
        assert all(r.terminated for r in runners.values())
        # The pool is empty afterwards, so a later run builds fresh processes.
        assert HotpatchPool._runners == {}
    finally:
        HotpatchPool.terminate_all()


def test_clear_staged_segments_removes_and_forgets_dirs(tmp_path: Path):
    staged = tmp_path / "synno-synth-fake"
    staged.mkdir()
    (staged / "lineitem.arrow").write_bytes(b"x")
    shm_stage._STAGED.add(staged)
    try:
        shm_stage.clear_staged_segments()
        assert not staged.exists()  # RAM (tmpfs) reclaimed
        assert shm_stage._STAGED == set()  # not re-cleaned or leaked on the next run
    finally:
        shm_stage._STAGED.discard(staged)


def test_reset_warm_runtime_tears_down_procs_and_segments(tmp_path: Path):
    runner = _FakeRunner()
    HotpatchPool.get("k", factory=lambda: runner, fingerprint="id")
    staged = tmp_path / "synno-synth-fake"
    staged.mkdir()
    shm_stage._STAGED.add(staged)
    try:
        reset_warm_runtime()
        assert runner.terminated is True
        assert HotpatchPool._runners == {}
        assert not staged.exists()
        assert shm_stage._STAGED == set()
    finally:
        HotpatchPool.terminate_all()
        shm_stage._STAGED.discard(staged)


def test_reset_warm_runtime_is_a_safe_noop_when_cold():
    # No warm procs, nothing staged: calling at a resync boundary must not raise.
    reset_warm_runtime()
    assert HotpatchPool._runners == {}
    assert shm_stage._STAGED == set()
