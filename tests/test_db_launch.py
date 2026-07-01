"""Tests for the db_launch exec-in-place launcher and the cgroup manager (A2).

These cover the parts that run without cgroup delegation: the launcher's
exec/argv/fail-closed behavior, its preservation of the control-pipe fds across
exec (the keystone invariant - HotpatchProc's IPC must survive the launcher), and
the cgroup manager's parsing/fail-closed logic. The privileged cgroup-OOM path is
covered in test_hotpatch_cgroup.py, gated on real delegation.
"""
import os
import subprocess
import sys

import pytest

from synnodb.cpp_runner.hotpatch.cgroup import RunnerCgroup
from synnodb.cpp_runner.hotpatch.db_launch import db_launch_binary


@pytest.fixture(scope="module")
def launcher() -> str:
    return str(db_launch_binary())


def test_build_is_content_addressed_and_cached():
    a = db_launch_binary()
    b = db_launch_binary()
    assert a == b
    assert a.exists() and os.access(a, os.X_OK)


def test_exec_passthrough(launcher):
    out = subprocess.run(
        [launcher, "--", "/bin/echo", "hello"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "hello"


@pytest.mark.parametrize(
    "argv",
    [
        ["--cgroup"],          # flag missing its value
        ["--as-limit"],        # flag missing its value
        ["--"],                # no target program
        ["--bogus", "--", "/bin/true"],  # unknown flag
    ],
)
def test_usage_errors_exit_64(launcher, argv):
    out = subprocess.run([launcher, *argv], capture_output=True, text=True)
    assert out.returncode == 64, out.stderr


def test_fail_closed_on_unjoinable_cgroup(launcher):
    """A bad --cgroup must abort before exec, not run the target unbounded."""
    out = subprocess.run(
        [launcher, "--cgroup", "/sys/fs/cgroup/does/not/exist", "--",
         "/bin/echo", "SHOULD_NOT_RUN"],
        capture_output=True, text=True,
    )
    assert out.returncode == 83, out.stderr
    assert "SHOULD_NOT_RUN" not in out.stdout


def test_as_limit_is_enforced(launcher):
    """--as-limit caps RLIMIT_AS so an over-budget allocation fails in the target."""
    out = subprocess.run(
        [launcher, "--as-limit", str(64 * 1024 * 1024), "--",
         sys.executable, "-c", "b = bytearray(256*1024*1024); print('ALLOC OK')"],
        capture_output=True, text=True,
    )
    assert out.returncode != 0
    assert "ALLOC OK" not in out.stdout


def test_sets_new_session(launcher):
    """The target becomes its own session leader (PID == SID) for group reaping."""
    out = subprocess.run(
        [launcher, "--", "/bin/sh", "-c", 'echo "$$ $(ps -o sid= -p $$)"'],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    pid, sid = out.stdout.split()
    # db_launch called setsid() then exec'd /bin/sh in place, so sh is a session
    # leader: its session id equals its own pid.
    assert pid == sid


def test_control_pipe_ipc_survives_launcher_exec(launcher):
    """Keystone: db_launch must preserve the pass_fds control pipes across its
    in-place exec, exactly as HotpatchProc relies on. Replicates HotpatchProc's
    pipe wiring and proves a parent->child->parent round-trip over P2C_FD/C2P_FD.
    """
    # Same topology HotpatchProc uses: p2c (parent writes, child reads via P2C_FD),
    # c2p (child writes via C2P_FD, parent reads).
    p2c_r, p2c_w = os.pipe()
    c2p_r, c2p_w = os.pipe()
    stub = (
        "import os\n"
        "p2c = int(os.environ['P2C_FD']); c2p = int(os.environ['C2P_FD'])\n"
        "data = os.read(p2c, 5)\n"
        "os.write(c2p, b'ACK:' + data)\n"
    )
    proc = subprocess.Popen(
        [launcher, "--", sys.executable, "-c", stub],
        pass_fds=(p2c_r, c2p_w),
        env={**os.environ, "P2C_FD": str(p2c_r), "C2P_FD": str(c2p_w)},
    )
    os.close(p2c_r)
    os.close(c2p_w)
    try:
        os.write(p2c_w, b"hello")
        resp = os.read(c2p_r, 64)
        assert resp == b"ACK:hello"
        assert proc.wait(timeout=10) == 0
    finally:
        os.close(p2c_w)
        os.close(c2p_r)


def test_memory_events_parsing(tmp_path):
    """RunnerCgroup.memory_events parses the kernel's key/value lines."""
    (tmp_path / "memory.events").write_text(
        "low 0\nhigh 0\nmax 37\noom 1\noom_kill 2\noom_group_kill 1\n"
    )
    events = RunnerCgroup(tmp_path).memory_events()
    assert events == {
        "low": 0, "high": 0, "max": 37, "oom": 1, "oom_kill": 2, "oom_group_kill": 1
    }


def test_memory_events_missing_file_is_empty(tmp_path):
    assert RunnerCgroup(tmp_path / "gone").memory_events() == {}
