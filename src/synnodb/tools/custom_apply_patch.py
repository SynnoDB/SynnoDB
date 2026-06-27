import logging
import re

from agents.editor import ApplyPatchOperation

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomApplyPatchTool:
    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    @staticmethod
    def _normalize_diff(diff: str, op_type: str) -> str:
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
            if line == "*** Begin Patch":
                continue
            if re.match(r"@@ .* @@$", line):
                cleaned.append("@@")
                continue
            cleaned.append(line)

        if op_type == "create_file":
            # apply_diff(create) expects only "+" lines.
            cleaned = [line for line in cleaned if line.startswith("+")]

        return "\n".join(cleaned)

    async def __call__(self, op_type: str, path: str, diff: str | None) -> str:
        if diff is not None:
            diff = self._normalize_diff(diff, op_type)
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
