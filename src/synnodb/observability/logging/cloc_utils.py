import json
import logging
import pickle
import subprocess
from pathlib import Path

from utils import utils

logger = logging.getLogger(__name__)


def calculate_loc(
    cloc_cache_dir: Path | None,
    current_hash: str,
    working_dir: Path,
    do_not_cache: bool,
) -> int:
    count_stats = None
    if cloc_cache_dir is not None:
        # check if cloc is in cache
        payload = {
            "snapshot_hash": current_hash,
        }
        hash = utils.sha256(utils.stable_json(payload))
        cache_path = _cache_path_for(cloc_cache_dir, hash)

        if cache_path.exists():
            # we manually check filetypes - manual pickle invocation (not using utils.load_pickle)
            count_stats = pickle.load(cache_path.open("rb"))
            assert count_stats is not None, "Cache file exists but failed to load"
    else:
        cache_path = None

    if count_stats is None:
        # run cloc with json output
        cmd = "cloc . --json"

        # execute the command with subprocess and capture the output
        result = subprocess.run(
            cmd, shell=True, cwd=working_dir, capture_output=True, text=True
        )

        # check for error
        if result.returncode != 0:
            logger.error(f"Error running cloc: {result.stderr}")
            return 0

        count_stats = result.stdout.strip()
        count_stats = json.loads(count_stats)

        assert count_stats, (
            "cloc output is empty. Failed to count LOC. Error: {result.stderr}"
        )

        if cache_path is not None and not do_not_cache:
            # write out to cache
            utils.dump_pickle(cache_path, count_stats, do_not_cache=do_not_cache)

    assert isinstance(count_stats, dict), (
        f"cloc output is not a dict. Instead: {type(count_stats)}"
    )

    # sum up the number of lines for the different file types
    loc = 0
    have_seen_file_types = set()
    for file_type, stats in count_stats.items():
        # skip general cloc files
        if file_type in ("SUM", "header", "SUM!"):
            continue

        # skip text / json files
        if file_type in ("Text", "JSON", "Markdown", "D", "CSV"):
            continue

        # accumulate lines of code for each file type
        have_seen_file_types.add(file_type)
        loc += stats.get("code", 0)  # only count code (ignore comments and blank lines)

    expected_filetypes = {"C++", "C/C++ Header"}
    if not have_seen_file_types.issubset(expected_filetypes):
        logger.warning(
            f"Encountered unexpected file types in cloc output: {have_seen_file_types}. Expected only: {expected_filetypes}. LOC count may be inaccurate."
        )

    return loc


def _cache_path_for(cloc_cache_dir: Path, hash: str) -> Path:
    assert cloc_cache_dir is not None, "cloc_cache_dir must be set to use cache"
    return cloc_cache_dir / f"{hash}.pkl"
