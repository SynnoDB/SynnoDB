"""Compile the C++ engine-side shm Arrow helpers and round-trip them against the
tested Python transport — validating the Phase-3 zero-copy data plane in *real* C++.

Skips cleanly when no C++ toolchain / Arrow dev headers are present, so CI without a
compiler still passes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pyarrow as pa
import pytest

from synnodb.router.shm_transport import ShmWriter, read_table

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER_SRC = REPO / "tests" / "cpp" / "shm_io_test.cpp"


def _compiler() -> str | None:
    for cc in ("g++", "clang++"):
        if shutil.which(cc):
            return cc
    return None


def _pkgconfig(flag: str) -> list[str]:
    out = subprocess.run(["pkg-config", flag, "arrow"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.split()


@pytest.fixture(scope="module")
def driver(tmp_path_factory) -> Path:
    cc = _compiler()
    if cc is None:
        pytest.skip("no C++ compiler (g++/clang++) available")
    if (
        not shutil.which("pkg-config")
        or subprocess.run(["pkg-config", "--exists", "arrow"]).returncode != 0
    ):
        pytest.skip("Arrow C++ dev (pkg-config arrow) not available")

    out_bin = tmp_path_factory.mktemp("cpp") / "shm_io_test"
    cmd = (
        [cc, "-std=c++17", "-O2", "-I", str(CPP_HELPERS)]
        + _pkgconfig("--cflags")
        + [str(DRIVER_SRC)]
        + _pkgconfig("--libs")
        + ["-o", str(out_bin)]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"C++ compile failed:\n{proc.stderr}")
    return out_bin


def test_ingest_python_to_cpp(driver, tmp_path):
    n = 50_000
    table = pa.table(
        {"a": pa.array(range(n), pa.int64()), "label": [f"r{i % 7}" for i in range(n)]}
    )
    w = ShmWriter(base_dir=tmp_path)
    ref = w.write_table(table)
    out = subprocess.run(
        [str(driver), "read", str(tmp_path / ref.name)], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"rows={n} cols=2 col0=a sum0={sum(range(n))}"
    w.close()


def test_egress_cpp_to_python(driver, tmp_path):
    m = 30_000
    path = tmp_path / "result.arrow"
    out = subprocess.run(
        [str(driver), "write", str(path), str(m)], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    back = read_table(path)  # Python reads the C++-written segment, zero-copy
    assert back.num_rows == m
    assert back.column("a").to_pylist()[:5] == [0, 1, 2, 3, 4]
    assert sum(back.column("a").to_pylist()) == sum(range(m))


def test_large_table_roundtrip_both_directions(driver, tmp_path):
    n = 1_000_000  # ~8 MB int64 column
    table = pa.table({"a": pa.array(range(n), pa.int64()), "label": ["r0"] * n})
    w = ShmWriter(base_dir=tmp_path)
    ref = w.write_table(table)
    out = subprocess.run(
        [str(driver), "read", str(tmp_path / ref.name)], capture_output=True, text=True
    )
    assert out.stdout.strip() == f"rows={n} cols=2 col0=a sum0={sum(range(n))}"
    w.close()
