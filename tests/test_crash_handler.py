"""The engine crash handler (cpp_helpers/crash_handler.hpp) must turn a fatal signal into an
actionable, LLM-readable stack trace instead of a bare "killed by signal 11".

A SIGSEGV inside a generated run_qN escapes the per-query try/catch and kills the engine
child; before this handler the only feedback was the parent's signal number with no function,
file, or line - which cost the SF50 Q10 run hours. These tests prove, by compiling and running
the real header, that a crash now emits a block carrying the signal, the running query, and a
backtrace, and that the validator's symbolizer resolves a frame back to file:line.

Skips without a C++ toolchain.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from synnodb.tools.validate.query_validator_class import _symbolize_crash_backtrace

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER = REPO / "tests" / "cpp" / "crash_handler_test.cpp"


@pytest.fixture(scope="module")
def crash_run(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("no C++ toolchain")
    out = tmp_path_factory.mktemp("crash") / "crash_handler_test"
    # -O0 keeps the null deref a real load (not folded to a trap); -g + -rdynamic give the
    # handler real symbols, matching how the engine `db` binary is linked.
    compile_cmd = [
        "g++",
        "-std=c++20",
        "-O0",
        "-g",
        "-rdynamic",
        "-I",
        str(CPP_HELPERS),
        str(DRIVER),
        "-o",
        str(out),
    ]
    proc = subprocess.run(compile_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"compile failed:\n{proc.stderr}")
    run = subprocess.run([str(out)], capture_output=True, text=True)
    return out, run


def test_process_dies_by_signal(crash_run):
    """The handler re-raises, so the parent still observes the original SIGSEGV."""
    _, run = crash_run
    assert run.returncode == -signal.SIGSEGV


def test_crash_block_is_present_and_named(crash_run):
    """stderr carries the crash banner, the signal, and the running-query context."""
    _, run = crash_run
    err = run.stderr
    assert "SynnoDB engine CRASH" in err
    assert "SIGSEGV" in err
    # The query tag set via set_query_context() must name the offending query.
    assert "run #7 Q42(test-context)" in err


def test_backtrace_names_the_faulting_function(crash_run):
    """The raw backtrace references the function that faulted (mangled symbol is fine)."""
    _, run = crash_run
    # crash_handler.hpp emits "<module>(+0x<off>) <mangled_symbol>"; the mangled name of
    # crashing_query_body contains its identifier.
    assert "crashing_query_body" in run.stderr


def test_symbolizer_resolves_to_file_and_line(crash_run):
    """The validator's symbolizer turns the raw frames into function + file:line."""
    if not shutil.which("addr2line"):
        pytest.skip("no addr2line")
    _, run = crash_run
    symbolized = _symbolize_crash_backtrace(run.stderr)
    assert "Resolved crash frames" in symbolized
    assert "crashing_query_body" in symbolized
    assert "crash_handler_test.cpp" in symbolized


def test_no_crash_block_returns_empty():
    """Plain stderr with no crash block yields no symbolization."""
    assert _symbolize_crash_backtrace("just some normal stderr\n") == ""
