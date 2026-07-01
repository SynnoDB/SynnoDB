"""The SSD engine template's build() must be exception-safe (reproduce-first for B2).

build() allocates the Database, its BufferPool, and every page frame. A bad_alloc mid-build -
the out-of-memory-during-build case that ratcheted the SF50 run to 480 GB - must free everything
already allocated, not hard-leak the partial dataset. The C++ driver installs a throwing
operator new[] that fails partway through the frame-pool allocation, runs the real build() from
the template, and lets AddressSanitizer's exit-time leak check catch any leak or double-free.

Against the raw-new template this fails (ASan reports the leaked Database + partial frames);
against the RAII template it passes. It is the regression guard for that fix.

Skips without a C++ toolchain, a working sanitizer runtime, or Arrow/Parquet dev headers
(db_loader.cpp includes them).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SSD = REPO / "src" / "synnodb" / "cpp_runner" / "prepare_repo" / "templates" / "olap" / "ssd"
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER = REPO / "tests" / "cpp" / "build_raii_test.cpp"

# A broken sanitizer runtime (e.g. LeakSanitizer denied ptrace in a sandbox) aborts nonzero even
# with no leak; these markers tell that apart from a real leak so the test skips instead of failing.
_SANITIZER_UNAVAILABLE = (
    "LeakSanitizer has encountered a fatal error",
    "Failed to use and restart",
    "AddressSanitizer failed to allocate",
    "operation not permitted",
)


def _toolchain_ok() -> bool:
    if not (shutil.which("g++") or shutil.which("clang++")):
        return False
    return shutil.which("pkg-config") is not None and (
        subprocess.run(["pkg-config", "--exists", "arrow", "parquet"]).returncode == 0
    )


def _pkg(*args: str) -> list[str]:
    return subprocess.run(["pkg-config", *args, "arrow", "parquet"], capture_output=True, text=True).stdout.split()


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    if not _toolchain_ok():
        pytest.skip("no C++ toolchain / Arrow / Parquet dev headers")
    cc = "g++" if shutil.which("g++") else "clang++"
    out = tmp_path_factory.mktemp("build_raii") / "build_raii_test"
    cmd = [
        cc, "-std=c++20", "-O1", "-g", "-fsanitize=address", "-fno-omit-frame-pointer",
        "-I", str(SSD), "-I", str(CPP_HELPERS), *_pkg("--cflags"),
        str(DRIVER), str(SSD / "db_loader.cpp"), *_pkg("--libs"),
        "-o", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if "sanitize" in proc.stderr.lower() and "unrecognized" in proc.stderr.lower():
            pytest.skip("compiler has no AddressSanitizer")
        pytest.fail(f"compile failed:\n{proc.stderr}")
    return out


def _run(driver: Path, mode: str, storage: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1")
    return subprocess.run([str(driver), mode, str(storage)], capture_output=True, text=True, env=env)


def test_build_is_exception_safe_under_oom(driver, tmp_path):
    # The always-leak-free path is the canary: if it fails, the sanitizer runtime cannot run here,
    # so skip rather than report a false leak.
    ok = _run(driver, "ok", tmp_path)
    if ok.returncode != 0:
        if any(m in ok.stderr for m in _SANITIZER_UNAVAILABLE):
            pytest.skip(f"sanitizer runtime unavailable in this environment:\n{ok.stderr}")
        pytest.fail(f"successful build()/destroy_database leaked or double-freed:\n{ok.stderr}")

    # With the sanitizer confirmed working, a throw mid-build must free the partial dataset.
    thrown = _run(driver, "throw", tmp_path)
    assert thrown.returncode == 0, (
        "build() leaked the partial dataset when it threw mid-build "
        f"(exit {thrown.returncode}):\n{thrown.stderr}"
    )
