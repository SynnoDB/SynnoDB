"""HotpatchProc cgroup launch-path tests (A2).

Covers the fail-closed contract, the dev/test fallback, and - when real cgroup v2
delegation is available - an end-to-end memory-ceiling breach classified as a
cgroup OOM. The OOM test is skipped where delegation is unavailable (e.g. CI
without a delegated slice); the launcher and cgroup mechanics are proven
separately in test_db_launch.py and under a delegated cgroup in development.
"""
import logging
import sys
from pathlib import Path

import pytest

from synnodb.cpp_runner.hotpatch import cgroup as cgroup_mod
from synnodb.cpp_runner.hotpatch import hotpatch_proc as hp_mod
from synnodb.cpp_runner.hotpatch.cgroup import delegation_available
from synnodb.cpp_runner.hotpatch.hotpatch_proc import HotpatchProc

SLEEP_CMD = f"{sys.executable} -c 'import time; time.sleep(60)'"


@pytest.fixture(autouse=True)
def _reset_cgroup_module_caches():
    """Keep each test hermetic: the cgroup module memoizes the parent/delegation and the
    process-static config signature, so reset them around every test (some tests here set
    SYNNO_CGROUP_PARENT) to avoid cross-test ordering effects."""
    for attr in ("_runner_parent", "_delegation", "_delegation_error", "_cgroup_env_sig"):
        setattr(cgroup_mod, attr, None)
    yield
    for attr in ("_runner_parent", "_delegation", "_delegation_error", "_cgroup_env_sig"):
        setattr(cgroup_mod, attr, None)


def test_fallback_pdeathsig_fails_closed_without_libc(monkeypatch):
    """The fallback launch path must refuse to launch when PR_SET_PDEATHSIG cannot be
    armed (no libc), rather than orphan a child that would survive a SIGKILL'd parent.
    Tested via the _libc-None branch, which raises before touching the test process's
    own session."""
    monkeypatch.setattr(hp_mod, "_libc", None)
    with pytest.raises(OSError, match="libc unavailable"):
        hp_mod._install_pdeathsig_and_new_session()


def test_fail_closed_when_cgroup_required_but_unavailable(monkeypatch):
    """require_cgroup=True with no delegation must refuse to launch."""
    monkeypatch.setattr(hp_mod, "delegation_available", lambda: False)
    proc = HotpatchProc(
        SLEEP_CMD, cwd=Path.cwd(), memory_max_bytes=64 << 20, require_cgroup=True
    )
    with pytest.raises(RuntimeError, match="required but unavailable"):
        proc._start()
    assert proc._proc is None  # nothing launched, nothing to clean up


def test_configured_shared_parent_fails_closed_even_without_require(monkeypatch):
    """A configured SYNNO_CGROUP_PARENT makes the cgroup ceiling mandatory regardless of
    require_cgroup: if the slice cannot be prepared, launch must raise rather than
    silently fall back to RLIMIT_AS (which defeats the aggregate guarantee)."""
    monkeypatch.setattr(hp_mod, "delegation_available", lambda: False)
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", "/sys/fs/cgroup/synnodb.slice")
    proc = HotpatchProc(
        SLEEP_CMD, cwd=Path.cwd(), memory_max_bytes=64 << 20, require_cgroup=False
    )
    with pytest.raises(RuntimeError, match="required but unavailable"):
        proc._start()
    assert proc._proc is None


def test_fallback_to_rlimit_when_not_required(monkeypatch, caplog):
    """require_cgroup=False with no delegation falls back (warns), no cgroup used."""
    monkeypatch.setattr(hp_mod, "delegation_available", lambda: False)
    proc = HotpatchProc(
        SLEEP_CMD, cwd=Path.cwd(), memory_max_bytes=64 << 20, require_cgroup=False
    )
    try:
        with caplog.at_level(logging.WARNING, logger=hp_mod.logger.name):
            proc._start()
        assert proc._proc is not None and proc._proc.poll() is None
        assert proc._cgroup is None  # fallback path: no cgroup
        assert "falling back to RLIMIT_AS" in caplog.text
    finally:
        if proc._proc is not None:
            proc._proc.kill()
            proc._proc.wait(timeout=5)
        proc._clear_proc_state()


def test_no_cgroup_path_when_memory_max_unset(monkeypatch):
    """Without memory_max_bytes the cgroup path is never engaged (no probe)."""
    called = {"probe": False}

    def _flag():
        called["probe"] = True
        return True

    monkeypatch.setattr(hp_mod, "delegation_available", _flag)
    proc = HotpatchProc(SLEEP_CMD, cwd=Path.cwd())  # memory_max_bytes=None
    try:
        proc._start()
        assert proc._cgroup is None
        assert called["probe"] is False
    finally:
        if proc._proc is not None:
            proc._proc.kill()
            proc._proc.wait(timeout=5)
        proc._clear_proc_state()


def test_failed_launch_after_cgroup_create_reclaims_cgroup(monkeypatch):
    """If launch fails after the runner cgroup is created (e.g. the launcher binary
    is missing), the cgroup must be removed and self._proc left None - no leak."""
    removed = {"called": False}

    class _FakeCgroup:
        path = Path("/sys/fs/cgroup/__fake__")
        procs_dir = "/sys/fs/cgroup/__fake__"

        def remove(self):
            removed["called"] = True

    def _boom():
        raise RuntimeError("launcher build boom")

    monkeypatch.setattr(hp_mod, "delegation_available", lambda: True)
    monkeypatch.setattr(
        hp_mod.RunnerCgroup,
        "create",
        classmethod(lambda cls, memory_max_bytes, name, oom_group=True: _FakeCgroup()),
    )
    # Fails after the cgroup is created but before Popen succeeds.
    monkeypatch.setattr(hp_mod, "db_launch_binary", _boom)

    proc = HotpatchProc("/bin/true", cwd=Path.cwd(), memory_max_bytes=64 << 20)
    with pytest.raises(RuntimeError, match="boom"):
        proc._start()
    assert removed["called"] is True  # cgroup reclaimed
    assert proc._cgroup is None
    assert proc._proc is None


@pytest.mark.skipif(
    not delegation_available(),
    reason="cgroup v2 memory delegation unavailable in this environment",
)
def test_cgroup_breach_is_classified_as_cgroup_oom():
    """A real memory.max breach is OOM-killed as a group and surfaced as a cgroup
    OOM in the run result, and the runner cgroup is cleaned up afterwards.

    run() reports a signal-kill via the result's `response` rather than raising,
    so the production invariant is that the response names it a cgroup OOM (proving
    memory.events drove the classification, not a guess from the bare signal).
    """
    hog = f"{sys.executable} -c 'b = bytearray(256*1024*1024)'"
    proc = HotpatchProc(hog, cwd=Path.cwd(), memory_max_bytes=64 << 20)
    try:
        result = proc.run(timeout=30)
        assert "cgroup OOM" in result.response, result.response
        assert "oom_group_kill=1" in result.response, result.response
        # run() must reclaim the cgroup itself on child death - no manual cleanup,
        # no dead runner left cached holding an empty cgroup.
        assert proc._cgroup is None
        assert proc._proc is None
    finally:
        proc._clear_proc_state()  # safety net only if an assertion fired first
