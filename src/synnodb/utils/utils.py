import enum
import hashlib
import json
import logging
import os
import pickle
from pathlib import Path
from typing import Any, TypeVar

from synnodb.utils.json_utils import JsonEncoder

logger = logging.getLogger(__name__)


def parse_db_storage(s: str) -> "DBStorage":
    try:
        return DBStorage(s)
    except ValueError:
        raise ValueError(
            f"Invalid db storage: {s}. Valid options are: {[e.value for e in DBStorage]}"
        )


class DBStorage(str, enum.Enum):
    LABSTORE = "labstore"
    SSD = "ssd"
    IN_MEMORY = "in_memory"


def is_persistent_storage(db_storage: DBStorage) -> bool:
    """Whether the storage backend persists to disk (vs. purely in-memory)."""
    return db_storage in (DBStorage.LABSTORE, DBStorage.SSD)


class DataSource(str, enum.Enum):
    """How the queried data is physically represented for a run.

    - ``FLAT``: data loaded flat into memory (DuckDB's native materialized tables; the only
      representation available for an in-memory run).
    - ``PARQUET``: queries stream directly from parquet files on disk (DuckDB parquet views).
    - ``BESPOKE``: the bespoke engine's on-disk storage plan.

    This is part of the query-execution cache key: the DuckDB reference answer differs between
    materialized tables and parquet views, so it must not be shared across sources.
    """

    FLAT = "flat"
    PARQUET = "parquet"
    BESPOKE = "bespoke"


def storage_label(db_storage: DBStorage) -> str:
    """Canonical 'ssd' / 'in_memory' label, e.g. for debug-log paths."""
    return "ssd" if is_persistent_storage(db_storage) else "in_memory"


def get_disk_db_dir(
    db_storage: DBStorage, workspace_path: Path
) -> tuple[Path | None, Path | None]:
    if db_storage == DBStorage.LABSTORE:
        raise NotImplementedError(
            "LABSTORE storage is not supported in this codebase. Please use SSD or IN_MEMORY."
        )
        # disk_db_dir = Path("/mnt/labstore/bespoke_olap/dbs")
        # bespoke_db_dir = Path("/mnt/labstore/bespoke_olap/tmp")
    elif db_storage == DBStorage.SSD:
        disk_db_dir = Path(__file__).parent.parent / "dbs"
        bespoke_db_dir = workspace_path.absolute() / "tmp"
    elif db_storage == DBStorage.IN_MEMORY:
        disk_db_dir = None
        bespoke_db_dir = None
    else:
        raise ValueError(f"Unknown db source: {db_storage}")

    return disk_db_dir, bespoke_db_dir


def ask_yes_no(prompt: str, default: bool | None = None) -> bool:
    """
    Ask a yes/no question.

    - default=True  -> Enter means "yes"
    - default=False -> Enter means "no"
    - default=None  -> Enter not allowed, must type y/n
    """
    if default is True:
        suffix = " [Y/n] "
    elif default is False:
        suffix = " [y/N] "
    else:
        suffix = " [y/n] "

    while True:
        reply = input(prompt + suffix).strip().lower()

        if not reply:
            if default is not None:
                return default
            continue

        if reply in ("y", "yes"):
            return True
        if reply in ("n", "no"):
            return False


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, cls=JsonEncoder
    )


def atomic_write(path: Path, data: bytes, mode: int = 0o777) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic
    try:
        os.chmod(path, mode)
    except Exception:
        pass  # best effort, ignore failures


def create_dir_and_set_permissions(path: Path, mode: int = 0o777) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    except Exception:
        pass  # best effort, ignore failures


def create_parent_and_set_permissions(path: Path, mode: int = 0o777) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, mode)
    except Exception:
        pass  # best effort, ignore failures


def exclude_workspace_from_enclosing_repo(workspace_path: Path) -> None:
    """If a run workspace lives inside another git repo (e.g. the SynnoDB checkout),
    register it in that repo's ``.git/info/exclude`` so the generated engine — itself a
    nested git repo — is never accidentally ``git add``ed/pushed.

    Uses the local, uncommitted exclude file (not the tracked ``.gitignore``), so it
    works for any workspace name without polluting version control. Best-effort.
    """
    try:
        ws = Path(workspace_path).resolve()
        for (
            parent
        ) in ws.parents:  # nearest enclosing repo (workspace's own .git excluded)
            git_dir = parent / ".git"
            if git_dir.is_dir():
                rel = ws.relative_to(parent).as_posix()
                entry = f"/{rel}/"
                exclude_file = git_dir / "info" / "exclude"
                exclude_file.parent.mkdir(parents=True, exist_ok=True)
                existing = exclude_file.read_text() if exclude_file.exists() else ""
                if entry not in existing.split():
                    with exclude_file.open("a") as f:
                        if existing and not existing.endswith("\n"):
                            f.write("\n")
                        f.write(f"# synno run workspace (auto-added)\n{entry}\n")
                    logger.debug("Excluded run workspace %s in %s", entry, exclude_file)
                return  # only the nearest enclosing repo matters
    except Exception:
        pass  # best effort — never block a run on this


T = TypeVar("T")

# Map old module paths (before refactoring) to their new locations.
# Format: {"old.module.path": "new.module.path"}
PICKLE_MODULE_REMAP: dict[str, str] = {
    # Example: "old_package.module": "new_package.module",
    # "tools.validate_tool.query_cache": "pipeline.tools.validate.query_cache",
    # "pipeline.tools.validate_tool.query_cache": "pipeline.tools.validate.query_cache",
    "llm_cache.cached_litellm": "agents_sdk.llm.cached_litellm",
    "llm_cache.cached_openai": "agents_sdk.llm.cached_openai",
    "llm_cache.cached_compaction_session": "agents_sdk.llm.cached_compaction_session",
}


# Top-level packages that moved under the `synnodb.` namespace when the project
# became an installable package. Caches/snapshots pickled before that move embed
# the old paths (e.g. "utils.snapshot_utils"); prepend the namespace so they still
# resolve instead of forcing a cache wipe.
_PRE_NAMESPACE_TOP_LEVELS = (
    "utils.",
    "tools.",
    "conversations.",
    "workloads.",
    "cpp_runner.",
    "observability.",
    "synth_framework.",
    "llm.",
    "main.",
)


class _RemappingUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        module = PICKLE_MODULE_REMAP.get(module, module)
        if module.startswith(_PRE_NAMESPACE_TOP_LEVELS):
            module = "synnodb." + module
        try:
            return super().find_class(module, name)
        except ModuleNotFoundError as e:
            logger.error(f"Module not found during unpickling: {module} ({name})")
            raise e


def load_pickle(path: Path, expected: type[T]) -> T | None:
    """
    Load a pickled object from `path` and verify its type.

    Returns the object if it matches `expected`. On deserialization failure
    or type mismatch, the file is renamed with a `.corrupt` suffix and
    None is returned.

    Old module paths from before refactoring can be remapped via PICKLE_MODULE_REMAP.
    """
    try:
        import io

        obj = _RemappingUnpickler(io.BytesIO(path.read_bytes())).load()
        if isinstance(obj, expected):
            return obj
    except Exception as e:
        logger.exception(f"Failed to read from {path}: {e}")
        raise e


def dump_pickle(
    path: Path, obj: T, do_not_cache: bool, assert_not_exists: bool = True
) -> None:
    """
    Dumps an object to a pickle file at the given path.

    Args:
        path (Path): The file path where the object will be saved.
        obj (T): The object to pickle, can be any type.
    """

    assert not do_not_cache, (
        "dump_pickle should not be called when do_not_cache is True"
    )

    try:
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        if assert_not_exists:
            if path.exists():
                raise FileExistsError(f"File already exists: {path}")
        atomic_write(path, data)
    except Exception as e:
        logger.exception(f"Failed to write to {path}: {e}")
        raise e


def prefix_dict(d: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {f"{prefix}_{k}": v for k, v in d.items()}
