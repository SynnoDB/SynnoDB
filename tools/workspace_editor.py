import logging
import time
from pathlib import Path
from typing import Optional

from agents import apply_diff, custom_span
from agents.editor import ApplyPatchOperation, ApplyPatchResult

from observability.logging.logger import PLAIN
from observability.logging.run_stats_collector import RunStatsCollector
from synth_framework.git_snapshotter import GitSnapshotter
from synth_framework.runtime_tracker import RuntimeTracker
from utils import utils

logger = logging.getLogger(__name__)


def print_colored_diff(diff: str, is_create: bool = False) -> None:
    RED = "\033[31m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    lines = diff.splitlines()

    if is_create:
        max_lines = 20
        cutoff = len(lines) > max_lines
        lines = lines[:max_lines]  # show only first 20 lines for create
        if cutoff:
            lines.append("...")

    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            logger.log(PLAIN, f"{GREEN}{line}{RESET}")
        elif line.startswith("-") and not line.startswith("---"):
            logger.log(PLAIN, f"{RED}{line}{RESET}")
        elif line.startswith("@@"):
            logger.log(PLAIN, f"{CYAN}{line}{RESET}")
        else:
            logger.log(PLAIN, line)


class ApplyPatchCacheType:
    def __init__(
        self,
        result_output: str | None,
        result_status: str | None,
        snapshot_hash: str,
        hash_payload: str,
        runtime_seconds: float,
        activity_summary_entry: str | None = None,
    ):
        self.result_output = result_output
        self.result_status = result_status
        self.snapshot_hash = snapshot_hash
        self.hash_payload = hash_payload
        self.runtime_seconds = runtime_seconds
        self.activity_summary_entry = activity_summary_entry


class WorkspaceEditor:
    def __init__(
        self,
        root: Path,
        run_stats_collector: RunStatsCollector,
        readonly_files: set[str],
        untracked_cpp_runner_content: str,
        snapshotter: Optional[GitSnapshotter] = None,
        cache_dir: Optional[Path] = None,
        do_not_cache: bool = False,
        runtime_tracker: Optional[RuntimeTracker] = None,
        only_from_cache: bool = False,
    ) -> None:
        self._root = root.resolve()
        self._run_stats_collector = run_stats_collector
        self._readonly_files = [
            self._root / ro_f for ro_f in readonly_files
        ]  # convert from file names to paths based on workspace dir
        self._snapshotter = snapshotter
        self._cache_dir = cache_dir
        self._do_not_cache = do_not_cache
        self._runtime_tracker = runtime_tracker
        self._only_from_cache = only_from_cache
        self._untracked_cpp_runner_content = untracked_cpp_runner_content

        if self._cache_dir is not None:
            utils.create_dir_and_set_permissions(self._cache_dir)

    # ---------- public ops (cached) ----------

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("create_file", operation, self._create_file_impl)

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("update_file", operation, self._update_file_impl)

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("delete_file", operation, self._delete_file_impl)

    # ---------- cache wrapper ----------

    def _cache_enabled(self) -> bool:
        return (
            self._snapshotter is not None
            and self._cache_dir is not None
            and not self._do_not_cache
        )

    def _cache_path_for(self, hash_str: str) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / f"{hash_str}.pkl"

    def _cached_op(
        self, op_type: str, operation: ApplyPatchOperation, impl
    ) -> ApplyPatchResult:
        # Build cache key from current workspace state + operation. The cache
        # entry captures both the result and the post-op snapshot so a hit can
        # restore the workspace without re-applying the patch.
        if not self._cache_enabled():
            raise RuntimeError(
                "Cache is not enabled for WorkspaceEditor. Check initialization parameters."
            )

            # return impl(operation)

        assert self._snapshotter is not None
        payload = {
            "snapshotter_hash": self._snapshotter.current_hash,
            "op_type": op_type,
            "path": operation.path,
            "diff": operation.diff,
            "untracked_cpp_runner_content": self._untracked_cpp_runner_content,
        }
        hash_payload = utils.stable_json(payload)
        key = utils.sha256(hash_payload)
        cache_path = self._cache_path_for(key)

        if cache_path.exists():
            cached = utils.load_pickle(cache_path, ApplyPatchCacheType)
            assert cached is not None
            if self._runtime_tracker is not None:
                self._runtime_tracker.add_skipped_time(cached.runtime_seconds)
            self._snapshotter.restore(cached.snapshot_hash)
            activity_summary_entry = getattr(
                cached, "activity_summary_entry", None
            ) or self._legacy_activity_summary_entry_for_cached_result(
                op_type,
                operation,
                cached.result_status,
                cached.result_output,
            )
            self._record_activity_summary(activity_summary_entry)
            logger.debug(
                f"Apply_patch ({op_type} {operation.path}) replayed from cache"
            )
            return ApplyPatchResult(
                status=cached.result_status,  # type: ignore[arg-type]
                output=cached.result_output,
            )
        elif self._only_from_cache:
            raise ValueError(
                f"Apply_patch result not found in cache for operation {op_type} {operation.path} and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )

        start = time.perf_counter()
        try:
            result, activity_summary_entry = impl(operation)
        except Exception as e:
            # catch the exception and write to result
            result = ApplyPatchResult(
                status="failed", output=f"Error applying patch: {e}"
            )
            activity_summary_entry = (
                f"Apply_patch called: {operation.type} {operation.path} (FAILED - {e})"
            )

        runtime_seconds = time.perf_counter() - start
        self._record_activity_summary(activity_summary_entry)

        _, commit = self._snapshotter.snapshot(key)
        assert commit is not None, (
            "Failed to create git snapshot for apply_patch operation"
        )
        utils.dump_pickle(
            cache_path,
            ApplyPatchCacheType(
                result_output=result.output,
                result_status=result.status,
                snapshot_hash=commit,
                hash_payload=hash_payload,
                runtime_seconds=runtime_seconds,
                activity_summary_entry=activity_summary_entry,
            ),
            do_not_cache=self._do_not_cache,
        )
        # logger.debug(
        #     f"Apply_patch ({op_type} {operation.path}) wrote to cache: {cache_path}"
        # )
        return result

    def _record_activity_summary(self, entry: str | None) -> None:
        if entry is not None:
            self._run_stats_collector.add_to_activity_summary(entry)

    def _legacy_activity_summary_entry_for_cached_result(
        self,
        op_type: str,
        operation: ApplyPatchOperation,
        result_status: str | None,
        result_output: str | None,
    ) -> str | None:
        if result_status == "completed":
            action = {
                "create_file": "Create file",
                "update_file": "Update file",
                "delete_file": "Delete file",
            }.get(op_type)
            if action is None:
                return None
            return f"Apply_patch called: {action} {operation.path}"

        if result_status != "failed":
            return None

        output = result_output or ""
        action = {
            "create_file": "Create file",
            "update_file": "Update file",
            "delete_file": "Delete file",
        }.get(op_type)
        if action is None:
            return None

        if "read-only" in output:
            return f"Apply_patch called: {action} {operation.path} (FAILED - read-only)"
        if "Refusing to overwrite non-empty file" in output:
            return f"Apply_patch called: {action} {operation.path} (FAILED - non-empty file exists)"

        return None

    # ---------- raw operations ----------

    def _create_file_impl(
        self, operation: ApplyPatchOperation
    ) -> tuple[ApplyPatchResult, str | None]:
        with custom_span(
            f"create file ({operation.path})",
            {
                "path": operation.path,
                "diff": operation.diff[:1000] if operation.diff else None,
            },
        ):
            diff = operation.diff or ""

            # compute stats
            added, deleted = count_diff_operations(diff)

            relative = self._relative_path(operation.path)
            target = self._resolve(operation.path, ensure_parent=True)
            logger.info(
                f"Creating: {target} (added lines: {added}, deleted lines: {deleted})"
            )

            # check if file is read-only before applying create
            if target in self._readonly_files:
                activity_summary_entry = f"Apply_patch called: Create file {operation.path} (FAILED - read-only)"
                return (
                    ApplyPatchResult(
                        status="failed",
                        output=f"Error: Attempting to create read-only file: {relative}",
                    ),
                    activity_summary_entry,
                )

            if target.exists():
                activity_summary_entry = f"Apply_patch called: Create file {operation.path} (FAILED - non-empty file exists)"
                return (
                    ApplyPatchResult(
                        status="failed",
                        output=f"Error: Refusing to overwrite non-empty file {relative}",
                    ),
                    activity_summary_entry,
                )

            content = apply_diff("", diff, mode="create")
            print_colored_diff(diff, is_create=True)
            target.write_text(content, encoding="utf-8")

            # report stats
            str_diff = f"=== CREATING: {target} ===\n{operation.diff[:1000] if operation.diff else ''}\n"
            assert deleted == 0, "Create operation should not have deleted lines"
            self._run_stats_collector.log_apply_patch_stats(
                "create",
                added_lines=added,
                deleted_lines=deleted,
                string_diff=str_diff,
                file_touched=Path(operation.path).name,
            )

            return (
                ApplyPatchResult(status="completed", output=f"Created {relative}"),
                f"Apply_patch called: Create file {operation.path}",
            )

    def _update_file_impl(
        self, operation: ApplyPatchOperation
    ) -> tuple[ApplyPatchResult, str | None]:
        with custom_span(
            f"update file ({operation.path})",
            {
                "file": operation.path,
                "diff": operation.diff[:1000] if operation.diff else None,
            },
        ):
            diff = operation.diff or ""

            # calc stats
            added, deleted = count_diff_operations(diff)

            relative = self._relative_path(operation.path)
            target = self._resolve(operation.path)
            logger.info(
                f"Updating: {target} (added lines: {added}, deleted lines: {deleted})"
            )

            # check if file is read-only before applying update
            if target in self._readonly_files:
                activity_summary_entry = f"Apply_patch called: Update file {operation.path} (FAILED - read-only)"
                return (
                    ApplyPatchResult(
                        status="failed",
                        output=f"Error: Attempting to update read-only file: {relative}",
                    ),
                    activity_summary_entry,
                )

            original = target.read_text(encoding="utf-8")
            print_colored_diff(diff)

            try:
                patched = apply_diff(original, diff)
                target.write_text(patched, encoding="utf-8")
            except Exception as e:
                self._run_stats_collector.log_apply_patch_stats(
                    "update",
                    added_lines=0,
                    deleted_lines=0,
                    string_diff="",
                    file_touched=Path(operation.path).name,
                    failed=str(e),
                )
                return (
                    ApplyPatchResult(
                        status="failed", output=f"Error applying patch: {e}"
                    ),
                    None,
                )

            # report stats
            str_diff = f"=== UPDATING: {target} ===\n{operation.diff[:1000] if operation.diff else ''}\n"

            self._run_stats_collector.log_apply_patch_stats(
                "update",
                added_lines=added,
                deleted_lines=deleted,
                string_diff=str_diff,
                file_touched=Path(operation.path).name,
            )

            return (
                ApplyPatchResult(
                    status="completed",
                    output=f"Updated {relative}",
                ),
                f"Apply_patch called: Update file {operation.path}",
            )

    def _delete_file_impl(
        self, operation: ApplyPatchOperation
    ) -> tuple[ApplyPatchResult, str | None]:
        with custom_span(f"delete file ({operation.path})", {"file": operation.path}):
            relative = self._relative_path(operation.path)
            target = self._resolve(operation.path)
            logger.info(f"Deleting: {target}")

            # check if file is read-only before applying update
            if target in self._readonly_files:
                activity_summary_entry = f"Apply_patch called: Delete file {operation.path} (FAILED - read-only)"
                return (
                    ApplyPatchResult(
                        status="failed",
                        output=f"Error: Attempting to delete read-only file: {relative}",
                    ),
                    activity_summary_entry,
                )

            original = target.read_text(encoding="utf-8")
            target.unlink(missing_ok=True)

            # report stats
            str_diff = f"=== DELETING: {target} ===\nDelete\n"
            self._run_stats_collector.log_apply_patch_stats(
                "delete",
                added_lines=0,
                deleted_lines=len(original.splitlines()),
                string_diff=str_diff,
                file_touched=Path(operation.path).name,
            )

            return (
                ApplyPatchResult(
                    status="completed",
                    output=f"Deleted {relative}",
                ),
                f"Apply_patch called: Delete file {operation.path}",
            )

    def _relative_path(self, value: str) -> str:
        resolved = self._resolve(value)
        return resolved.relative_to(self._root).as_posix()

    def _resolve(self, relative: str, ensure_parent: bool = False) -> Path:
        candidate = Path(relative)
        target = candidate if candidate.is_absolute() else (self._root / candidate)
        target = target.resolve()
        # Only allow files directly in the root directory (no subdirectories)
        if target.parent != self._root:
            raise RuntimeError(
                f"Operation outside allowed root dir (no subdirs): {relative}"
            )
        try:
            target.relative_to(self._root)
        except ValueError:
            raise RuntimeError(f"Operation outside workspace: {relative}") from None
        if ensure_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target


def count_diff_operations(diff: str) -> tuple[int, int]:
    added = sum(
        1
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deleted = sum(
        1
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return added, deleted
