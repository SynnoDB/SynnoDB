import argparse
import logging
import pickle
import sys
from pathlib import Path

from pipeline.git_snapshotter import GitSnapshotter
from utils.logging_and_reporting.logger import setup_logging

logger = logging.getLogger(__name__)


valid_snapshots = set()


def inspect_pickle_file(git, path: Path) -> None:
    try:
        with path.open("rb") as f:
            obj = pickle.load(f)
    except Exception as e:
        logger.error(f"{path}: failed to load ({e})")
        return

    # key = path.stem
    # logger.info(key)
    # maintain previous behavior: print parent_hash and filename
    parent_hash = getattr(obj, "parent_hash", None)

    assert parent_hash not in valid_snapshots
    valid_snapshots.add(parent_hash)

    if not git.has_snapshot(parent_hash):
        return

    logger.info(f"{parent_hash} {path}")

    # mtime = path.stat().st_mtime
    # human_time = datetime.fromtimestamp(mtime)
    # print(human_time)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect pickle cache files")
    parser.add_argument(
        "directory", type=Path, help="Directory containing .pkl / .pickle files"
    )
    args = parser.parse_args()

    directory: Path = args.directory

    if not directory.exists():
        logger.error(f"{directory}: does not exist")
        sys.exit(1)
    if not directory.is_dir():
        logger.error(f"{directory}: is not a directory")
        sys.exit(1)

    workspace_path = Path("./output")
    git = GitSnapshotter(working_dir=workspace_path)

    for path in directory.iterdir():
        if path.suffix not in (".pkl", ".pickle"):
            continue
        if (
            "9f2821d7446705594b3a3dc963905aec149e84a6c84c1744b345c2545823c32c.pkl"
            not in str(path)
        ):
            continue
        inspect_pickle_file(git, path)

    # print(sum(1 for _ in git.iter_snapshots()))
    # print(len(valid_snapshots))


if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    main()
