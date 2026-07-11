import difflib
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

from agents import apply_diff, custom_span
from agents.editor import ApplyPatchOperation, ApplyPatchResult

from synnodb.observability.logging.logger import PLAIN
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.tools.tool_call_error_logger import log_tool_call_error
from synnodb.utils import utils

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


class RejectedApplyPatchCacheType:
    """Cache entry for an apply_patch call rejected at argument-schema validation.

    Keyed on the raw tool arguments and looked up BEFORE the arguments are
    re-validated, so the recorded verdict - not the current rules - decides the
    outcome on replay. This keeps old runs replayable even if what counts as an
    invalid apply_patch later changes. The rejection has no file side effects, so
    the entry stores only what a faithful replay needs: the exact ``message``
    returned to the model, plus ``path``/``reason`` for the live-ui stats."""

    def __init__(self, args_json: str, path: str | None, reason: str, message: str):
        self.args_json = args_json
        self.path = path
        self.reason = reason
        self.message = message


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

    def set_readonly(self, filename: str, readonly: bool) -> None:
        """Add or remove a workspace-relative filename from the read-only set at
        runtime. Used to lock query stubs during the storage-build stages and
        release them when per-query implementation begins."""
        path = self._root / filename
        if readonly:
            if path not in self._readonly_files:
                self._readonly_files.append(path)
        elif path in self._readonly_files:
            self._readonly_files.remove(path)

    # ---------- public ops (cached) ----------

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("create_file", operation, self._create_file_impl)

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("update_file", operation, self._update_file_impl)

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return self._cached_op("delete_file", operation, self._delete_file_impl)

    def replace_in_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ApplyPatchResult:
        # Search/replace edit primitive (sibling of apply_patch update_file).
        # Needs only a locally-unique `old_string`, not surrounding context, so
        # it sidesteps the verbatim-context failures that weak models hit with
        # V4A diffs. Routed through the same cache/snapshot wrapper.
        return self._run_cached(
            op_type="replace_in_file",
            path=path,
            cache_extra={
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": replace_all,
            },
            run_impl=lambda: self._replace_in_file_impl(
                path, old_string, new_string, replace_all
            ),
        )

    def write_file(self, path: str, content: str) -> ApplyPatchResult:
        # Full-content create/overwrite primitive (sibling of apply_patch
        # create_file/update_file). Takes raw file content directly instead of a
        # V4A diff, so it sidesteps both the "+"-prefixed-whole-file format for
        # create and the byte-exact context matching for update. Routed through
        # the same cache/snapshot wrapper.
        return self._run_cached(
            op_type="write_file",
            path=path,
            cache_extra={"content": content},
            run_impl=lambda: self._write_file_impl(path, content),
            legacy_activity=lambda _status, _output: None,
        )

    def read_file(
        self, path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        # Read primitive (sibling of write_file/replace_in_file). Never mutates
        # the workspace, so - unlike the ops above - it does not go through
        # _run_cached: the underlying file content is already reproducible from
        # the deterministic snapshot state, and routing reads through the cache
        # would only bloat the cache dir for no benefit.
        relative = self._relative_path(path)
        target = self._resolve(path)
        self._run_stats_collector.log_read_file_stats(path)
        if not target.exists():
            return f"Error: file {relative} does not exist."
        if target.is_dir():
            return f"Error: {relative} is a directory, not a file."

        original = target.read_text(encoding="utf-8")
        rendered = _render_numbered_lines(original, offset, limit)
        self._record_activity_summary(f"read_file called: {path}")
        return rendered

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
        # Thin wrapper preserving the apply_patch cache-key shape. `stable_json`
        # sorts keys, so the {diff} payload here hashes identically to the
        # pre-refactor version - existing apply_patch caches stay valid.
        return self._run_cached(
            op_type=op_type,
            path=operation.path,
            cache_extra={"diff": operation.diff},
            run_impl=lambda: impl(operation),
        )

    def _run_cached(
        self,
        op_type: str,
        path: str,
        cache_extra: dict,
        run_impl,
    ) -> ApplyPatchResult:
        # Build cache key from current workspace state + operation. The cache
        # entry captures both the result and the post-op snapshot so a hit can
        # restore the workspace without re-applying the patch. `cache_extra`
        # carries the op-specific key fields (apply_patch: {diff};
        # replace_in_file: {old_string, new_string, replace_all}).
        if not self._cache_enabled():
            raise RuntimeError(
                "Cache is not enabled for WorkspaceEditor. Check initialization parameters."
            )

        assert self._snapshotter is not None
        payload = {
            "snapshotter_hash": self._snapshotter.current_hash,
            "op_type": op_type,
            "path": path,
            **cache_extra,
            "untracked_cpp_runner_content": self._untracked_cpp_runner_content,
        }
        hash_payload = utils.stable_json(payload)
        key = utils.sha256(hash_payload)
        cache_path = self._cache_path_for(key)

        cached = (
            utils.load_pickle(cache_path, ApplyPatchCacheType)
            if cache_path.exists()
            else None
        )

        # An entry is only replayable if it carries the stored activity-summary
        # line. That line feeds the supervisor prompt (and thus the supervisor LLM
        # cache key), so it must be reproduced byte-for-byte on replay: we store it
        # verbatim when the op runs and replay it verbatim here, never reconstruct
        # it from the result text. Entries written before this field existed lack
        # the attribute; they are treated as a miss and recomputed below (which
        # regenerates the current-format entry) rather than guessed at.
        if cached is not None and hasattr(cached, "activity_summary_entry"):
            if self._runtime_tracker is not None:
                self._runtime_tracker.add_skipped_time(cached.runtime_seconds)
            self._snapshotter.restore(cached.snapshot_hash)
            self._record_activity_summary(cached.activity_summary_entry)
            self._run_stats_collector.record_apply_patch_cache_hit()
            logger.debug(f"Apply_patch ({op_type} {path}) replayed from cache")
            return ApplyPatchResult(
                status=cached.result_status,  # type: ignore[arg-type]
                output=cached.result_output,
            )

        if self._only_from_cache:
            detail = (
                "cache entry predates the stored activity-summary format and "
                "cannot be replayed deterministically; re-record the cache"
                if cached is not None
                else "result not found in cache"
            )
            raise ValueError(
                f"Apply_patch {detail} for operation {op_type} {path} and "
                f"only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )

        if cached is not None:
            # A stale legacy entry occupies this key but can't be replayed. Drop it
            # so the recompute below can rewrite the key in the current format
            # (dump_pickle refuses to overwrite an existing file).
            cache_path.unlink(missing_ok=True)

        start = time.perf_counter()
        try:
            result, activity_summary_entry = run_impl()
        except Exception as e:
            # catch the exception and write to result
            result = ApplyPatchResult(
                status="failed", output=f"Error applying patch: {e}"
            )
            activity_summary_entry = (
                f"Apply_patch called: {op_type} {path} (FAILED - {e})"
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
        return result

    def _record_activity_summary(self, entry: str | None) -> None:
        if entry is not None:
            self._run_stats_collector.add_to_activity_summary(entry)

    def _rejected_patch_key(self, args_json: str) -> str:
        return utils.sha256(
            utils.stable_json(
                {"op_type": "rejected_apply_patch", "args_json": args_json}
            )
        )

    def replay_rejected_patch(self, args_json: str) -> str | None:
        """If these exact apply_patch arguments were recorded as a rejection, replay
        that rejection from cache and return the original tool message; otherwise
        return None (the caller then validates the arguments live).

        This is looked up BEFORE argument validation, so the recorded verdict wins
        over the current rules: a later change to what counts as an invalid
        apply_patch cannot alter the outcome of an already-recorded run. The stored
        message is returned verbatim so the model sees the identical tool result it
        saw when the run was recorded."""
        if self._cache_dir is None:
            return None
        cache_path = self._cache_path_for(self._rejected_patch_key(args_json))
        if not cache_path.exists():
            return None
        entry = utils.load_pickle(cache_path, RejectedApplyPatchCacheType)
        assert entry is not None, f"Corrupt rejected-apply_patch cache: {cache_path}"
        self._run_stats_collector.record_apply_patch_rejected(entry.path, entry.reason)
        self._run_stats_collector.record_apply_patch_cache_hit()
        logger.debug("Rejected apply_patch replayed from cache")
        return entry.message

    def record_rejected_patch(
        self, args_json: str, path: str | None, reason: str, message: str
    ) -> None:
        """Persist a freshly-rejected apply_patch, keyed on its raw arguments, so
        later replays reproduce this exact rejection via replay_rejected_patch, and
        record the rejection stats for the live-ui. Called only after a live
        validation failure (i.e. replay_rejected_patch found no entry).

        The rejection has no file side effects, so - unlike a real edit - it needs
        no snapshot and no activity-summary line (an activity line would perturb the
        supervisor prompt and its cache key). Writing is skipped under
        only_from_cache to honour read-only replay."""
        self._run_stats_collector.record_apply_patch_rejected(path, reason)

        if self._cache_dir is None or self._do_not_cache or self._only_from_cache:
            return

        cache_path = self._cache_path_for(self._rejected_patch_key(args_json))
        if cache_path.exists():
            return  # idempotent: an identical rejection was already recorded

        utils.dump_pickle(
            cache_path,
            RejectedApplyPatchCacheType(
                args_json=args_json, path=path, reason=reason, message=message
            ),
            do_not_cache=self._do_not_cache,
        )

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

            # The target may already exist as a scaffold/stub. create_file supplies the
            # FULL content, so overwriting is the intended outcome (read-only files were
            # already rejected above). We overwrite rather than failing: agents routinely
            # use create_file to (re)write a scaffolded file, and the hard "refusing to
            # overwrite" error left weaker models confused (delete-then-recreate loops).
            overwrote_existing = target.exists()

            content = apply_diff("", diff, mode="create")
            print_colored_diff(diff, is_create=True)
            target.write_text(content, encoding="utf-8")

            # report stats
            str_diff = f"=== CREATING: {target} ===\n{operation.diff[:8000] if operation.diff else ''}\n"
            assert deleted == 0, "Create operation should not have deleted lines"
            self._run_stats_collector.log_apply_patch_stats(
                "create",
                added_lines=added,
                deleted_lines=deleted,
                string_diff=str_diff,
                file_touched=Path(operation.path).name,
            )

            verb = "Overwrote existing file" if overwrote_existing else "Created"
            suffix = " (overwrote existing file)" if overwrote_existing else ""
            return (
                ApplyPatchResult(status="completed", output=f"{verb} {relative}"),
                f"Apply_patch called: Create file {operation.path}{suffix}",
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
            # Strip model-output wrappers (markdown fences, *** Begin Patch
            # envelope) so an otherwise-valid hunk still applies.
            diff = normalize_diff_payload(operation.diff or "")

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
                # Repair near-miss context/deletion lines (unicode lookalikes the
                # model retyped) before applying; apply_diff's matcher only does
                # exact/rstrip/strip and would otherwise reject them.
                diff = repair_diff_context(original, diff)
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
                log_tool_call_error(
                    error_type="ApplyPatchFailed",
                    error=e,
                    model=self._run_stats_collector.model,
                    turn=self._run_stats_collector.last_turn,
                    extra={
                        "file": str(target),
                        "diff (first 60 lines)": "\n".join(diff.splitlines()[:60]),
                    },
                )
                # Include the current file content so the model can rebuild the
                # diff immediately without an extra round-trip. The workspace may
                # have been silently reverted since the model last read this file,
                # making its cached view stale.
                return (
                    ApplyPatchResult(
                        status="failed",
                        output=(
                            f"Error applying patch to {relative}: {e}. "
                            "The workspace may have been reverted since you last read this file — your cached version is likely stale. "
                            "Rebuild the diff using the CURRENT file content shown below. "
                            "Context lines must EXACTLY match. Do not wrap the diff in markdown code fences.\n\n"
                            f"{_current_content_block(relative, original)}"
                        ),
                    ),
                    None,
                )

            # report stats
            str_diff = f"=== UPDATING: {target} ===\n{operation.diff[:8000] if operation.diff else ''}\n"

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

    def _replace_in_file_impl(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> tuple[ApplyPatchResult, str | None]:
        with custom_span(
            f"replace_in_file ({path})",
            {
                "file": path,
                "old_string": old_string[:1000],
                "replace_all": replace_all,
            },
        ):
            relative = self._relative_path(path)
            target = self._resolve(path)
            logger.info(f"Replacing in: {target} (replace_all={replace_all})")

            if target in self._readonly_files:
                return self._replace_failed(
                    path,
                    f"Error: Attempting to modify read-only file: {relative}",
                    summary="read-only",
                )

            if old_string == "":
                return self._replace_failed(
                    path,
                    "Error: old_string must not be empty. Use apply_patch create_file "
                    "to create a new file.",
                    summary="empty old_string",
                )

            if old_string == new_string:
                return self._replace_failed(
                    path,
                    "Error: old_string and new_string are identical; nothing to change.",
                    summary="no-op",
                )

            if not target.exists():
                return self._replace_failed(
                    path,
                    f"Error: file {relative} does not exist. Use apply_patch "
                    "create_file to create it.",
                    summary="missing file",
                )

            original = target.read_text(encoding="utf-8")

            # Exact substring first, then a narrow quote-normalized fallback.
            actual_old = _find_actual_string(original, old_string)
            if actual_old is None:
                return self._replace_failed(
                    path,
                    f"String to replace was not found in {relative}. The file may have "
                    "changed since you last read it. Re-anchor old_string on the CURRENT "
                    "content shown below; it must match exactly.\n\n"
                    f"{_current_content_block(relative, original)}",
                    summary="string not found",
                    log_error=True,
                )

            matches = original.count(actual_old)
            if matches > 1 and not replace_all:
                return self._replace_failed(
                    path,
                    f"Found {matches} occurrences of old_string in {relative}, but "
                    "replace_all is false. Add surrounding lines so old_string uniquely "
                    "identifies ONE location, or set replace_all=true to change all of "
                    "them.",
                    summary=f"{matches} matches, replace_all=false",
                    log_error=True,
                )

            new_final = (
                new_string
                if _is_markdown(relative)
                else _strip_trailing_whitespace(new_string)
            )
            n_repl = matches if replace_all else 1
            patched = (
                original.replace(actual_old, new_final)
                if replace_all
                else original.replace(actual_old, new_final, 1)
            )

            if patched == original:
                return self._replace_failed(
                    path,
                    f"Error: replace produced no change in {relative}.",
                    summary="no change",
                )

            target.write_text(patched, encoding="utf-8")

            added = (new_final.count("\n") + 1) if new_final else 0
            deleted = actual_old.count("\n") + 1
            str_diff = (
                f"=== REPLACE_IN_FILE: {target} (x{n_repl}) ===\n"
                f"- {actual_old[:4000]}\n+ {new_final[:4000]}\n"
            )
            self._run_stats_collector.log_apply_patch_stats(
                "replace",
                added_lines=added * n_repl,
                deleted_lines=deleted * n_repl,
                string_diff=str_diff,
                file_touched=Path(path).name,
            )

            occ = f"{n_repl} occurrence{'s' if n_repl != 1 else ''}"
            return (
                ApplyPatchResult(
                    status="completed",
                    output=f"Replaced {occ} in {relative}",
                ),
                f"replace_in_file called: {path}",
            )

    def _write_file_impl(
        self, path: str, content: str
    ) -> tuple[ApplyPatchResult, str | None]:
        with custom_span(
            f"write file ({path})",
            {"file": path, "content_len": len(content)},
        ):
            relative = self._relative_path(path)
            target = self._resolve(path, ensure_parent=True)
            logger.info(f"Writing: {target} ({len(content)} bytes)")

            if target in self._readonly_files:
                return self._write_failed(
                    path,
                    f"Error: Attempting to write read-only file: {relative}",
                    summary="read-only",
                )

            existed = target.exists()
            original = target.read_text(encoding="utf-8") if existed else ""

            if original == content:
                return self._write_failed(
                    path,
                    f"Error: write produced no change in {relative}.",
                    summary="no change",
                )

            diff = "\n".join(
                difflib.unified_diff(
                    original.splitlines(),
                    content.splitlines(),
                    lineterm="",
                )
            )
            added, deleted = count_diff_operations(diff)

            target.write_text(content, encoding="utf-8")

            str_diff = f"=== WRITING: {target} ===\n{diff[:1000]}\n"
            self._run_stats_collector.log_apply_patch_stats(
                "write",
                added_lines=added,
                deleted_lines=deleted,
                string_diff=str_diff,
                file_touched=Path(path).name,
            )

            verb = "Overwrote" if existed else "Wrote"
            return (
                ApplyPatchResult(status="completed", output=f"{verb} {relative}"),
                f"write_file called: {path}",
            )

    def _write_failed(
        self,
        path: str,
        message: str,
        summary: str,
    ) -> tuple[ApplyPatchResult, str | None]:
        self._run_stats_collector.log_apply_patch_stats(
            "write",
            added_lines=0,
            deleted_lines=0,
            string_diff="",
            file_touched=Path(path).name,
            failed=summary,
        )
        return (
            ApplyPatchResult(status="failed", output=message),
            f"write_file called: {path} (FAILED - {summary})",
        )

    def _replace_failed(
        self,
        path: str,
        message: str,
        summary: str,
        log_error: bool = False,
    ) -> tuple[ApplyPatchResult, str | None]:
        self._run_stats_collector.log_apply_patch_stats(
            "replace",
            added_lines=0,
            deleted_lines=0,
            string_diff="",
            file_touched=Path(path).name,
            failed=summary,
        )
        if log_error:
            log_tool_call_error(
                error_type="ReplaceInFileFailed",
                error=ValueError(summary),
                model=self._run_stats_collector.model,
                turn=self._run_stats_collector.last_turn,
            )
        return (
            ApplyPatchResult(status="failed", output=message),
            f"replace_in_file called: {path} (FAILED - {summary})",
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


# Curly -> straight quotes only. Length-preserving (1 char -> 1 char), which lets
# _find_actual_string map a normalized match back to the file's real bytes by
# index. Deliberately narrow: we must NOT collapse whitespace, because a
# substring replace writes back exact bytes/indentation.
_QUOTE_NORMALIZATION = {
    "‘": "'",  # left single
    "’": "'",  # right single
    "“": '"',  # left double
    "”": '"',  # right double
}


def _normalize_quotes(text: str) -> str:
    for src, dst in _QUOTE_NORMALIZATION.items():
        text = text.replace(src, dst)
    return text


def _find_actual_string(file_content: str, search: str) -> str | None:
    """Return the real file substring matching `search`, or None.

    Tries an exact substring match first, then a curly/straight quote-normalized
    match - returning the file's true bytes at that position so the subsequent
    replace preserves the file's existing typography.
    """
    if search in file_content:
        return search
    idx = _normalize_quotes(file_content).find(_normalize_quotes(search))
    if idx != -1:
        return file_content[idx : idx + len(search)]
    return None


def _is_markdown(path: str) -> bool:
    return path.lower().endswith((".md", ".mdx"))


def _strip_trailing_whitespace(text: str) -> str:
    # Drop trailing spaces/tabs on each line (assumes LF; the workspace is LF).
    return re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)


_DEFAULT_READ_MAX_LINES = 2000


def _current_content_block(relative: str, original: str) -> str:
    """Render the current file content (bounded) for a failed-edit response so the
    model can re-anchor without an extra round-trip."""
    lines = original.splitlines()
    if len(lines) <= _DEFAULT_READ_MAX_LINES:
        current = original
    else:
        current = (
            "\n".join(lines[:_DEFAULT_READ_MAX_LINES])
            + f"\n... (truncated, {len(lines)} lines total — cat the file for the full content)"
        )
    return f"=== CURRENT CONTENT OF {relative} ===\n{current}\n=== END ==="


def _render_numbered_lines(content: str, offset: int | None, limit: int | None) -> str:
    """Render file content `cat -n` style (1-based line numbers) for read_file.

    `offset` is the 1-based line to start from (default 1). `limit` bounds how
    many lines are returned (default _DEFAULT_READ_MAX_LINES). A trailing note
    is appended when more lines remain past what was returned, mirroring
    _current_content_block's truncation note.
    """
    lines = content.splitlines()
    total = len(lines)
    start = max(offset, 1) if offset is not None else 1
    count = limit if limit is not None else _DEFAULT_READ_MAX_LINES
    start_idx = start - 1
    end_idx = min(start_idx + max(count, 0), total)

    numbered = "\n".join(f"{i + 1:6d}\t{lines[i]}" for i in range(start_idx, end_idx))

    if start_idx >= total:
        return f"(file has {total} lines; offset {start} is past the end of the file)"

    if end_idx < total:
        numbered += (
            f"\n... (truncated, showing lines {start}-{end_idx} of {total} "
            "total — pass offset/limit to read more)"
        )
    return numbered


# Unicode lookalikes the model tends to transcribe as ASCII (or vice versa) when
# retyping context lines. Canonicalizing both sides lets us recover the file's
# true bytes for a context line the model quoted almost-correctly.
_CANON_REPLACEMENTS = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‒": "-",  # figure dash
    "‑": "-",  # non-breaking hyphen
    "−": "-",  # minus sign
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    "→": "->",
    "⇒": "=>",
    "≈": "~",
    "×": "x",
    "≤": "<=",
    "≥": ">=",
}


def _canonicalize_line(line: str) -> str:
    line = unicodedata.normalize("NFKC", line)
    for src, dst in _CANON_REPLACEMENTS.items():
        line = line.replace(src, dst)
    line = re.sub(r"\s", " ", line)  # any unicode whitespace -> ASCII space
    line = re.sub(r"(?<=\d)[, ](?=\d{3})", "", line)  # digit group separators
    return re.sub(r" +", " ", line).strip()


def repair_diff_context(original: str, diff: str) -> str:
    """Replace mistyped context/deletion lines in a V4A diff with the file's true bytes.

    The model retypes context lines from memory; on long unicode-heavy lines it
    drifts (em dash vs hyphen, NBSP vs space, ...), which apply_diff's matcher
    (exact / rstrip / strip only) cannot absorb. For each ' '/'-' diff line that
    has no exact match in the file, substitute the file line whose canonical form
    matches - but only when that match is unambiguous. '+' lines keep the model's
    bytes. Returns the diff unchanged when no repair was needed.
    """
    file_lines = original.splitlines()
    exact = set(file_lines)
    canon_to_bodies: dict[str, set[str]] = {}
    for fl in file_lines:
        canon_to_bodies.setdefault(_canonicalize_line(fl), set()).add(fl)

    repaired: list[str] = []
    changed = False
    for line in diff.splitlines():
        if line.startswith((" ", "-")) and not line.startswith("---"):
            body = line[1:]
            if body and body not in exact:
                candidates = canon_to_bodies.get(_canonicalize_line(body))
                if candidates is not None and len(candidates) == 1:
                    true_body = next(iter(candidates))
                    if true_body != body:
                        line = line[0] + true_body
                        changed = True
        repaired.append(line)

    if not changed:
        return diff
    logger.debug(
        "repair_diff_context: substituted file bytes for mistyped context lines"
    )
    return "\n".join(repaired)


# Markdown code fences and apply_patch envelope markers that models often wrap a
# diff in. Stripping them lets an otherwise-valid hunk apply. Both are safe to
# remove: a real V4A hunk line is always ' '/'-'/'+' prefixed, so it can never look
# like a bare ``` fence or a '***' envelope marker.
_FENCE_OPEN = re.compile(r"^```[^\n]*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")
_PATCH_ENVELOPE_PREFIXES = (
    "*** Begin Patch",
    "*** End Patch",
    "*** Update File:",
    "*** Add File:",
    "*** Delete File:",
)


def _strip_code_fences(diff: str) -> str:
    """Remove a single markdown code fence wrapping the whole diff.

    Conservative: strips only when the first non-blank line opens a fence at column
    0 (``` / ```diff / ```patch) AND the last non-blank line is a bare closing
    fence. Context lines (space-prefixed) and '+'/'-' content lines never match, so
    a diff that legitimately edits backtick lines is left untouched.
    """
    lines = diff.splitlines()
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or not _FENCE_OPEN.match(lines[start]):
        return diff
    end = len(lines) - 1
    while end > start and lines[end].strip() == "":
        end -= 1
    if end <= start or not _FENCE_CLOSE.match(lines[end]):
        return diff  # no clean closing fence — leave the diff exactly as-is
    return "\n".join(lines[start + 1 : end])


def _strip_patch_envelope(diff: str) -> str:
    """Drop apply_patch envelope markers (``*** Begin Patch``, ``*** Update File:``,
    ...) a model sometimes includes in the hunk body. Safe: hunk content lines are
    ' '/'-'/'+' prefixed and never start with ``***``.
    """
    lines = diff.splitlines()
    kept = [ln for ln in lines if not ln.startswith(_PATCH_ENVELOPE_PREFIXES)]
    if len(kept) == len(lines):
        return diff  # nothing stripped — preserve exact bytes
    return "\n".join(kept)


def normalize_diff_payload(diff: str) -> str:
    """Strip model-output wrappers that are not part of the V4A hunk body: a
    surrounding markdown code fence and apply_patch envelope markers. Applied to
    every update diff before context repair / apply, covering both the built-in and
    custom apply_patch tools."""
    return _strip_patch_envelope(_strip_code_fences(diff))


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
