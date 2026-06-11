import logging
from pathlib import Path

from cpp_runner.prepare_repo.prepare import get_art_str
from synth_framework.git_snapshotter import GitSnapshotter
from utils import utils

logger = logging.getLogger(__name__)


class PrepareOptimCacheType:
    def __init__(
        self,
        hash_payload: str,
        snapshot_hash: str,
    ):
        self.hash_payload = hash_payload
        self.snapshot_hash = snapshot_hash


def prepare_repo_for_optim(
    workspace_dir: Path,
    query_impl_filename: str,
    git_snapshotter: GitSnapshotter | None = None,
    cache_dir: Path | None = None,
    do_not_cache: bool = True,
    readonly_files_not_git_tracked: set[str] = set(),
    write_non_tracked_only: bool = False,
    only_from_cache: bool = False,
) -> str:
    # Step 1: Build artifacts dict without writing files

    # query_impl.cpp: read current on-disk version
    # Per-query TRACE_RESET/FLUSH are already emitted by the template generator
    # (assemble_query_and_args.py). We only need to ensure trace.hpp is included.
    query_impl_path = workspace_dir / query_impl_filename
    query_impl_str = query_impl_path.read_text()

    # add #include "trace.hpp" only if not already present:
    if '#include "trace.hpp"' not in query_impl_str:
        include_pos = query_impl_str.find("#include")
        assert include_pos != -1, f"Could not find #include in {query_impl_path}"
        query_impl_str = (
            query_impl_str[:include_pos]
            + '#include "trace.hpp"\n'
            + query_impl_str[include_pos:]
        )

    # replace trace stuff
    trace_kw = 'results.push_back(QueryResult{req.query_id, req.req_id, "", elapsed_ms, error});'
    trace_target = "results.push_back(QueryResult{req.query_id, req.req_id, trace_get_and_clear(), elapsed_ms, error});"
    assert trace_kw in query_impl_str, (
        f"Could not find '{trace_kw}' in {query_impl_path}"
    )
    query_impl_str = query_impl_str.replace(trace_kw, trace_target)

    # remove comments
    trace_kw_list = ["TRACE_FLUSH();", "TRACE_RESET();"]
    for kw in trace_kw_list:
        query_impl_str = query_impl_str.replace(f"// {kw}", kw)

    artifacts: dict[str, str] = {
        query_impl_filename: query_impl_str,
    }

    # delete base impl files:
    delete_kw = "<<DELETE>>"
    for filename in [
        "storage_plan.txt",
        "base_impl_todo.txt",
        "trace.hpp",  # in old snapshot versions trace.hpp was in llm workspace
    ]:
        path = workspace_dir / filename
        if path.exists():
            artifacts[filename] = delete_kw

    # split into read-only (git-ignored) and regular files
    ro_artifacts: dict[str, str] = {}
    for f in readonly_files_not_git_tracked:
        if f in artifacts:
            ro_artifacts[f] = artifacts.pop(f)

    artifacts_str = get_art_str(artifacts)
    ro_artifacts_str = get_art_str(ro_artifacts, must_be_version=True)

    if git_snapshotter is not None and cache_dir is not None:
        # Compute cache key (only over tracked artifacts; ro files are identified by FILE_VERSION
        # which is included via the snapshotter's framework-version hash elsewhere)
        payload = {
            "snapshotter_hash": git_snapshotter.current_hash
            if git_snapshotter
            else None,
            "artifacts_str": artifacts_str if not write_non_tracked_only else "",
            "ro_artifacts_str": ro_artifacts_str,
        }
        hash_payload = utils.stable_json(payload)
        cache_hash = utils.sha256(hash_payload)
        cache_path = cache_dir / f"{cache_hash}.pkl" if cache_dir is not None else None
    else:
        cache_path = None

    if cache_dir is not None:
        utils.create_dir_and_set_permissions(cache_dir)

    # Always write ro (git-ignored) artifacts so they exist on disk regardless of cache state.
    # These files are excluded from git snapshots, so a snapshot restore would not bring them back.
    write_artifacts(
        ro_artifacts, workspace_dir, delete_kw, require_delete_targets=False
    )

    if write_non_tracked_only:
        return ro_artifacts_str

    # Load checkpoint or write regular artifacts
    if cache_path is not None and cache_path.exists():
        cached = utils.load_pickle(cache_path, PrepareOptimCacheType)
        assert cached is not None
        logger.info(f"Restoring prepared optim repo from cache: {cache_path.name}")
        assert git_snapshotter is not None
        git_snapshotter.restore(cached.snapshot_hash)
    else:
        if only_from_cache:
            raise ValueError(
                f"Prepared optim repo not found in cache and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )

        write_artifacts(
            artifacts, workspace_dir, delete_kw, require_delete_targets=True
        )

        if cache_path is not None and not do_not_cache:
            assert git_snapshotter is not None
            _, commit = git_snapshotter.snapshot(cache_hash)
            assert commit is not None, (
                "Failed to create git snapshot for prepare_repo_for_optim"
            )
            utils.dump_pickle(
                cache_path,
                PrepareOptimCacheType(
                    hash_payload=hash_payload,
                    snapshot_hash=commit,
                ),
                do_not_cache=False,
            )

    return artifacts_str + "\n" + ro_artifacts_str


def write_artifacts(
    artifacts: dict[str, str],
    workspace_dir: Path,
    delete_kw: str,
    require_delete_targets: bool,
) -> None:
    if not artifacts:
        return
    logger.info(
        f"Writing {len(artifacts)} artifact files ({', '.join(artifacts.keys())}) to `{workspace_dir}` for optim"
    )
    for filename, content in artifacts.items():
        path = workspace_dir / filename
        if content == delete_kw:
            if not path.exists():
                assert not require_delete_targets, (
                    f"Expected to find {path} for deletion but it does not exist"
                )
                continue
            logger.info(f"Deleting {path} as part of optim preparation")
            path.unlink()
        else:
            path.write_text(content)
