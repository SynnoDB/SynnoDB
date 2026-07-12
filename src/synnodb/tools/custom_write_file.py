import logging

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomWriteFileTool:
    """Full-content create/overwrite primitive (sibling of CustomReplaceInFileTool).

    Unlike apply_patch's create_file/update_file, this takes the raw file
    content directly - no V4A diff syntax, no "+"-prefixing the whole file.
    Backed by WorkspaceEditor.write_file, sharing the same caching/snapshot/
    readonly/stats plumbing.
    """

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    async def __call__(self, path: str, content: str) -> str:
        result = self._editor.write_file(path=path, content=content)
        if result.output is not None:
            return result.output

        assert result.status == "completed", (
            f"write_file failed with status: {result.status}"
        )
        return "ok"
