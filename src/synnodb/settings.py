"""Process configuration for SynnoDB — the single place env/paths are resolved.

Resolution is lazy: importing the package needs no configuration. The data dir
and derived paths are computed on first use (and cached), so a module import
never asserts or touches the filesystem.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The data_dir *we* configured programmatically, used to detect a conflicting
# reconfigure (two SynnoDB instances pointed at different dirs in one process).
_our_data_dir: str | None = None


def configure(*, data_dir: str | None = None, env_file: str | None = None) -> None:
    """Explicit, idempotent process configuration.

    An explicit ``data_dir`` wins over a value from ``.env``/the environment. A
    second, conflicting ``configure()`` is a fail-fast error rather than a silent
    clobber.
    """
    global _our_data_dir
    load_dotenv(env_file)
    if data_dir is not None:
        if _our_data_dir is not None and _our_data_dir != data_dir:
            raise RuntimeError(
                f"SynnoDB already configured with data_dir={_our_data_dir!r}; "
                f"refusing to reconfigure to {data_dir!r}."
            )
        os.environ["SYNNO_DATA_DIR"] = data_dir
        _our_data_dir = data_dir
    get_data_dir.cache_clear()


@lru_cache(maxsize=1)
def get_data_dir() -> Path:
    """The SynnoDB data root (caches, logs, conversations, workloads)."""
    load_dotenv()  # harmless if already loaded; lets .env work without configure()
    value = os.getenv("SYNNO_DATA_DIR")
    if not value:
        raise RuntimeError(
            "SYNNO_DATA_DIR is not set. Export it, put it in .env, or pass "
            "data_dir=... (e.g. SynnoDB(data_dir=...))."
        )
    return Path(value)


def get_workspace_dir(override: str | None = None) -> Path:
    """The run's git-tracked output directory and local snapshot cache.

    Must be a **relative** path: the framework folds the workspace path into
    cache keys and asserts relativity so caches stay portable across
    machines/users. It is therefore resolved against the cwd — now explicit and
    overridable via ``override`` or ``SYNNO_WORKSPACE`` (default ``output``)
    instead of a hard-coded ``"./output"``. Keep it stable across runs from a
    given directory to reuse the local snapshot cache.
    """
    value = override or os.getenv("SYNNO_WORKSPACE") or "output"
    path = Path(value)
    if path.is_absolute():
        raise ValueError(
            f"workspace must be a relative path (got {value!r}); the framework "
            "requires this for cross-machine cache portability."
        )
    return path


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    return _mkdir(get_data_dir() / "logs" / "logfiles")


def duckdb_drain_dir() -> Path:
    return _mkdir(get_data_dir() / "logs" / "duckdb")
