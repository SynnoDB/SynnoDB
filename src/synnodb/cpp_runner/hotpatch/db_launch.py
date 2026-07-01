"""Build and locate the ``db_launch`` exec-in-place launcher binary.

``db_launch.cpp`` is a tiny standalone helper (see its header for what it does). It
is compiled once into a content-addressed cache and reused; the binary has no
runtime dependencies beyond libc, so a single build serves every runner.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_SRC = Path(__file__).with_name("db_launch.cpp")


def _cache_dir() -> Path:
    """Directory holding the compiled launcher, content-addressed by source hash.

    Overridable via ``SYNNO_LAUNCHER_CACHE`` (e.g. for read-only deployments)."""
    base = os.environ.get("SYNNO_LAUNCHER_CACHE")
    root = Path(base) if base else Path(tempfile.gettempdir()) / "synno_launcher"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_launch_binary() -> Path:
    """Return the path to the compiled ``db_launch`` binary, building it if needed.

    The binary is keyed by a hash of ``db_launch.cpp``, so editing the source yields
    a fresh build and a stale binary is never reused. Concurrent builders are safe:
    each writes a private temp file and atomically renames it into place.
    """
    src_bytes = _SRC.read_bytes()
    digest = hashlib.sha256(src_bytes).hexdigest()[:16]
    binary = _cache_dir() / f"db_launch.{digest}"
    if binary.exists():
        return binary

    cxx = os.environ.get("CXX", "g++")
    fd, tmp_name = tempfile.mkstemp(prefix="db_launch.", dir=str(binary.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            [cxx, "-O2", "-std=c++17", "-Wall", "-Wextra", "-o", str(tmp), str(_SRC)],
            check=True,
            capture_output=True,
            text=True,
        )
        tmp.chmod(0o755)
        os.replace(tmp, binary)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"failed to compile db_launch ({cxx}): {exc.stderr or exc.stdout}"
        ) from exc
    finally:
        if tmp.exists():
            tmp.unlink()
    logger.debug("built db_launch -> %s", binary)
    return binary
