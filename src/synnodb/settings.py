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

# Default W&B coordinates, used when WANDB_ENTITY/WANDB_PROJECT are not set in
# the environment/.env. Kept here so writer (wandb.init) and all readers resolve
# the same destination — a mismatch silently sends runs somewhere they can't be
# read back from.
#
# The entity default is intentionally None: a hardcoded entity (e.g. one user's
# personal team) is unreadable by everyone else. Leaving it unset lets W&B map
# to *each user's own* default entity — wandb.init(entity=None), weave.init with
# a bare project name, and api.run("project/run_id") all resolve the caller's
# default entity server-side. Users who want a specific team set WANDB_ENTITY.
DEFAULT_WANDB_ENTITY: str | None = None
DEFAULT_WANDB_PROJECT = "SynnoDB"


def get_wandb_entity_project(
    entity: str | None = None, project: str | None = None
) -> tuple[str | None, str]:
    """Resolve the W&B ``(entity, project)`` once, from args → env → defaults.

    Explicit arguments win; otherwise fall back to ``WANDB_ENTITY`` /
    ``WANDB_PROJECT`` from the environment (or ``.env``), then the project
    defaults above. This is the single source of truth — every wandb write and
    read should resolve through here so they always agree.

    ``entity`` may be ``None`` (the default): callers must treat that as "let
    W&B pick the user's default entity" rather than substituting a literal.
    """
    if entity is None or project is None:
        load_dotenv()  # harmless if already loaded; lets .env work without configure()
        entity = entity or os.getenv("WANDB_ENTITY", DEFAULT_WANDB_ENTITY)
        project = project or os.getenv("WANDB_PROJECT", DEFAULT_WANDB_PROJECT)
    return entity, project


def wandb_logging_enabled(
    entity: str | None = None, project: str | None = None
) -> bool:
    """Whether W&B logging should run for this configuration.

    W&B is opt-in and has no separate on/off flag: it is enabled iff an entity or
    project is supplied - either explicitly (``entity``/``project`` here) or via
    ``WANDB_ENTITY``/``WANDB_PROJECT`` in the environment (or ``.env``). The
    single source of truth for *that decision*, shared by the CLI and the Python
    API so ``.env`` behaves identically on both.

    The project *default* (``DEFAULT_WANDB_PROJECT``) deliberately does NOT count
    as opting in - otherwise every run would log. Only an explicitly provided
    entity/project or a set env var turns W&B on; ``get_wandb_entity_project``
    then resolves *where* the enabled run logs.
    """
    if entity is not None or project is not None:
        return True
    load_dotenv()  # harmless if already loaded; lets .env opt in without configure()
    return bool(os.getenv("WANDB_ENTITY") or os.getenv("WANDB_PROJECT"))


def configure(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    engines_dir: str | os.PathLike[str] | None = None,
    env_file: str | None = None,
) -> None:
    """Explicit, idempotent process configuration.

    Sets the process-wide folders SynnoDB derives everything else from. Each may
    instead come from ``.env``/the environment (``SYNNO_DATA_DIR``,
    ``SYNNO_ENGINES_DIR``); an explicit argument here wins over that. A second,
    conflicting ``data_dir`` is a fail-fast error rather than a silent clobber.

    ``engines_dir`` is left to default to ``<data_dir>/engines`` when unset - it
    is resolved lazily by ``resolve_engines_dir`` - so configuring only
    ``data_dir`` is enough for the common case.
    """
    global _our_data_dir
    load_dotenv(env_file)
    if data_dir is not None:
        data_dir = os.fspath(data_dir)  # accept Path/PathLike, store as str
        if _our_data_dir is not None and _our_data_dir != data_dir:
            raise RuntimeError(
                f"SynnoDB already configured with data_dir={_our_data_dir!r}; "
                f"refusing to reconfigure to {data_dir!r}."
            )
        os.environ["SYNNO_DATA_DIR"] = data_dir
        _our_data_dir = data_dir
    if engines_dir is not None:
        os.environ["SYNNO_ENGINES_DIR"] = os.fspath(engines_dir)
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


def get_snapshotter_dir() -> Path | None:
    """The single shared git-snapshotter repository (one bare repo for all workspaces).

    By default it lives at ``<data_dir>/git_snapshotter`` so snapshots sit in the
    SynnoDB data folder (shared across workspaces and users) instead of inside
    each run's workspace. Set ``SYNNO_SNAPSHOTTER_DIR`` to override that location
    (e.g. point it at a faster local disk). Returns ``None`` only when neither the
    override nor ``SYNNO_DATA_DIR`` is configured, letting a standalone snapshotter
    fall back to a workspace-local ``.git``.
    """
    load_dotenv()  # harmless if already loaded; lets .env work without configure()
    override = os.getenv("SYNNO_SNAPSHOTTER_DIR")
    if override:
        return Path(override)
    try:
        return get_data_dir() / "git_snapshotter"
    except RuntimeError:
        return None


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    return _mkdir(get_data_dir() / "logs" / "logfiles")


def duckdb_drain_dir() -> Path:
    return _mkdir(get_data_dir() / "logs" / "duckdb")
