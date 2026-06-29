import logging
from typing import Any, Dict

from agents import (
    ShellActionRequest,
    ShellCallData,
    ShellCommandRequest,
)
from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.tools.custom_apply_patch import CustomApplyPatchTool
from synnodb.tools.custom_replace_in_file import CustomReplaceInFileTool
from synnodb.tools.shell_executor import ShellExecutor
from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


# class CustomShellTool:
#     def __init__(
#         self,
#         cwd: Path,
#         cache_dir: Path,
#         do_not_cache: bool,
#         git_snapshotter: Optional[GitSnapshotter] = None,
#         wandb_metrics_hook: WandbRunHook | None = None,
#     ) -> None:
#         self.cwd = cwd
#         self.cache_dir = cache_dir
#         self.do_not_cache = do_not_cache
#         self.git_snapshotter = git_snapshotter
#         self.wandb_metrics_hook = wandb_metrics_hook
#         utils.create_dir_and_set_permissions(self.cache_dir)

#     def _cache_path_for(self, hash: str) -> Path:
#         return self.cache_dir / f"{hash}.pkl"

#     async def __call__(self, command: str, timeout_ms: int | None) -> str:
#         if "sudo" in command:
#             raise RuntimeError("sudo rejected")
#         logger.debug(f"Running shell command: {command}")

#         payload = {
#             "snapshotter_hash": self.git_snapshotter.current_hash
#             if self.git_snapshotter
#             else None,
#             "command": command,
#             "timeout_ms": timeout_ms,
#         }
#         hash = utils.sha256(utils.stable_json(payload))
#         path = self._cache_path_for(hash)

#         if path.exists():
#             cached = utils.load_pickle(path, str)
#             if cached is not None:
#                 return cached

#         cfg = SandboxConfig(
#             writable_roots=[str(self.cwd), "/tmp"],
#             cwd=str(self.cwd),
#             nproc=None,
#         )
#         proc = await sandbox_shell_async(
#             command,
#             cfg=cfg,
#             env=os.environ.copy(),
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE,
#         )
#         timed_out = False
#         try:
#             timeout = (timeout_ms or 0) / 1000 or None
#             stdout_bytes, stderr_bytes = await asyncio.wait_for(
#                 proc.communicate(), timeout=timeout
#             )
#         except asyncio.TimeoutError:
#             proc.kill()
#             stdout_bytes, stderr_bytes = await proc.communicate()
#             timed_out = True

#         stdout = stdout_bytes.decode("utf-8", errors="ignore")
#         stderr = stderr_bytes.decode("utf-8", errors="ignore")
#         exit_code = getattr(proc, "returncode", None)

#         output = (
#             f"$ {command}\n"
#             f"stdout: {stdout[:200]}\n"
#             f"stderr: {stderr[:200]}\n"
#             f"exit_code: {exit_code}\n"
#             f"status: {'timeout' if timed_out else 'exit'}"
#         )

#         if path is not None and not self.do_not_cache:
#             utils.dump_pickle(
#                 path,
#                 output,
#                 do_not_cache=self.do_not_cache,
#             )

#         if self.wandb_metrics_hook is not None:
#             self.wandb_metrics_hook.log_metrics_callback(
#                 {
#                     "type": "shell",
#                     "shell/num_commands": 1,
#                     "shell/commands": [command[:20]],
#                 },
#                 log_and_increment=True,
#             )

#         return output


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
        args = CustomApplyPatchArgs.model_validate_json(args_json)
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
            "description": "The replacement text (use \"\" only to delete the matched text).",
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
