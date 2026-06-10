import logging
import re
from pathlib import Path

from cpp_runner.prepare_repo.assemble_query_and_args import build_query_and_args_files
from cpp_runner.prepare_repo.assemble_query_files import build_query_files
from cpp_runner.prepare_repo.assemble_template_files import build_template_files
from synth_framework.git_snapshotter import GitSnapshotter
from utils import utils
from utils.utils import DBStorage

logger = logging.getLogger(__name__)


class PrepareCacheType:
    def __init__(
        self,
        hash_payload: str,
        snapshot_hash: str,
    ):
        self.hash_payload = hash_payload
        self.snapshot_hash = snapshot_hash


def extract_version_id(
    file_path: Path | None, content: str | None, must_be_version: bool = False
) -> tuple[str | None, str | None]:
    if content is None:
        assert file_path is not None, "Either file_path or content must be provided"
        content = file_path.read_text()

    # apply regex
    file_version_regex = r"// FILE_VERSION: ([0-9]+)"
    match = re.search(file_version_regex, content)

    if must_be_version:
        assert match, (
            f"Expected to find version string in {file_path}. Ensure the file contains a line like '// FILE_VERSION: 123'. E.g. file was marked as read-only, then requires such a version string to be used in cache keys / ..."
        )

    if match:
        version = match.group(1)
        return version, None

    else:
        return None, content


def get_art_str(artifacts: dict[str, str], must_be_version: bool = False) -> str:
    # extract version str for each file: if found add the version, if not, add the full file content
    artifacts_str = []
    for name, content in sorted(artifacts.items()):
        file_version, content_without_version = extract_version_id(
            file_path=None, content=content, must_be_version=must_be_version
        )

        if file_version is not None:
            artifacts_str.append(
                f"// ---- {name} ----\n// FILE_VERSION: {file_version}"
            )
        else:
            artifacts_str.append(f"// ---- {name} ----\n{content_without_version}")

    # Stable string for cache key and LLM context
    return "\n\n".join(artifacts_str)


def prepare_repo(
    workspace_dir: Path,
    benchmark: str,
    storage_plan: str | None,
    query_list: list[str],
    sql_dict: dict[str, str],
    gen_placeholders_fn,
    db_storage: utils.DBStorage,
    git_snapshotter: GitSnapshotter | None = None,
    cache_dir: Path | None = None,
    do_not_cache: bool = True,
    readonly_files_not_git_tracked: set[str] = set(),
    write_non_tracked_only: bool = False,
    add_thread_pool_to_query_impl: bool = False,
    only_query_txt: bool = False,
    only_from_cache: bool = False,
) -> str:
    # Step 1: Build artifacts dict without writing files
    template_files = build_template_files(
        benchmark,
        db_storage=db_storage,
    )

    query_and_args_files = build_query_and_args_files(
        benchmark_name=benchmark,
        gen_placeholders_fn=gen_placeholders_fn,
        query_list=query_list,
        storage_plan=storage_plan,
        query_impl_content=template_files.pop("query_impl.cpp"),
        drop_os_caches_for_each_query=db_storage in [DBStorage.SSD, DBStorage.LABSTORE],
        add_thread_pool_to_query_impl=add_thread_pool_to_query_impl,
    )

    query_source_files = build_query_files(
        query_list=query_list,
        sql_dict=sql_dict,
    )

    # Merge: query_and_args_files overrides template_files for query_impl.cpp
    artifacts: dict[str, str] = {}
    artifacts.update(query_and_args_files)
    artifacts.update(query_source_files)
    artifacts.update(template_files)

    if only_query_txt:
        assert "queries.txt" in artifacts, (
            "queries.txt must be generated to use only_query_txt mode"
        )
        artifacts = {"queries.txt": artifacts["queries.txt"]}

    # split into read-only and regular files
    ro_artifacts: dict[str, str] = {}
    for f in readonly_files_not_git_tracked:
        if f in artifacts:
            ro_artifacts[f] = artifacts.pop(f)

    artifacts_str = get_art_str(artifacts)
    ro_artifacts_str = get_art_str(ro_artifacts, must_be_version=True)

    if git_snapshotter is not None:
        # Compute cache key
        payload = {
            "snapshotter_hash": git_snapshotter.current_hash
            if git_snapshotter
            else None,
            "artifacts_str": artifacts_str,
        }
        hash_payload = utils.stable_json(payload)
        cache_hash = utils.sha256(hash_payload)
        cache_path = cache_dir / f"{cache_hash}.pkl" if cache_dir is not None else None

        if cache_dir is not None:
            utils.create_dir_and_set_permissions(cache_dir)
    else:
        cache_path = None

    # always write ro files
    logger.info(
        f"Writing {len(ro_artifacts)} read-only artifact files to `{workspace_dir}` for benchmark {benchmark}"
    )
    for filename, content in ro_artifacts.items():
        (workspace_dir / filename).write_text(content)

    if write_non_tracked_only:
        return ro_artifacts_str

    # Load checkpoint or write files
    if cache_path is not None and cache_path.exists():
        cached = utils.load_pickle(cache_path, PrepareCacheType)
        assert cached is not None
        logger.info(f"Restoring prepared repo from cache: {cache_path.name}")
        assert git_snapshotter is not None
        git_snapshotter.restore(cached.snapshot_hash)
    else:
        if only_from_cache:
            raise ValueError(
                f"Prepared repo not found in cache and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )
        logger.info(
            f"Writing {len(artifacts)} artifact files to `{workspace_dir}` for benchmark {benchmark}"
        )
        # write artifact files
        for filename, content in artifacts.items():
            (workspace_dir / filename).write_text(content)

        if cache_path is not None and not do_not_cache:
            assert git_snapshotter is not None
            _, commit = git_snapshotter.snapshot(cache_hash)
            assert commit is not None, "Failed to create git snapshot for prepare_repo"
            utils.dump_pickle(
                cache_path,
                PrepareCacheType(
                    hash_payload=hash_payload,
                    snapshot_hash=commit,
                ),
                do_not_cache=do_not_cache,
            )

    return artifacts_str + "\n" + ro_artifacts_str
