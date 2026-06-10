import functools
import logging
from pathlib import Path
from typing import Optional

from utils import utils

logger = logging.getLogger(__name__)

CEB_DIR = Path("/mnt/labstore/bespoke_olap/datasets/ceb/imdb")


def get_query_gen(benchmark: str):
    # prepare query gen
    if benchmark == "tpch":
        from workloads.dataset.gen_tpch.gen_tpch_query import gen_query

        gen_query_fn = gen_query
    elif benchmark == "ceb":
        from workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

        gen_query_fn = functools.partial(gen_query_single_only, ceb_dir=CEB_DIR)
    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")

    return gen_query_fn


def get_placeholders_fn(benchmark: str, do_not_cache: bool, cache_dir: Optional[Path]):
    # prepare query gen
    gen_fn = None
    if benchmark == "tpch":
        from workloads.dataset.gen_tpch.gen_tpch_query import gen_query

        def gen_placeholder_tpch(**kwargs):
            # we only need the placeholders dict
            return gen_query(**kwargs)[2]

        gen_fn = gen_placeholder_tpch

    elif benchmark == "ceb":
        from workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

        # load placeholders from disk

        def gen_placeholder_ceb(**kwargs):
            # check cache first
            hash_payload = {
                "benchmark": "ceb",
                "query_name": kwargs["query_name"],
            }
            stable_payload = utils.stable_json(hash_payload)

            hash = utils.sha256(stable_payload)

            if cache_dir is None:
                cache_path = None
            else:
                # create cache dir if needed
                utils.create_dir_and_set_permissions(cache_dir)
                cache_path = _cache_path_for_hash(cache_dir, hash)

            # check compile cache - replay compile result from cache if available
            if cache_path is not None and cache_path.exists():
                cached: Optional[PlaceholdersCacheType] = utils.load_pickle(
                    cache_path, PlaceholdersCacheType
                )
                assert cached is not None
                logger.debug(f"Loaded placeholders from cache: {cache_path}")

                return cached.placeholders

            # we only need the placeholders dict
            placeholders = gen_query_single_only(**kwargs, ceb_dir=CEB_DIR)[2]

            # store output in cache
            if cache_path is not None and not do_not_cache:
                utils.dump_pickle(
                    cache_path,
                    PlaceholdersCacheType(
                        placeholders=placeholders, hash_payload=stable_payload
                    ),
                    do_not_cache=do_not_cache,
                )

            return placeholders

        gen_fn = gen_placeholder_ceb

    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")

    return gen_fn


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Path:
    return cache_dir / f"{hash}.pkl"


class PlaceholdersCacheType:
    def __init__(self, placeholders: dict, hash_payload: str):
        self.placeholders = placeholders
        self.hash_payload = hash_payload
