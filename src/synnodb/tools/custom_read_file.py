import logging

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomReadFileTool:
    """Line-numbered file read primitive (sibling of CustomWriteFileTool).

    Renders content `cat -n` style so the model can build precise
    replace_in_file/apply_patch edits without an extra round-trip through the
    shell tool. Backed by WorkspaceEditor.read_file, which is not routed
    through the cache/snapshot machinery since reads never mutate the
    workspace.
    """

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    async def __call__(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        return self._editor.read_file(path=path, offset=offset, limit=limit)
