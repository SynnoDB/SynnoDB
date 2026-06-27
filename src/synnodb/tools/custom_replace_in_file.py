import logging

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomReplaceInFileTool:
    """Search/replace edit primitive (sibling of CustomApplyPatchTool).

    Unlike V4A apply_patch, this needs only a locally-unique `old_string` rather
    than verbatim surrounding context, so it avoids the context-match failures
    weak local models hit. Backed by WorkspaceEditor.replace_in_file, sharing the
    same caching/snapshot/readonly/stats plumbing.
    """

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    async def __call__(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        result = self._editor.replace_in_file(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )
        if result.output is not None:
            return result.output

        assert result.status == "completed", (
            f"replace_in_file failed with status: {result.status}"
        )
        return "ok"
