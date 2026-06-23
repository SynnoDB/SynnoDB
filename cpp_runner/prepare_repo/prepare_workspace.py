import logging
from abc import abstractmethod
from pathlib import Path

from cpp_runner.prepare_repo.retrieve_framework_version_hash import extract_version_id
from synth_framework.git_snapshotter import GitSnapshotter
from utils import utils
from workloads.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)

DELETE_KW = "<<DELETE>>"


class PrepareCacheType:
    def __init__(
        self,
        hash_payload: str,
        snapshot_hash: str,
    ):
        self.hash_payload = hash_payload
        self.snapshot_hash = snapshot_hash


class PrepareWorkspace:
    def __init__(
        self,
        workload_provider: WorkloadProvider,
        workspace_dir: Path,
        git_snapshotter: GitSnapshotter | None,
        prepare_cache_dir: Path | None = None,
    ):
        self.workload_provider = workload_provider
        self.workspace_dir = workspace_dir.resolve()
        self.git_snapshotter = git_snapshotter
        self.prepare_cache_dir = prepare_cache_dir

        (
            self.ro_files_not_git_tracked,
            self.ro_files_to_be_git_tracked,
        ) = self._get_readonly_files()

        # verifications
        if self.git_snapshotter is not None:
            assert self.git_snapshotter.working_dir == self.workspace_dir, (
                f"Git snapshotter working dir {self.git_snapshotter.working_dir} does not match workspace dir {self.workspace_dir}"
            )

    @abstractmethod
    def _assemble_usecase_files(self) -> dict[str, str]:
        """Build template file contents without writing to disk."""
        raise NotImplementedError(
            "assemble_usecase_files is not implemented in the base PrepareWorkspace. Use a specific implementation like OLAPPrepareWorkspace."
        )

    @staticmethod
    def _get_readonly_files() -> tuple[set[str], set[str]]:
        readonly_files_not_git_tracked = {
            "args_parser.hpp",
            "query_impl.hpp",
            "query_impl.cpp",
            "parquet_reader.hpp",
            "parquet_reader.cpp",
        }

        readonly_files_to_be_git_tracked = {
            "queries.md",
        }

        return readonly_files_not_git_tracked, readonly_files_to_be_git_tracked

    def prepare(
        self,
        only_query_md: bool = False,
        write_non_tracked_only: bool = False,
        only_from_cache: bool = False,
        do_not_cache: bool = False,
        usecase_args: dict[str, str] = dict(),
    ) -> str:
        """Main method to prepare the workspace. Returns a dict of filename to file content."""

        # assemble per usecase files
        usecase_files = self._assemble_usecase_files(**usecase_args)

        file_ids_in_context = self._write_files(
            usecase_files,
            only_query_md=only_query_md,
            write_non_tracked_only=write_non_tracked_only,
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

        return file_ids_in_context

    def prepare_optim(
        self,
        write_non_tracked_only: bool = False,
        only_from_cache: bool = False,
        do_not_cache: bool = False,
    ) -> str:
        # query_impl.cpp: read current on-disk version
        # Per-query TRACE_RESET/FLUSH are already emitted by the template generator
        # (assemble_query_and_args.py). We only need to ensure trace.hpp is included.
        query_impl_path = self.workspace_dir / "query_impl.cpp"
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

        files: dict[str, str] = {
            "query_impl.cpp": query_impl_str,
        }

        # delete base impl files:
        delete_kw = "<<DELETE>>"
        for filename in [
            "storage_plan.txt",
            "base_impl_todo.txt",
            "trace.hpp",  # in old snapshot versions trace.hpp was in llm workspace
        ]:
            path = self.workspace_dir / filename
            if path.exists():
                files[filename] = delete_kw

        return self._write_files(
            files,
            write_non_tracked_only=write_non_tracked_only,
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

    def prepare_mt(
        self, do_not_cache: bool = True, only_from_cache: bool = False
    ) -> str:
        # thread_pool.hpp: copy from prepare_repo/templates
        thread_pool_hpp_path = Path(__file__).parent / "templates" / "thread_pool.hpp"
        thread_pool_str = thread_pool_hpp_path.read_text()

        query_pool_hpp_path = Path(__file__).parent / "templates" / "query_pool.hpp"
        query_pool_str = query_pool_hpp_path.read_text()

        files: dict[str, str] = {
            "thread_pool.hpp": thread_pool_str,
            "query_pool.hpp": query_pool_str,
        }

        return self._write_files(
            files,
            do_not_cache=do_not_cache,
            only_from_cache=only_from_cache,
        )

    def _write_files(
        self,
        files: dict[str, str],
        only_query_md: bool = False,
        write_non_tracked_only: bool = False,
        only_from_cache: bool = False,
        do_not_cache: bool = False,
    ) -> str:
        # check files
        for filename, content in files.items():
            assert content is not None, f"Content for file {filename} is None"

        # filter out files
        if only_query_md:
            assert "queries.md" in files, (
                "queries.md must be generated to use only_query_md mode"
            )
            files = {"queries.md": files["queries.md"]}

        # split into read-only and regular files
        ro_files_not_git: dict[str, str] = {}
        for f in self.ro_files_not_git_tracked:
            if f in files:
                ro_files_not_git[f] = files.pop(f)

        # assemble identifiers for files/repo version
        files_id_str = _get_files_identifier_str(files)
        ro_files_not_git_id_str = _get_files_identifier_str(
            ro_files_not_git, must_be_version=True
        )

        # always write non-tracked ro files
        logger.info(
            f"Writing {len(ro_files_not_git)} read-only artifact files to `{self.workspace_dir}` for benchmark {self.workload_provider.benchmark_name}"
        )
        _write_files(
            ro_files_not_git,
            self.workspace_dir,
            delete_kw=DELETE_KW,
            require_delete_targets=False,
        )

        if write_non_tracked_only:
            # early return
            return ro_files_not_git_id_str

        #####
        # Check if we can restore prepare from git snapshotter - otherwise have to create new snapshot
        #####
        if self.git_snapshotter is not None:
            # Compute cache key
            payload = {
                "snapshotter_hash": self.git_snapshotter.current_hash
                if self.git_snapshotter
                else None,
                "files_id_str": files_id_str,  # exclude ro files excluded from git
            }
            hash_payload = utils.stable_json(payload)
            cache_hash = utils.sha256(hash_payload)
            cache_path = (
                self.prepare_cache_dir / f"{cache_hash}.pkl"
                if self.prepare_cache_dir is not None
                else None
            )

            if self.prepare_cache_dir is not None:
                utils.create_dir_and_set_permissions(self.prepare_cache_dir)
        else:
            cache_path = None

        # Load checkpoint or write files
        if cache_path is not None and cache_path.exists():
            cached = utils.load_pickle(cache_path, PrepareCacheType)
            assert cached is not None
            logger.info(f"Restoring prepared repo from cache: {cache_path.name}")
            assert self.git_snapshotter is not None
            self.git_snapshotter.restore(cached.snapshot_hash)
        else:
            if only_from_cache:
                raise ValueError(
                    f"Prepared repo not found in cache and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
                )
            logger.info(
                f"Writing {len(files)} artifact files to `{self.workspace_dir}` for benchmark {self.workload_provider.benchmark_name}"
            )
            # write artifact files
            _write_files(
                files,
                self.workspace_dir,
                delete_kw=DELETE_KW,
                require_delete_targets=True,
            )

            if cache_path is not None and not do_not_cache:
                assert self.git_snapshotter is not None
                _, commit = self.git_snapshotter.snapshot(cache_hash)
                assert commit is not None, (
                    "Failed to create git snapshot for prepare_repo"
                )
                utils.dump_pickle(
                    cache_path,
                    PrepareCacheType(
                        hash_payload=hash_payload,
                        snapshot_hash=commit,
                    ),
                    do_not_cache=do_not_cache,
                )

        return files_id_str + "\n" + ro_files_not_git_id_str


def _write_files(
    files: dict[str, str],
    workspace_dir: Path,
    delete_kw: str,
    require_delete_targets: bool,
) -> None:
    if not files:
        return
    logger.info(
        f"Writing {len(files)} artifact files ({', '.join(files.keys())}) to `{workspace_dir}` for optim"
    )
    for filename, content in files.items():
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


def _get_files_identifier_str(
    files: dict[str, str], must_be_version: bool = False
) -> str:
    # extract version str for each file: if found add the version, if not, add the full file content
    artifacts_str = []
    for name, content in sorted(files.items()):
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
