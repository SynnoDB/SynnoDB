"""read_build_id must agree with the linker's GNU build-id and fail soft on junk.

It is the loader-source-change detector behind the engine restart in run.py, so a
wrong-but-non-None result would either restart the engine needlessly or, worse,
keep a stale one; a robust None on unreadable input keeps it from thrashing.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from synnodb.cpp_runner.hotpatch.elf_build_id import read_build_id

_CXX = os.environ.get("CXX", "g++")

pytestmark = pytest.mark.skipif(
    shutil.which(_CXX) is None or not sys.platform.startswith("linux"),
    reason="needs a C++ compiler producing ELF shared objects",
)

_SRC = "extern \"C\" int f() { return 0; }\n"


def _build_so(path: Path, build_id_flag: str) -> None:
    src = path.with_suffix(".cpp")
    src.write_text(_SRC)
    subprocess.run(
        [_CXX, "-O0", "-shared", "-fPIC", build_id_flag, "-o", str(path), str(src)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_reads_explicit_build_id(tmp_path):
    want = "0123456789abcdef0123456789abcdef01234567"
    so = tmp_path / "lib_explicit.so"
    _build_so(so, f"-Wl,--build-id=0x{want}")
    assert read_build_id(so) == want


def test_distinguishes_two_build_ids(tmp_path):
    a = tmp_path / "a.so"
    b = tmp_path / "b.so"
    _build_so(a, "-Wl,--build-id=0x%040x" % 1)
    _build_so(b, "-Wl,--build-id=0x%040x" % 2)
    assert read_build_id(a) != read_build_id(b)
    assert read_build_id(a) is not None


def test_default_build_id_matches_readelf(tmp_path):
    if shutil.which("readelf") is None:
        pytest.skip("readelf not available for cross-check")
    so = tmp_path / "lib_default.so"
    _build_so(so, "-Wl,--build-id")
    out = subprocess.run(
        ["readelf", "-n", str(so)], capture_output=True, text=True, check=True
    ).stdout
    # readelf prints "Build ID: <hex>"
    expected = next(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if "Build ID:" in line
    )
    assert read_build_id(so) == expected


def test_missing_file_returns_none(tmp_path):
    assert read_build_id(tmp_path / "nope.so") is None


def test_non_elf_returns_none(tmp_path):
    junk = tmp_path / "junk.so"
    junk.write_bytes(b"not an elf file, just some bytes" * 4)
    assert read_build_id(junk) is None


def test_elf_without_build_id_returns_none(tmp_path):
    so = tmp_path / "lib_none.so"
    _build_so(so, "-Wl,--build-id=none")
    assert read_build_id(so) is None
