import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from tools.sandbox import (
    SandboxConfig,
    sandbox_exec_async,
    sandbox_popen,
    sandbox_shell_async,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux-only sandbox (Landlock)"
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _have_landlock() -> bool:
    try:
        import landlock  # noqa: F401

        return True
    except Exception:
        return False


def _sandbox_available_or_skip():
    """
    Skip if landlock isn't installed or Landlock can't be applied on this kernel.
    We probe by launching a tiny process that applies the sandbox and exits.
    """
    if not _have_landlock():
        pytest.skip("landlock not installed")

    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from pipeline.tools.sandbox import SandboxConfig, _apply_sandbox

        d = sys.argv[1]
        _apply_sandbox(SandboxConfig(writable_roots=[d]).normalized())
        sys.exit(0)
        """
    ).strip()

    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(
            [sys.executable, "-c", probe, d], capture_output=True, text=True
        )
    if r.returncode != 0:
        pytest.skip(
            f"Landlock sandbox not supported/enabled here: {r.stderr.strip() or r.stdout.strip()}"
        )


@pytest.fixture(autouse=True)
def _skip_if_no_landlock():
    _sandbox_available_or_skip()


@pytest.fixture
def rw_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def other_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def test_popen_allows_write_in_writable_roots_and_denies_elsewhere(rw_dir, other_dir):
    allowed = os.path.join(rw_dir, "ok.txt")
    denied = os.path.join(other_dir, "no.txt")

    code = textwrap.dedent(
        f"""
        import sys, os
        def w(p):
            try:
                with open(p, "wb") as f:
                    f.write(b"x")
                return True
            except OSError as e:
                print("FAIL", p, getattr(e, "errno", None), type(e).__name__)
                return False

        a = w({allowed!r})
        b = w({denied!r})
        sys.exit(0 if (a and not b) else 2)
        """
    ).strip()

    with sandbox_popen(
        [sys.executable, "-c", code],
        cfg=SandboxConfig(
            writable_roots=[rw_dir], cwd=rw_dir, cpu_seconds=2, nproc=None
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as p:
        out, err = p.communicate(timeout=10)

    assert p.returncode == 0, f"stdout:\n{out}\n\nstderr:\n{err}"
    assert os.path.exists(allowed)
    assert not os.path.exists(denied)


def test_popen_denies_tmp_write_by_default(rw_dir):
    code = textwrap.dedent(
        """
        import os, sys
        p = "/tmp/landlock_tmp_should_fail.txt"
        try:
            with open(p, "wb") as f:
                f.write(b"nope")
            print("UNEXPECTED_OK", p)
            sys.exit(3)
        except OSError as e:
            print("EXPECTED_FAIL", getattr(e, "errno", None), type(e).__name__)
            sys.exit(0)
        """
    ).strip()

    with sandbox_popen(
        [sys.executable, "-c", code],
        cfg=SandboxConfig(
            writable_roots=[rw_dir], cwd=rw_dir, cpu_seconds=2, nproc=None
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as p:
        out, err = p.communicate(timeout=10)
    assert p.returncode == 0, f"stdout:\n{out}\n\nstderr:\n{err}"


@pytest.mark.asyncio
async def test_async_shell_allows_write_in_workspace_and_denies_tmp(rw_dir):
    cfg = SandboxConfig(writable_roots=[rw_dir], cwd=rw_dir, cpu_seconds=2, nproc=None)

    async with sandbox_shell_async(
        "echo hi > ok.txt; echo nope > /tmp/landlock_should_fail_async.txt",
        cfg=cfg,
    ) as proc:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)

    # The shell will return nonzero because the second redirect should fail.
    assert proc.returncode != 0

    # Verify the allowed write happened.
    assert os.path.exists(os.path.join(rw_dir, "ok.txt"))
    # Verify /tmp write did not happen (best-effort: file shouldn't exist)
    assert not os.path.exists("/tmp/landlock_should_fail_async.txt")

    # Helpful debug on failure
    if err:
        _ = err.decode(errors="replace")


@pytest.mark.asyncio
async def test_async_exec_spawning_child_inherits_restrictions(rw_dir, other_dir):
    """
    Verify that forking/spawning a subprocess doesn't break out: the child is still restricted.
    We run a python that spawns another python, which tries a denied write.
    """

    denied = os.path.join(other_dir, "nope.txt")

    inner = textwrap.dedent(
        f"""
        import sys
        try:
            open({denied!r}, "wb").write(b"x")
            print("INNER_UNEXPECTED_OK")
            sys.exit(5)
        except OSError as e:
            print("INNER_EXPECTED_FAIL", getattr(e, "errno", None), type(e).__name__)
            sys.exit(0)
        """
    ).strip()

    outer = textwrap.dedent(
        f"""
        import subprocess, sys, textwrap
        inner = {inner!r}
        r = subprocess.run([sys.executable, "-c", inner], capture_output=True, text=True)
        print("INNER_RC", r.returncode)
        print(r.stdout)
        print(r.stderr)
        sys.exit(0 if r.returncode == 0 else 6)
        """
    ).strip()

    cfg = SandboxConfig(writable_roots=[rw_dir], cwd=rw_dir, cpu_seconds=5, nproc=None)

    async with sandbox_exec_async(sys.executable, "-c", outer, cfg=cfg) as proc:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)

    assert proc.returncode == 0, (
        f"stdout:\n{(out or b'').decode(errors='replace')}\n\nstderr:\n{(err or b'').decode(errors='replace')}"
    )
    assert not os.path.exists(denied)


def test_popen_readonly_files_cannot_be_written(rw_dir):
    """
    A file inside writable_roots but listed in readonly_files must be
    write-protected for the duration of the sandboxed process.  _readonly_ctx
    strips the write bits before exec and restores them afterwards.
    """
    from pathlib import Path as _Path

    protected = os.path.join(rw_dir, "protected.txt")
    _Path(protected).write_text("original", encoding="utf-8")

    code = textwrap.dedent(
        f"""
        import sys
        try:
            with open({protected!r}, "wb") as f:
                f.write(b"overwritten")
            print("UNEXPECTED_OK")
            sys.exit(3)
        except OSError as e:
            print("EXPECTED_FAIL", getattr(e, "errno", None), type(e).__name__)
            sys.exit(0)
        """
    ).strip()

    with sandbox_popen(
        [sys.executable, "-c", code],
        cfg=SandboxConfig(
            writable_roots=[rw_dir],
            cwd=rw_dir,
            cpu_seconds=2,
            nproc=None,
            readonly_files=[_Path(protected)],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as p:
        out, err = p.communicate(timeout=10)

    assert p.returncode == 0, f"stdout:\n{out}\n\nstderr:\n{err}"
    # File content must be unchanged
    assert _Path(protected).read_text(encoding="utf-8") == "original"
    # Write bits must be restored after the context manager exits
    assert os.access(protected, os.W_OK)


def test_no_new_privs_is_set(rw_dir):
    """
    Verify PR_SET_NO_NEW_PRIVS is in effect inside the sandboxed process.
    We check /proc/self/status contains NoNewPrivs: 1
    """

    code = textwrap.dedent(
        """
        import sys
        s = open("/proc/self/status", "r", encoding="utf-8", errors="replace").read()
        # line looks like: "NoNewPrivs:\t1"
        ok = any(line.startswith("NoNewPrivs:") and line.strip().endswith("1") for line in s.splitlines())
        sys.exit(0 if ok else 9)
        """
    ).strip()

    with sandbox_popen(
        [sys.executable, "-c", code],
        cfg=SandboxConfig(
            writable_roots=[rw_dir], cwd=rw_dir, cpu_seconds=2, nproc=None
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as p:
        out, err = p.communicate(timeout=10)
    assert p.returncode == 0, f"stdout:\n{out}\n\nstderr:\n{err}"
