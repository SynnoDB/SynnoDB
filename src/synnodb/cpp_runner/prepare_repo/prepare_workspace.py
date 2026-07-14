import logging
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from synnodb.conversations.filenames import PLAN_FILENAME_BY_USECASE
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    PREPARE_METADATA_FILENAME,
    PrepareFeatures,
    assert_resolved,
)
from synnodb.cpp_runner.prepare_repo.retrieve_framework_version_hash import (
    extract_version_id,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)

DELETE_KW = "<<DELETE>>"


@dataclass(frozen=True)
class PreparedWorkspaceFiles:
    tracked_files: dict[str, str]
    readonly_files_not_git_tracked: dict[str, str]
    tracked_artifacts_str: str
    readonly_artifacts_str: str
    artifacts_str: str


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
        db_storage: DBStorage,
        prepare_cache_dir: Path | None = None,
    ):
        self.workload_provider = workload_provider
        self.workspace_dir = workspace_dir.resolve()
        self.git_snapshotter = git_snapshotter
        self.db_storage = db_storage
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

    # ------------------------- per-feature builders ---------------------------
    # Each builder maps to exactly one PrepareFeatures field (see the module
    # docstring of prepare_features.py) and returns file contents without
    # writing to disk.

    @abstractmethod
    def build_scaffold_files(self, features: PrepareFeatures) -> dict[str, str]:
        """The ``scaffold``/``storage`` feature: framework files for the storage
        variant, queries.md, per-query files, and ``query_impl.cpp`` shaped by
        the query-impl flags (parallel_ready_impl / tracing / sample_trace)."""
        raise NotImplementedError(
            "build_scaffold_files is not implemented in the base PrepareWorkspace. Use a specific implementation like OLAPPrepareWorkspace."
        )

    @property
    @abstractmethod
    def plan_filename(self) -> str:
        """The usecase's storage-plan filename inside the workspace."""
        raise NotImplementedError

    def build_storage_plan_files(self, features: PrepareFeatures) -> dict[str, str]:
        """The ``storage_plan_text`` feature: inject the plan text as a file."""
        if features.storage_plan_text is None:
            return {}
        return {self.plan_filename: features.storage_plan_text}

    def build_cleanup_deletes(self) -> dict[str, str]:
        """The base-impl inputs to drop when a chain moves past the base impl
        (tracing newly enabled): plan file, todo list, and the workspace-local
        trace.hpp old snapshot versions carried."""
        files: dict[str, str] = {}
        for filename in [
            *PLAN_FILENAME_BY_USECASE.values(),
            "base_impl_todo.txt",
            "trace.hpp",
        ]:
            if (self.workspace_dir / filename).exists():
                files[filename] = DELETE_KW
        return files

    @classmethod
    def _get_readonly_files(cls) -> tuple[set[str], set[str]]:
        """The scaffold files the agent may not edit.

        A classmethod, not a static one: the names are language-specific
        (``query_impl.cpp`` vs ``query/src/lib.rs``), so a Rust workspace
        overrides the untracked half.
        """
        return cls._readonly_scaffold_files(), {
            "queries.md",
            # The workspace's prepare record: committed with every snapshot (it
            # is the authoritative record of what the files were prepared with)
            # but never modifiable by the agent.
            PREPARE_METADATA_FILENAME,
        }

    @classmethod
    def _readonly_scaffold_files(cls) -> set[str]:
        """Read-only scaffold that is NOT git-tracked (always rewritten on prepare)."""
        return {
            "args_parser.hpp",
            "query_impl.hpp",
            "query_impl.cpp",
            "parquet_reader.hpp",
            "parquet_reader.cpp",
            "query_pool.hpp",
            "thread_pool.hpp",
        }

    # ------------------------ interpreter entry points -------------------------
    def prepare(
        self,
        features: PrepareFeatures,
        write_non_tracked_only: bool = False,
    ) -> str:
        """Write the scaffold (with the storage plan, if any) for the resolved
        ``features``. Returns the artifacts string of the written files."""
        prepared = self.assemble(features, write_non_tracked_only)
        self.write_prepared_files(prepared)
        return prepared.artifacts_str

    def prepare_cleanup(self) -> str:
        """Drop the base-impl inputs from the workspace (see
        :meth:`build_cleanup_deletes`)."""
        prepared = self.assemble_cleanup()
        self.write_prepared_files(prepared)
        return prepared.artifacts_str

    def assemble(
        self,
        features: PrepareFeatures,
        write_non_tracked_only: bool = False,
    ) -> PreparedWorkspaceFiles:
        """Assemble the scaffold files and artifact identifier without touching
        the workspace."""
        assert_resolved(features, "preparing the workspace")
        files = self.build_scaffold_files(features)
        files.update(self.build_storage_plan_files(features))
        return self._assemble_files(
            files,
            only_query_md=features.scaffold == "queries_md_only",
            write_non_tracked_only=write_non_tracked_only,
        )

    def assemble_cleanup(self) -> PreparedWorkspaceFiles:
        """Assemble the cleanup deletes without touching the workspace."""
        return self._assemble_files(self.build_cleanup_deletes())

    def write_prepared_files(
        self,
        prepared: PreparedWorkspaceFiles,
        write_tracked: bool = True,
    ) -> None:
        """Write assembled files into the workspace.

        Read-only files excluded from git are always written because git
        snapshots cannot restore them. Tracked files are written only on a cache
        miss; a cache hit restores them from the prepared snapshot.
        """
        logger.info(
            f"Writing {len(prepared.readonly_files_not_git_tracked)} read-only artifact files to `{self.workspace_dir}` for benchmark {self.workload_provider.benchmark_name}"
        )
        _write_files(
            prepared.readonly_files_not_git_tracked,
            self.workspace_dir,
            delete_kw=DELETE_KW,
            require_delete_targets=False,
        )

        if not write_tracked:
            return

        logger.info(
            f"Writing {len(prepared.tracked_files)} artifact files to `{self.workspace_dir}` for benchmark {self.workload_provider.benchmark_name}"
        )
        _write_files(
            prepared.tracked_files,
            self.workspace_dir,
            delete_kw=DELETE_KW,
            require_delete_targets=True,
        )

    def _write_files(
        self,
        files: dict[str, str],
        only_query_md: bool = False,
        write_non_tracked_only: bool = False,
    ) -> str:
        prepared = self._assemble_files(
            files,
            only_query_md=only_query_md,
            write_non_tracked_only=write_non_tracked_only,
        )
        self.write_prepared_files(prepared)
        return prepared.artifacts_str

    def _assemble_files(
        self,
        files: dict[str, str],
        only_query_md: bool = False,
        write_non_tracked_only: bool = False,
    ) -> PreparedWorkspaceFiles:
        """Split the given files into tracked/untracked groups and compute the
        artifacts string without touching the workspace."""
        # check files
        for filename, content in files.items():
            assert content is not None, f"Content for file {filename} is None"

        # filter out files
        if only_query_md:
            assert "queries.md" in files, (
                "queries.md must be generated to use only_query_md mode"
            )
            files = {"queries.md": files["queries.md"]}
        else:
            files = dict(files)

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

        if write_non_tracked_only:
            return PreparedWorkspaceFiles(
                tracked_files={},
                readonly_files_not_git_tracked=ro_files_not_git,
                tracked_artifacts_str="",
                readonly_artifacts_str=ro_files_not_git_id_str,
                artifacts_str=ro_files_not_git_id_str,
            )

        return PreparedWorkspaceFiles(
            tracked_files=files,
            readonly_files_not_git_tracked=ro_files_not_git,
            tracked_artifacts_str=files_id_str,
            readonly_artifacts_str=ro_files_not_git_id_str,
            artifacts_str=files_id_str + "\n" + ro_files_not_git_id_str,
        )


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
            # Unlink any existing target before writing. A prior run that was
            # hard-killed inside the sandbox's read-only window (see
            # sandbox._readonly_ctx) can leave artifacts at mode 0444, which
            # would make write_text fail with PermissionError. Unlinking (the
            # workspace dir is writable, so this succeeds even for 0444 files)
            # lets the write self-heal instead of wedging every future run.
            assert path.is_file() or not path.exists(), (
                f"Expected {path} to be a file or not exist"
            )
            path.unlink(missing_ok=True)
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
