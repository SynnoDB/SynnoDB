import logging
from typing import Any, Dict

from agents import (
    ShellActionRequest,
    ShellCallData,
    ShellCommandRequest,
)
from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field, ValidationError

from synnodb.tools.custom_apply_patch import CustomApplyPatchTool
from synnodb.tools.custom_read_file import CustomReadFileTool
from synnodb.tools.custom_replace_in_file import CustomReplaceInFileTool
from synnodb.tools.custom_write_file import CustomWriteFileTool
from synnodb.tools.shell_executor import ShellExecutor
from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


class CustomShellArgs(BaseModel):
    command: str = Field(..., description="Shell command to execute")
    timeout_ms: int | None = Field(
        None, description="Timeout in milliseconds (optional)"
    )


def make_custom_openai_shell_tool(
    # cwd: Path,
    # cache_dir: Path,
    # do_not_cache: bool,
    # git_snapshotter: Optional[GitSnapshotter] = None,
    # wandb_metrics_hook: WandbRunHook | None = None,
    shell_executor: ShellExecutor,
) -> FunctionTool:
    async def on_invoke(
        ctx: RunContextWrapper[Any], args_json: str
    ) -> Dict[int, Dict[str, str]]:
        args = CustomShellArgs.model_validate_json(args_json)

        # assemble ShellCommandRequest object
        request = ShellCommandRequest(
            ctx_wrapper=ctx,
            data=ShellCallData(
                call_id="123",  # not necessary in our shell_executor impl
                action=ShellActionRequest(
                    commands=[args.command],
                    timeout_ms=args.timeout_ms,
                ),
            ),
        )

        # run command
        shell_result = await shell_executor(request)

        # # format shell result
        out_dict = {}
        for i, out in enumerate(shell_result.output):
            entry = {
                "stdout": out.stdout,
                "stderr": out.stderr,
                "exit_code": out.exit_code,
            }
            out_dict[i] = entry
        return out_dict

        # out_list = []
        # for out in shell_result.output:
        #     assert out.exit_code is not None
        #     out_list.append(
        #         ResponseFunctionShellCallOutputContentParam(
        #             outcome=OutcomeExit(
        #                 exit_code=out.exit_code,
        #                 type="exit",
        #             ),
        #             stdout=out.stdout,
        #             stderr=out.stderr,
        #         )
        #     )

        # out = ShellCallOutput(
        #     call_id=None,
        #     output=out_list,
        #     type="shell_call_output",
        # )

        # return out

    return FunctionTool(
        name="shell",
        description="Runs a shell command locally",
        params_json_schema=CustomShellArgs.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=False,  # always shown to the model
    )


class CustomApplyPatchArgs(BaseModel):
    type: str = Field(..., description="create_file, update_file, or delete_file")
    path: str = Field(..., description="Path relative to workspace root")
    diff: str | None = Field(
        None,
        description=(
            "The V4A hunk only — no markdown fences, no '*** Begin Patch' wrapper. "
            "update_file: ' ' context / '-' remove / '+' add lines, context copied "
            "byte-for-byte from the file. create_file: full content as '+' lines. "
            "delete_file: empty."
        ),
    )


_APPLY_PATCH_SCHEMA_SIMPLE = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["create_file", "update_file", "delete_file"],
            "description": "Operation type: create_file, update_file, or delete_file",
        },
        "path": {
            "type": "string",
            "description": "Path relative to workspace root",
        },
        "diff": {
            "type": "string",
            "description": (
                "V4A hunk for create/update (empty for delete_file). Lines: ' ' "
                "context, '-' remove, '+' add; context copied byte-for-byte from "
                "the file. No markdown fences or '*** Begin Patch' wrapper."
            ),
        },
    },
    "required": ["type", "path"],
}


def make_custom_openai_apply_patch_tool(
    editor: WorkspaceEditor, use_simple_schema: bool = False
) -> FunctionTool:
    impl = CustomApplyPatchTool(editor=editor)

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        try:
            args = CustomApplyPatchArgs.model_validate_json(args_json)
        except ValidationError as e:
            # The JSON was well-formed but did not match the tool schema (e.g. the
            # required `type` field was omitted). Return the error to the model as
            # a tool result - matching this tool's convention of reporting failures
            # via the return value - so it can retry with a corrected call instead
            # of the ValidationError propagating and crashing the whole run.
            logger.warning(
                "apply_patch received arguments that failed schema validation: %s", e
            )
            return (
                "Error: apply_patch arguments failed validation. Retry with a valid "
                f"call.\n{e}"
            )
        return await impl(args.type, args.path, args.diff)

    schema = (
        _APPLY_PATCH_SCHEMA_SIMPLE
        if use_simple_schema
        else CustomApplyPatchArgs.model_json_schema()
    )

    return FunctionTool(
        name="apply_patch",
        description=(
            "Create, update, or delete a file with a V4A diff. For a small, localized "
            "change to an existing file, the `replace_in_file` tool is usually more "
            "reliable than an update_file diff.\n"
            "update_file diff format: each line is prefixed — ' ' (unchanged context), "
            "'-' (remove), '+' (add); an optional '@@ <a nearby line>' anchors the hunk. "
            "Copy context and '-' lines BYTE-FOR-BYTE from the CURRENT file — do not "
            "retype from memory; indentation and punctuation must match exactly. Send "
            "ONLY the hunk: no markdown code fences, no '*** Begin Patch'/'*** Update "
            "File:' wrapper, no '--- '/'+++ ' headers. create_file: provide the full file "
            "content as '+' lines; it writes the whole file, creating it OR overwriting "
            "an existing (non-read-only) file — use it to (re)write a scaffolded file in "
            "one shot. delete_file: empty diff. "
            "On failure the tool returns the current file content so you can re-anchor "
            "and retry. Read-only files that cannot be modified: parquet_reader.cpp, "
            "parquet_reader.hpp, query_impl.cpp, query_impl.hpp, args_parser.hpp."
        ),
        params_json_schema=schema,
        on_invoke_tool=on_invoke,
        defer_loading=False,  # always shown to the model
    )


class ReplaceInFileArgs(BaseModel):
    file_path: str = Field(..., description="Path relative to workspace root")
    old_string: str = Field(..., description="The exact text to find and replace")
    new_string: str = Field(..., description="The text to replace it with")
    replace_all: bool = Field(
        False,
        description="Replace every occurrence instead of requiring a unique match",
    )


_REPLACE_IN_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Path relative to workspace root",
        },
        "old_string": {
            "type": "string",
            "description": (
                "The exact text to find in the file. Must match the current file content "
                "byte-for-byte (indentation included) and must uniquely identify ONE location "
                "- include a few surrounding lines if a snippet would otherwise be ambiguous. "
                "Do NOT wrap in markdown code fences."
            ),
        },
        "new_string": {
            "type": "string",
            "description": 'The replacement text (use "" only to delete the matched text).',
        },
        "replace_all": {
            "type": "boolean",
            "description": (
                "Set true to replace every occurrence. If old_string matches more than once "
                "and this is false, the edit fails and asks you to disambiguate."
            ),
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}


def make_custom_openai_replace_in_file_tool(
    editor: WorkspaceEditor,
) -> FunctionTool:
    impl = CustomReplaceInFileTool(editor=editor)

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        args = ReplaceInFileArgs.model_validate_json(args_json)
        return await impl(
            args.file_path, args.old_string, args.new_string, args.replace_all
        )

    return FunctionTool(
        name="replace_in_file",
        description=(
            "Edits an existing file by replacing an exact string. The preferred way to modify "
            "files: provide `old_string` (the exact current text, uniquely identifying one spot) "
            "and `new_string`. No diff syntax or surrounding-context hunks needed. If old_string "
            "is not found or is ambiguous, the call fails with the current file content so you can "
            "retry. Use replace_all=true to change every occurrence. Read-only files that cannot "
            "be modified: parquet_reader.cpp, parquet_reader.hpp, query_impl.cpp, query_impl.hpp, "
            "args_parser.hpp."
        ),
        params_json_schema=_REPLACE_IN_FILE_SCHEMA,
        on_invoke_tool=on_invoke,
        defer_loading=False,  # always shown to the model
    )


class WriteFileArgs(BaseModel):
    path: str = Field(..., description="Path relative to workspace root")
    content: str = Field(..., description="The full content to write to the file")


_WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path relative to workspace root",
        },
        "content": {
            "type": "string",
            "description": (
                "The COMPLETE file content to write. Creates the file if it does "
                "not exist, or overwrites it entirely if it does - this is not a "
                "diff or a partial update, the previous content is fully replaced."
            ),
        },
    },
    "required": ["path", "content"],
}


def make_custom_openai_write_file_tool(
    editor: WorkspaceEditor,
) -> FunctionTool:
    impl = CustomWriteFileTool(editor=editor)

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        try:
            args = WriteFileArgs.model_validate_json(args_json)
        except ValidationError as e:
            # See make_custom_openai_apply_patch_tool's on_invoke for why this is
            # reported back to the model instead of left to propagate and crash
            # the run.
            logger.warning(
                "write_file received arguments that failed schema validation: %s", e
            )
            return (
                "Error: write_file arguments failed validation. Retry with a valid "
                f"path (str) and content (str). Details: {e}"
            )
        return await impl(args.path, args.content)

    return FunctionTool(
        name="write_file",
        description=(
            "Creates a new file or overwrites an existing one with the given full "
            "content. No diff/patch syntax needed - just the complete file text. "
            "Use this instead of apply_patch's create_file/update_file when it's "
            "simpler to (re)write the whole file in one shot. For a small, "
            "localized change to an existing file, prefer `replace_in_file` - it "
            "only touches the part that changed. Read-only files that cannot be "
            "written: parquet_reader.cpp, parquet_reader.hpp, query_impl.cpp, "
            "query_impl.hpp, args_parser.hpp."
        ),
        params_json_schema=_WRITE_FILE_SCHEMA,
        on_invoke_tool=on_invoke,
        defer_loading=False,  # always shown to the model
    )


class ReadFileArgs(BaseModel):
    path: str = Field(..., description="Path relative to workspace root")
    offset: int | None = Field(
        None,
        description="1-based line number to start reading from (optional, default 1)",
    )
    limit: int | None = Field(
        None, description="Maximum number of lines to return (optional, default 2000)"
    )


def make_custom_openai_read_file_tool(
    editor: WorkspaceEditor,
) -> FunctionTool:
    impl = CustomReadFileTool(editor=editor)

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        try:
            args = ReadFileArgs.model_validate_json(args_json)
        except ValidationError as e:
            # See make_custom_openai_apply_patch_tool's on_invoke for why this is
            # reported back to the model instead of left to propagate and crash
            # the run.
            logger.warning(
                "read_file received arguments that failed schema validation: %s", e
            )
            return (
                "Error: read_file arguments failed validation. Retry with a valid "
                "path (str) and, if provided, integer offset/limit. "
                f"Details: {e}"
            )
        return await impl(args.path, args.offset, args.limit)

    return FunctionTool(
        name="read_file",
        description=(
            "Reads a file's contents with line numbers (like `cat -n`). Prefer "
            "this over `shell cat` for viewing a file you plan to edit - the line "
            "numbers make it easy to build a precise replace_in_file or "
            "apply_patch edit. Returns up to 2000 lines by default; use "
            "offset/limit to page through a larger file."
        ),
        params_json_schema=ReadFileArgs.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=False,  # always shown to the model
    )
