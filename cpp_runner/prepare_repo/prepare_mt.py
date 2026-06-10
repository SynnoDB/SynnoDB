import logging
from pathlib import Path

from pipeline.git_snapshotter import GitSnapshotter
from utils import utils

logger = logging.getLogger(__name__)


class PrepareMtCacheType:
    def __init__(
        self,
        hash_payload: str,
        snapshot_hash: str,
    ):
        self.hash_payload = hash_payload
        self.snapshot_hash = snapshot_hash


def prepare_repo_for_mt(
    workspace_dir: Path,
    git_snapshotter: GitSnapshotter | None = None,
    cache_dir: Path | None = None,
    do_not_cache: bool = True,
    only_from_cache: bool = False,
) -> str:
    # thread_pool.hpp: copy from prepare_repo/templates
    thread_pool_hpp_path = Path(__file__).parent / "templates" / "thread_pool.hpp"
    thread_pool_str = thread_pool_hpp_path.read_text()

    query_pool_hpp_path = Path(__file__).parent / "templates" / "query_pool.hpp"
    query_pool_str = query_pool_hpp_path.read_text()

    artifacts: dict[str, str] = {
        "thread_pool.hpp": thread_pool_str,
        "query_pool.hpp": query_pool_str,
    }

    # Stable string for cache key and LLM context
    artifacts_str = "\n\n".join(
        f"// ---- {name} ----\n{content}" for name, content in sorted(artifacts.items())
    )

    if git_snapshotter is not None and cache_dir is not None:
        payload = {
            "snapshotter_hash": git_snapshotter.current_hash
            if git_snapshotter
            else None,
            "artifacts_str": artifacts_str,
        }
        hash_payload = utils.stable_json(payload)
        cache_hash = utils.sha256(hash_payload)
        cache_path = cache_dir / f"{cache_hash}.pkl"
    else:
        cache_path = None

    if cache_dir is not None:
        utils.create_dir_and_set_permissions(cache_dir)

    if cache_path is not None and cache_path.exists():
        cached = utils.load_pickle(cache_path, PrepareMtCacheType)
        assert cached is not None
        logger.info(f"Restoring prepared mt repo from cache: {cache_path.name}")
        assert git_snapshotter is not None
        git_snapshotter.restore(cached.snapshot_hash)
    else:
        if only_from_cache:
            raise ValueError(
                f"Prepared mt repo not found in cache and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )
        logger.info(
            f"Writing {len(artifacts)} artifact files ({', '.join(artifacts.keys())}) to `{workspace_dir}` for mt"
        )
        for filename, content in artifacts.items():
            (workspace_dir / filename).write_text(content)

        if cache_path is not None and not do_not_cache:
            assert git_snapshotter is not None
            _, commit = git_snapshotter.snapshot(cache_hash)
            assert commit is not None, (
                "Failed to create git snapshot for prepare_repo_for_mt"
            )
            utils.dump_pickle(
                cache_path,
                PrepareMtCacheType(
                    hash_payload=hash_payload,
                    snapshot_hash=commit,
                ),
                do_not_cache=False,
            )

    return artifacts_str
