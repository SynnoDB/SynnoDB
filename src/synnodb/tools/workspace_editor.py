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
            legacy_activity=lambda _status, _output: None,
        )

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
            legacy_activity=lambda status, output: (
                self._legacy_activity_summary_entry_for_cached_result(
                    op_type, operation, status, output
                )
            ),
        )

    def _run_cached(
        self,
        op_type: str,
        path: str,
        cache_extra: dict,
        run_impl,
        legacy_activity,
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

        if cache_path.exists():
            cached = utils.load_pickle(cache_path, ApplyPatchCacheType)
            assert cached is not None
            if self._runtime_tracker is not None:
                self._runtime_tracker.add_skipped_time(cached.runtime_seconds)
            self._snapshotter.restore(cached.snapshot_hash)
            activity_summary_entry = getattr(
                cached, "activity_summary_entry", None
            ) or legacy_activity(cached.result_status, cached.result_output)
            self._record_activity_summary(activity_summary_entry)
            logger.debug(f"Apply_patch ({op_type} {path}) replayed from cache")
            return ApplyPatchResult(
                status=cached.result_status,  # type: ignore[arg-type]
                output=cached.result_output,
            )
        elif self._only_from_cache:
            raise ValueError(
                f"Apply_patch result not found in cache for operation {op_type} {path} and only_from_cache is enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
            )

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


def _current_content_block(relative: str, original: str) -> str:
    """Render the current file content (bounded) for a failed-edit response so the
    model can re-anchor without an extra round-trip."""
    lines = original.splitlines()
    MAX_LINES = 2000
    if len(lines) <= MAX_LINES:
        current = original
    else:
        current = (
            "\n".join(lines[:MAX_LINES])
            + f"\n... (truncated, {len(lines)} lines total — cat the file for the full content)"
        )
    return f"=== CURRENT CONTENT OF {relative} ===\n{current}\n=== END ==="


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
