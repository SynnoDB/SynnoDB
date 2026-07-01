from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import resource
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

# ----- Linux hardening helpers -----
libc = ctypes.CDLL("libc.so.6", use_errno=True)

PR_SET_NO_NEW_PRIVS = 38


def _prctl_set_no_new_privs() -> None:
    rc = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(e)}")


def _set_rlimits(
    *,
    cpu_seconds: int | None = 10,
    as_bytes: int | None = 512 * 1024 * 1024,  # address space (rough memory cap)
    fsize_bytes: int | None = 50 * 1024 * 1024,  # max size of any single file created
    nofile: int | None = 256,
    nproc: int | None = 128,
) -> None:
    # Only set limits that are not None
    if cpu_seconds is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if as_bytes is not None:
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    if fsize_bytes is not None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    if nofile is not None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    if nproc is not None:
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))


@dataclass(frozen=True)
class SandboxConfig:
    writable_roots: Sequence[str]
    cwd: str | None = None

    # rlimits
    cpu_seconds: int | None = 10
    as_bytes: int | None = 512 * 1024 * 1024
    fsize_bytes: int | None = 50 * 1024 * 1024
    nofile: int | None = 256
    nproc: int | None = 128

    # misc
    umask: int = 0o077  # files created inside writable roots are private by default
    readonly_files: Sequence[
        Path
    ] = ()  # individual files to make non-writable before exec

    def normalized(self) -> "SandboxConfig":
        roots = [str(Path(p).resolve()) for p in self.writable_roots]
        cwd = str(Path(self.cwd).resolve()) if self.cwd else None
        readonly = [p.resolve() for p in self.readonly_files]
        return SandboxConfig(
            writable_roots=roots,
            cwd=cwd,
            cpu_seconds=self.cpu_seconds,
            as_bytes=self.as_bytes,
            fsize_bytes=self.fsize_bytes,
            nofile=self.nofile,
            nproc=self.nproc,
            umask=self.umask,
            readonly_files=readonly,
        )


def _apply_sandbox(cfg: SandboxConfig) -> None:
    """
    Runs in the *child* right before exec.

    Policy:
      - Reads are NOT restricted by Landlock (normal Unix perms apply)
      - Writes are denied everywhere except cfg.writable_roots
    """
    if sys.platform != "linux":
        raise RuntimeError("This sandbox is Linux-only")

    from landlock import Ruleset

    _prctl_set_no_new_privs()
    _set_rlimits(
        cpu_seconds=cfg.cpu_seconds,
        as_bytes=cfg.as_bytes,
        fsize_bytes=cfg.fsize_bytes,
        nofile=cfg.nofile,
        nproc=cfg.nproc,
    )

    rs = Ruleset()

    # Handle only write-ish operations so reads remain unaffected.
    # Different versions of the library expose different knobs; we probe them.
    #
    # Preferred: a method like rs.handle_write() / rs.restrict_writes()
    if hasattr(rs, "handle_write"):
        rs.handle_write()
    elif hasattr(rs, "restrict_writes"):
        rs.restrict_writes()
    else:
        # Next best: pass explicit accesses to allow(..., access=...)
        # We'll build the "write set" from whatever the library exports.
        write_access = None

        # Common patterns: landlock has an enum/bitmask for FS access
        # Try a few likely attribute names.
        for name in ("AccessFS", "FSAccess", "Access", "FS"):
            if hasattr(__import__("landlock"), name):
                write_access = getattr(__import__("landlock"), name)
                break

        if write_access is None:
            raise RuntimeError(
                "landlock package API not recognized. "
                'Run: python -c "import landlock; print(dir(landlock))" '
                "and adapt mapping for your version."
            )

        # Collect likely write-ish flags if present on the enum/namespace.
        # (The package should ignore absent ones; we only OR what exists.)
        write_names = [
            "WRITE_FILE",
            "TRUNCATE",
            "MAKE_REG",
            "MAKE_DIR",
            "MAKE_SYM",
            "MAKE_FIFO",
            "MAKE_SOCK",
            "MAKE_CHAR",
            "MAKE_BLOCK",
            "REMOVE_FILE",
            "REMOVE_DIR",
            "REFER",
        ]
        try:
            mask = write_access(0)
            _mask_is_enum = True
        except Exception:
            mask = 0
            _mask_is_enum = False
        for n in write_names:
            if hasattr(write_access, n):
                v = getattr(write_access, n)
                mask |= v if _mask_is_enum else int(v)

        if mask == 0:
            raise RuntimeError(
                "Could not build a write-access mask from landlock's exported flags."
            )

        try:
            rs = Ruleset(restrict_rules=mask)
        except TypeError as exc:
            raise RuntimeError(
                "landlock Ruleset does not support restrict_rules; cannot build write-only ruleset."
            ) from exc

        # Allow write mask only under writable roots
        for root in cfg.writable_roots:
            try:
                rs.allow(root, rules=mask)
            except TypeError:
                rs.allow(root, access=mask)

        rs.apply()
        os.umask(cfg.umask)
        return

    # If the library provides a "handle write" mode,
    # rs.allow(...) typically allows the handled operations within the path.
    for root in cfg.writable_roots:
        rs.allow(root)

    rs.apply()
    os.umask(cfg.umask)


# ----- helpers -----


def _readonly_ctx(paths: Sequence[Path]) -> contextlib.AbstractContextManager[None]:
    """Save write bits, strip them, restore unconditionally on exit."""

    @contextlib.contextmanager
    def _ctx() -> Iterator[None]:
        saved = {p: os.stat(p).st_mode for p in paths if p.exists()}
        for p, mode in saved.items():
            os.chmod(p, mode & ~0o222)
        try:
            yield
        finally:
            # Restore every path independently: if one chmod fails (e.g. the
            # file was deleted/renamed during the run) the remaining files must
            # still get their write bits back, and the restore error must not
            # mask the original exception propagating out of the `yield`.
            for p, mode in saved.items():
                try:
                    os.chmod(p, mode)
                except OSError:
                    logger.warning("Failed to restore permissions on %s", p, exc_info=True)

    return _ctx()


# ----- Sync: subprocess.Popen -----


@contextlib.contextmanager
def sandbox_popen(
    args: Sequence[str],
    *,
    cfg: SandboxConfig,
    stdin=None,
    stdout=None,
    stderr=None,
    env: Mapping[str, str] | None = None,
    text: bool = False,
) -> Iterator[subprocess.Popen]:
    cfg = cfg.normalized()

    def _child_setup():
        _apply_sandbox(cfg)
        if cfg.cwd:
            os.chdir(cfg.cwd)

    with _readonly_ctx(cfg.readonly_files):
        # NOTE: preexec_fn is not recommended in a heavily-threaded parent.
        proc = subprocess.Popen(
            list(args),
            preexec_fn=_child_setup,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=None if env is None else dict(env),
            text=text,
            close_fds=True,
        )
        try:
            yield proc
        finally:
            proc.wait()


# ----- Async: asyncio.create_subprocess_* -----
#
# asyncio does NOT support preexec_fn. So we run through a small Python launcher
# which applies the sandbox, then execs the target command.
#
# This also works for shell mode by execing: /bin/sh -lc "<your shell string>"
#
_LAUNCHER_CODE = r"""
import json, os, sys, ctypes, resource

libc = ctypes.CDLL("libc.so.6", use_errno=True)
PR_SET_NO_NEW_PRIVS = 38

def prctl_no_new_privs():
    rc = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(e)}")

def set_rlimits(rl):
    def setlim(which, v):
        if v is None:
            return
        resource.setrlimit(which, (v, v))
    setlim(resource.RLIMIT_CPU, rl.get("cpu_seconds"))
    setlim(resource.RLIMIT_AS, rl.get("as_bytes"))
    setlim(resource.RLIMIT_FSIZE, rl.get("fsize_bytes"))
    setlim(resource.RLIMIT_NOFILE, rl.get("nofile"))
    setlim(resource.RLIMIT_NPROC, rl.get("nproc"))

def apply_landlock(writable_roots):
    if sys.platform != "linux":
        raise RuntimeError("This sandbox is Linux-only")

    from landlock import Ruleset

    rs = Ruleset()
    if hasattr(rs, "handle_write"):
        rs.handle_write()
        for root in writable_roots:
            rs.allow(root)
        rs.apply()
        return

    if hasattr(rs, "restrict_writes"):
        rs.restrict_writes()
        for root in writable_roots:
            rs.allow(root)
        rs.apply()
        return

    import landlock as _ll

    write_access = None
    for name in ("AccessFS", "FSAccess", "Access", "FS"):
        if hasattr(_ll, name):
            write_access = getattr(_ll, name)
            break

    if write_access is None:
        raise RuntimeError(
            "landlock package API not recognized. "
            'Run: python -c "import landlock; print(dir(landlock))" '
            "and adapt mapping for your version."
        )

    write_names = [
        "WRITE_FILE",
        "TRUNCATE",
        "MAKE_REG",
        "MAKE_DIR",
        "MAKE_SYM",
        "MAKE_FIFO",
        "MAKE_SOCK",
        "MAKE_CHAR",
        "MAKE_BLOCK",
        "REMOVE_FILE",
        "REMOVE_DIR",
        "REFER",
    ]
    try:
        mask = write_access(0)
        _mask_is_enum = True
    except Exception:
        mask = 0
        _mask_is_enum = False
    for n in write_names:
        if hasattr(write_access, n):
            v = getattr(write_access, n)
            mask |= v if _mask_is_enum else int(v)

    if mask == 0:
        raise RuntimeError(
            "Could not build a write-access mask from landlock's exported flags."
        )

    try:
        rs = Ruleset(restrict_rules=mask)
    except TypeError as exc:
        raise RuntimeError(
            "landlock Ruleset does not support restrict_rules; cannot build write-only ruleset."
        ) from exc

    for root in writable_roots:
        try:
            rs.allow(root, rules=mask)
        except TypeError:
            rs.allow(root, access=mask)
    rs.apply()

def main():
    payload = json.loads(sys.argv[1])

    prctl_no_new_privs()
    set_rlimits(payload.get("rlimits", {}))
    apply_landlock(payload["writable_roots"])
    os.umask(payload.get("umask", 0o077))

    cwd = payload.get("cwd")
    if cwd:
        os.chdir(cwd)

    argv = payload["argv"]
    os.execvpe(argv[0], argv, payload.get("env") or os.environ)

if __name__ == "__main__":
    main()
""".strip()


def _launcher_argv(
    argv: Sequence[str],
    *,
    cfg: SandboxConfig,
    env: Mapping[str, str] | None,
) -> list[str]:
    cfg = cfg.normalized()
    payload = {
        "writable_roots": list(cfg.writable_roots),
        "cwd": cfg.cwd,
        "umask": cfg.umask,
        "rlimits": {
            "cpu_seconds": cfg.cpu_seconds,
            "as_bytes": cfg.as_bytes,
            "fsize_bytes": cfg.fsize_bytes,
            "nofile": cfg.nofile,
            "nproc": cfg.nproc,
        },
        "argv": list(argv),
        "env": None if env is None else dict(env),
    }
    return [sys.executable, "-c", _LAUNCHER_CODE, json.dumps(payload)]


@contextlib.asynccontextmanager
async def sandbox_exec_async(
    *argv: str,
    cfg: SandboxConfig,
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    cfg = cfg.normalized()
    launcher = _launcher_argv(argv, cfg=cfg, env=env)
    with _readonly_ctx(cfg.readonly_files):
        proc = await asyncio.create_subprocess_exec(
            *launcher,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            yield proc
        finally:
            if proc.returncode is None:
                await proc.wait()


@contextlib.asynccontextmanager
async def sandbox_shell_async(
    command: str,
    *,
    cfg: SandboxConfig,
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """
    Replacement for: asyncio.create_subprocess_shell(command, ...)
    while enforcing the sandbox.

    We exec: /bin/sh -c <command> inside the sandbox.
    """
    cfg = cfg.normalized()
    shell_argv = ("/bin/sh", "-c", command)
    launcher = _launcher_argv(shell_argv, cfg=cfg, env=env)
    with _readonly_ctx(cfg.readonly_files):
        proc = await asyncio.create_subprocess_exec(
            *launcher,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            yield proc
        finally:
            if proc.returncode is None:
                await proc.wait()
