import logging
import re

from agents.editor import ApplyPatchOperation

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomApplyPatchTool:
    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    # apply_patch envelope markers a model sometimes wraps the hunk body in. Not
    # part of the V4A content, so they are dropped during normalization.
    _ENVELOPE_PREFIXES = (
        "*** Begin Patch",
        "*** End Patch",
        "*** Add File:",
        "*** Update File:",
        "*** Delete File:",
    )

    @classmethod
    def _normalize_diff(cls, diff: str, op_type: str) -> str:
        lines = diff.splitlines()
        # Strip unified diff headers if present.
        cleaned: list[str] = []
        for line in lines:
            if line.startswith("diff --git "):
                continue
            if line.startswith("index "):
                continue
            if line.startswith("--- "):
                continue
            if line.startswith("+++ "):
                continue
            if line.startswith(cls._ENVELOPE_PREFIXES):
                continue
            if re.match(r"@@ .* @@$", line):
                cleaned.append("@@")
                continue
            cleaned.append(line)

        if op_type == "create_file":
            # create expects either all lines to start with "+" (diff format) or no lines to start with "+" (raw format).
            body = [line for line in cleaned if line != "@@"]
            is_diff_formatted = all(
                line.startswith("+") for line in body if line.strip() != ""
            )
            if is_diff_formatted:
                # V4A add-diff: drop the diff "+", keep bare blank lines as-is.
                content = [line[1:] if line.startswith("+") else line for line in body]
            else:
                # Raw body: every line is content verbatim, a leading "+" included.
                content = body
                logger.debug(
                    "create_file diff is not in diff format; treating all lines as raw content."
                )
            cleaned = [f"+{line}" for line in content]

        return "\n".join(cleaned)

    async def __call__(self, op_type: str, path: str, diff: str | None) -> str:
        raw_diff = diff
        if diff is not None:
            diff = self._normalize_diff(diff, op_type)

        # A create whose payload carried real text but normalized to nothing (only
        # headers/envelope markers) must fail loudly - never silently write a 0-byte
        # file the model believes it just created. An intentionally empty create
        # (empty/blank payload) still passes through.
        if (
            op_type == "create_file"
            and raw_diff is not None
            and raw_diff.strip()
            and not (diff or "").strip()
        ):
            return (
                f"Error: create_file for {path} received no file content (only "
                "headers/markers). Resend the full file body; lines without a "
                "leading '+' are written as-is."
            )

        op = ApplyPatchOperation(type=op_type, path=path, diff=diff)  # type: ignore

        if op.type == "create_file":
            result = self._editor.create_file(op)
        elif op.type == "update_file":
            result = self._editor.update_file(op)
        elif op.type == "delete_file":
            result = self._editor.delete_file(op)
        else:
            return f"Error: Unknown apply_patch operation type: {op_type}"

        if hasattr(result, "output") and result.output is not None:
            return result.output

        assert result.status == "completed", (
            f"Apply patch operation failed with status: {result.status}"
        )
        return "ok"
