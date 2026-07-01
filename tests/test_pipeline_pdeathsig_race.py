"""A3: an internal stage child must not run orphaned if its parent died in the fork->prctl window.

fork(2) clears PR_SET_PDEATHSIG, so every internal stage child re-arms it. But between fork() and
that prctl() the parent could already have exited, leaving the armed signal referencing a gone
parent and the child running forever. detail::rearm_pdeathsig_or_exit closes the race with the same
captured-parent-pid check db_launch performs for the top-level ./db: if getppid() no longer matches
the intended parent, the child _exit()s with kParentDeathSetupFailedExitCode instead of continuing.

This drives the real helper (compiled from pipeline.hpp) in a fork, forcing the mismatch branch, and
asserts the child exits with the sentinel rather than proceeding.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SOAK_DIR = Path(__file__).parent / "soak_engine"
_HOTPATCH_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "synnodb" / "cpp_runner" / "hotpatch"
)
_CXX = os.environ.get("CXX", "g++")

# Mirrors kParentDeathSetupFailedExitCode in pipeline.hpp.
_PARENT_DEATH_SETUP_FAILED = 72

pytestmark = pytest.mark.skipif(
    shutil.which(_CXX) is None or not sys.platform.startswith("linux"),
    reason="needs a C++ compiler on Linux",
)


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("pdeathsig") / "probe"
    proc = subprocess.run(
        [
            _CXX,
            "-O2",
            "-std=c++20",
            "-I",
            str(_HOTPATCH_DIR),
            "-o",
            str(out),
            str(_SOAK_DIR / "pdeathsig_race_probe.cpp"),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"compile failed:\n{proc.stderr}")
    return out


def test_child_exits_when_parent_already_died(probe):
    # The child's intended parent no longer matches getppid() -> it must refuse to run orphaned.
    result = subprocess.run([str(probe), "race"], timeout=15)
    assert result.returncode == _PARENT_DEATH_SETUP_FAILED


def test_child_proceeds_when_parent_is_alive(probe):
    # The common case: intended parent matches, so the helper arms pdeathsig and returns; the
    # child runs to its normal _exit(0). A regression that always exits would break this.
    result = subprocess.run([str(probe)], timeout=15)
    assert result.returncode == 0
