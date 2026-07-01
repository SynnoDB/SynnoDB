from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "src" / "synnodb" / "cpp_runner" / "prepare_repo" / "templates"
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER = REPO / "tests" / "cpp" / "thread_pool_test.cpp"
CPU_AFFINITY = CPP_HELPERS / "cpu_affinity.cpp"


def test_thread_pool_parallel_reduce_and_exceptions(tmp_path):
    cc = shutil.which("g++") or shutil.which("clang++")
    if not cc:
        pytest.skip("no C++ compiler")

    out = tmp_path / "thread_pool_test"
    cmd = [
        cc,
        "-std=c++20",
        "-O2",
        "-pthread",
        "-I",
        str(TEMPLATES),
        "-I",
        str(CPP_HELPERS),
        str(DRIVER),
        str(CPU_AFFINITY),
        "-o",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"compile failed:\n{proc.stderr}")

    run = subprocess.run([str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "ok"
