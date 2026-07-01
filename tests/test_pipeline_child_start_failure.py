"""A6: a stage that fails to start its child must fail the parent, not hang it.

stage_loop_impl set child_active=true unconditionally after next_start, but
start_stage_child returns an invalid ChildHandle (pid=-1) when fork()/pipe()
fails. The stage then never re-forks and never notifies a child, so no done token
is emitted and the Python parent waits forever. The fix emits a distinct
child-start-failure done token (kChildStartFailedExitCode, EX_OSERR=71), which
run_parent must include in its early-return set (otherwise it waits for trace data
that never comes - the child holds the trace pipe open - and hangs anyway).

This builds the real db.cpp with a minimal use-case whose single stage's
next_start deterministically returns the invalid handle a real fork failure
produces, then drives it over the actual P2C_FD/C2P_FD control protocol and
asserts run_parent unblocks within a timeout with a structured failure naming the
child-start failure (not the generic "threw a C++ exception"). The select()
timeout makes a regression - e.g. dropping the new sentinel from run_parent's
early-return - fail fast rather than wedge the suite.
"""
import json
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

_SOAK_DIR = Path(__file__).parent / "soak_engine"
_CPP_RUNNER = Path(__file__).resolve().parents[1] / "src" / "synnodb" / "cpp_runner"
_HOTPATCH_DIR = _CPP_RUNNER / "hotpatch"
_CXX = os.environ.get("CXX", "g++")

# Mirrors ipc::MESSAGE_MAGIC / ACTION_RUN and kChildStartFailedExitCode in pipeline.hpp.
_MAGIC = 0x31525043
_ACTION_RUN = 1
_CHILD_START_FAILED_EXIT_CODE = 71  # EX_OSERR; the distinct child-start-failure sentinel

pytestmark = pytest.mark.skipif(
    shutil.which(_CXX) is None or not sys.platform.startswith("linux"),
    reason="needs a C++ compiler",
)


def _compile(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"compile failed: {' '.join(args)}\n{proc.stderr}")


@pytest.fixture(scope="module")
def childfail_db(tmp_path_factory) -> Path:
    build_dir = tmp_path_factory.mktemp("childfail")
    # Real db.cpp + a minimal use-case whose stage fails to start its child.
    _compile([
        _CXX, "-O2", "-std=c++20",
        "-I", str(_CPP_RUNNER), "-I", str(_HOTPATCH_DIR), "-I", str(_CPP_RUNNER / "api"),
        "-o", str(build_dir / "db_childfail"),
        str(_CPP_RUNNER / "db.cpp"),
        str(_SOAK_DIR / "db_childfail_usecase.cpp"),
        str(_HOTPATCH_DIR / "build_id.cpp"),
        "-ldl",
    ])
    # The loader stage loads this .so before trying (and failing) to start its child.
    _compile([
        _CXX, "-O2", "-std=c++20", "-shared", "-fPIC", "-I", str(_SOAK_DIR),
        "-Wl,--build-id", "-o", str(build_dir / "libloader_soak.so"),
        str(_SOAK_DIR / "loader_soak.cpp"),
    ])
    return build_dir


def test_child_start_failure_fails_parent_not_hangs(childfail_db):
    p2c_r, p2c_w = os.pipe()  # Python writes p2c_w; db reads p2c_r via P2C_FD
    c2p_r, c2p_w = os.pipe()  # db writes c2p_w via C2P_FD; Python reads c2p_r
    env = dict(os.environ, P2C_FD=str(p2c_r), C2P_FD=str(c2p_w))
    proc = subprocess.Popen(
        [str(childfail_db / "db_childfail"), "."],
        cwd=str(childfail_db),
        env=env,
        pass_fds=(p2c_r, c2p_w),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(p2c_r)
    os.close(c2p_w)
    try:
        os.write(p2c_w, struct.pack("<IIQII", _MAGIC, _ACTION_RUN, 1, 0, 0))
        # run_parent must emit a JSON response line promptly. Without the fix it
        # waits for trace data that never arrives, so the select() below times out
        # and the test fails fast instead of hanging the suite.
        buf = b""
        while b"\n" not in buf:
            ready, _, _ = select.select([c2p_r], [], [], 15.0)
            assert ready, "run_parent hung: no response on child-start failure"
            chunk = os.read(c2p_r, 4096)
            assert chunk, "control pipe closed without a response"
            buf += chunk
        payload = json.loads(buf.split(b"\n", 1)[0].decode())
        assert payload["exit_code"] == _CHILD_START_FAILED_EXIT_CODE, payload
        assert payload["signal"] == 0, payload
        # The diagnostic must name the real failure, not the generic thrown-exception one.
        assert "failed to start its child process" in payload["stage_error"], payload
    finally:
        os.close(p2c_w)
        os.close(c2p_r)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
