import copy
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from synnodb.cpp_runner.compiler.compiler import Compiler
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.utils import utils

logger = logging.getLogger(__name__)


class CompileCacheType:
    def __init__(
        self,
        outputs: Optional[str],
        hash_payload: Optional[str],
        runtime_seconds: float,
        succeeded: bool = False,
    ):
        self.outputs = outputs
        self.hash_payload = hash_payload
        self.succeeded = succeeded
        self.runtime_seconds = runtime_seconds


class CachedCompiler(Compiler):
    def __init__(
        self,
        args: Dict,
        git_snapshotter: Optional[GitSnapshotter] = None,
        compile_cache_dir: Optional[Path] = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        runtime_tracker: Optional[RuntimeTracker] = None,
        untracked_cpp_runner_content: str | None = None,
    ):
        super().__init__(**args)
        self.args = args
        self.git_snapshotter = git_snapshotter
        self.cache_dir = compile_cache_dir
        self.do_not_cache = do_not_cache
        self.runtime_tracker = runtime_tracker
        self.only_from_cache = only_from_cache
        self.untracked_cpp_runner_content = untracked_cpp_runner_content

        # Whether the most recent build() served its result from the compile
        # cache (vs. actually invoking the compiler). Read by CompileTool to
        # report cache status, since build() can only return the output string.
        self.last_build_served_from_cache = False

        # create cache dir if needed
        for dir in [self.cache_dir]:
            if dir is None:
                continue

            utils.create_dir_and_set_permissions(dir)

    def build(self) -> Optional[str]:
        # forward to cache function. This is only to override the build function of the parent class, which is called by HotpatchProc. The actual caching logic is implemented in build_cached, which is called by this function.
        cached_result, used_cache, compile_key_hash = self.build_cached()
        self.last_build_served_from_cache = used_cache
        return cached_result

    def build_cached(
        self,
        skip_cache: bool = False,
        write_cache: bool = True,
        current_git_snapshot: Optional[str] = None,
    ) -> Tuple[str | None, bool, str]:
        """
        Build with caching support. Returns if the result was from cache.
        This is going beyond the original def build() by returning a tuple
        of (output, from_cache).
        """

        # skip_cache overrides self.only_from_cache: a caller passing skip_cache=True
        # explicitly needs a live build (the cache stores only the compiler verdict,
        # never the binary), so it must compile even in an only-from-cache replay.
        # The publish gate depends on this: a fully cache-replayed run has no `db`
        # binary on disk, and its forced rebuild is what produces the engine to ship.

        is_cached, cached_compile, cache_path, compile_key_hash, hash_payload = (
            self._check_answer_from_cache(current_git_snapshot)
        )
        if is_cached and not skip_cache:
            # Serve the recorded compiler verdict. The binary itself is not
            # cached: callers that need one on disk must trigger a live build
            # (see recompile_if_necessary in the run tool).
            assert cached_compile is not None

            if self.runtime_tracker is not None:
                self.runtime_tracker.add_skipped_time(cached_compile.runtime_seconds)

            assert cached_compile is not None
            return cached_compile.outputs, True, compile_key_hash

        if self.only_from_cache and not skip_cache:
            raise Exception(
                f"Result not found in cache for key {compile_key_hash} and only_from_cache is set. Cache path: {cache_path}"
            )

        # call normal build
        compile_start_time = time.perf_counter()
        output = super().build()

        # Store the output in the cache. An only-from-cache replay never writes: its
        # forced rebuilds (publish gate) must not overwrite the recorded chain with a
        # possibly non-deterministic fresh compile output. A forced (skip_cache)
        # rebuild over an existing entry likewise skips the write: the recorded
        # verdict stays authoritative, the rebuild only exists to produce the binary.
        # That is the ONLY tolerated case - on every other path an entry existing at
        # write time indicates a serious bug (or hash collision), and dump_pickle's
        # already-exists guard raises.
        if (
            cache_path is not None
            and write_cache
            and not (skip_cache and is_cached)
            and not self.do_not_cache
            and not self.only_from_cache
        ):
            utils.dump_pickle(
                cache_path,
                CompileCacheType(
                    outputs=output,
                    hash_payload=hash_payload,
                    succeeded=output is None,
                    runtime_seconds=time.perf_counter() - compile_start_time,
                ),
                do_not_cache=self.do_not_cache,
            )

            logger.debug(
                f"Saved compile result to cache: {cache_path} (succeeded={output is None})"
            )

        return output, False, compile_key_hash

    def _check_answer_from_cache(
        self, current_git_snapshot: Optional[str] = None
    ) -> Tuple[bool, Optional[CompileCacheType], Optional[Path], str, str]:
        if self.git_snapshotter is None and current_git_snapshot is None:
            logger.warning(
                "Can't determine current code version (GitSnapshotter is None); "
                "skipping compile cache lookup."
            )
            return False, None, None, "", ""

        # fetch git hash
        if current_git_snapshot is not None:
            assert self.git_snapshotter is None, (
                "Cannot provide current_git_snapshot if git_snapshotter is set"
            )
            git_hash = current_git_snapshot
        else:
            assert self.git_snapshotter is not None, (
                "git_snapshotter must be set to fetch git hash"
            )
            git_hash = self.git_snapshotter.current_hash

        if self.cache_dir is None:
            logger.info(
                "Cache directory not configured; skipping compile cache lookup."
            )
            return False, None, None, "", ""

        hash_payload = dict(self.args)
        hash_payload.pop("working_dir", None)

        # remove folder names from cache hash payload. This allows to move the parent folders without breaking the cache. The cache will still break if we change the filename of the sources, but this is less likely to happen.
        hash_payload = make_cache_hash_payload_dir_agnostic(hash_payload)
        hash_payload.update(
            {
                "snapshotter_hash": git_hash,
                "cxx_flags": self.extra_cxxflags,
                "untracked_cpp_runner_content": self.untracked_cpp_runner_content,
            }
        )
        stable_payload = utils.stable_json(hash_payload)

        compile_key_hash = utils.sha256(stable_payload)
        cache_path = _cache_path_for_hash(self.cache_dir, compile_key_hash)

        if not cache_path.exists():
            logger.info(f"No matching compile cache found at {cache_path=}")
            return False, None, cache_path, compile_key_hash, stable_payload

        cached: Optional[CompileCacheType] = utils.load_pickle(
            cache_path, CompileCacheType
        )
        assert cached is not None
        logger.debug(f"Loaded compile result from cache: {cache_path}")
        return True, cached, cache_path, compile_key_hash, stable_payload


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Path:
    return cache_dir / f"{hash}.pkl"


def make_cache_hash_payload_dir_agnostic(payload: dict) -> dict:
    # remove folder prefix from the payload --> allows later to move / rename parent folders
    app_extra_srcs: list[str] = payload["app_extra_srcs"]
    include_dirs: list[str] = payload["include_dirs"]
    libs: dict[str, list[str]] = payload["libs"]
    main_src: str = payload["main_src"]

    assert isinstance(app_extra_srcs, list)
    assert isinstance(include_dirs, list)
    assert isinstance(libs, dict)
    assert isinstance(main_src, Path), (
        f"main_src should be a Path, but got {type(main_src)}"
    )

    def adapt_path(path: str | Path) -> str:
        # keep only the filename, remove the folder path
        if isinstance(path, Path):
            return path.name
        else:
            return Path(path).name

    adapted_payload = copy.deepcopy(payload)
    adapted_payload["app_extra_srcs"] = [adapt_path(p) for p in app_extra_srcs]
    adapted_payload["include_dirs"] = [adapt_path(p) for p in include_dirs]
    adapted_payload["main_src"] = adapt_path(main_src)

    for lib_name, srcs in libs.items():
        adapted_payload["libs"][lib_name] = [adapt_path(p) for p in srcs]

    return adapted_payload
