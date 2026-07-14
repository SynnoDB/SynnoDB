"""One construction path for every FunctionTool the model can call, so that a malformed
tool call comes back as a tool result instead of killing the run.

The agents SDK wraps anything raised out of ``on_invoke_tool`` in an error that propagates
up and ends the conversation. Its own ``function_tool`` decorator installs a net against
that (``agents.tool.default_tool_error_function``), but tools built by constructing
``FunctionTool(...)`` directly - which is what this package does, to control the schema and
the result shape - opt out of it. Guarding each tool by hand instead means the guard is
opt-in, and a tool added without one takes down a run the first time a model mis-shapes its
arguments. Hence: every tool is built through ``make_guarded_function_tool``, which owns the
argument-validation guard.

Only argument validation is guarded here. An exception from the tool body is a real fault and
still fails the run fast, rather than being laundered into a message the model would retry
against forever.
"""

import json
import logging
from typing import Any, Awaitable, Callable

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, ValidationError

from synnodb.tools.workspace_editor import WorkspaceEditor

logger = logging.getLogger(__name__)


def summarize_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a short, human-readable reason for the
    live-ui (the full error is still returned to the model). Missing required
    fields - by far the common case (an omitted ``type``) - are reported explicitly."""
    missing = [
        ".".join(str(p) for p in err["loc"])
        for err in e.errors()
        if err.get("type") == "missing"
    ]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err.get('type', 'invalid')}"
        for err in e.errors()
    )


def extract_path_field(args_json: str, field: str) -> str | None:
    """Best-effort read of the file path from a rejected call so the live-ui can name
    the file it was aimed at. The arguments failed validation, so nothing about them can
    be assumed: returns None if they are not a JSON object or carry no string path."""
    try:
        data = json.loads(args_json)
    except (json.JSONDecodeError, TypeError):
        return None
    path = data.get(field) if isinstance(data, dict) else None
    return path if isinstance(path, str) and path else None


class RejectionRecorder:
    """How a tool records a call it rejected at argument validation.

    Needed because ``RunStatsCollector.on_tool_end`` emits an edit metric for every
    apply_patch/replace_in_file/write_file call, built from state that only the
    ``WorkspaceEditor`` writes. A tool that returns before reaching the editor therefore
    emits ``rejected=false, failed=None, +0/-0`` - the live-ui renders the rejected call
    the model saw as a clean, successful no-op. Recording the rejection closes that gap.

    The editor-less tools (compile, run, shell, query_data) pass no recorder: their
    metrics are emitted from inside the tool body, which a rejected call never reaches,
    so such a call is simply absent from the live-ui. Absent is survivable - misreported
    as a successful edit is not - hence only the editor tools record here.
    """

    def validate_args_model(self, args_model: type[BaseModel]) -> None:
        """Assert the recorder can actually read what it needs off this tool's arguments.
        Called once at construction: a recorder looking for a field the model does not
        have degrades silently (a rejection recorded with no path), so it is pinned here
        rather than discovered as a null path in the live-ui."""

    def replay(self, tool_name: str, args_json: str) -> str | None:
        """The tool result to return without re-validating, if this exact call was
        recorded as a rejection when the run was recorded; otherwise None."""
        return None

    def record(self, tool_name: str, args_json: str, reason: str, message: str) -> None:
        raise NotImplementedError


class EditRejectionRecorder(RejectionRecorder):
    """Rejection recorder for the edit tools (apply_patch, replace_in_file, write_file).

    ``path_field`` is the argument these tools name their target file with - ``path`` for
    apply_patch/write_file, ``file_path`` for replace_in_file - read best-effort so the
    live-ui can name the file the rejected call was aimed at.
    """

    def __init__(self, editor: WorkspaceEditor, path_field: str) -> None:
        self._editor = editor
        self._path_field = path_field

    def validate_args_model(self, args_model: type[BaseModel]) -> None:
        assert self._path_field in args_model.model_fields, (
            f"{args_model.__name__} has no field {self._path_field!r}: the recorder would "
            "record every rejection of this tool with a null path, and the live-ui could "
            "not name the file the call was aimed at."
        )

    def replay(self, tool_name: str, args_json: str) -> str | None:
        return self._editor.replay_rejected_call(tool_name, args_json)

    def record(self, tool_name: str, args_json: str, reason: str, message: str) -> None:
        self._editor.record_rejected_call(
            tool_name,
            args_json,
            extract_path_field(args_json, self._path_field),
            reason,
            message,
        )


class ReadRejectionRecorder(RejectionRecorder):
    """Rejection recorder for read_file, whose metric has no rejected flag: its
    ``read_file/output`` already carries the error string returned to the model, so all a
    rejected read loses is the path it was aimed at. Recording that keeps the step
    diagnosable. Nothing is cached - there is no side effect to replay."""

    def __init__(self, editor: WorkspaceEditor) -> None:
        self._editor = editor

    def validate_args_model(self, args_model: type[BaseModel]) -> None:
        assert "path" in args_model.model_fields, (
            f"{args_model.__name__} has no field 'path': a rejected read would be logged "
            "with a null path, losing the one thing that makes the step diagnosable."
        )

    def record(self, tool_name: str, args_json: str, reason: str, message: str) -> None:
        path = extract_path_field(args_json, "path")
        if path is not None:
            self._editor.record_attempted_read(path)


def make_guarded_function_tool(
    *,
    name: str,
    description: str,
    args_model: type[BaseModel],
    handler: Callable[[RunContextWrapper[Any], Any], Awaitable[Any]],
    retry_hint: str,
    params_json_schema: dict[str, Any] | None = None,
    render_error: Callable[[str], Any] | None = None,
    rejection: RejectionRecorder | None = None,
    defer_loading: bool = False,
) -> FunctionTool:
    """Build a FunctionTool whose arguments are validated behind a guard.

    On a schema-invalid call the model gets ``retry_hint`` - a plain statement of what the
    tool actually takes - plus the pydantic error, as an ordinary tool result, and the tool
    stays usable for the retry. ``retry_hint`` matters: a model that has confused two tools
    cannot tell from a pydantic dump alone which one it wanted.

    ``render_error`` adapts that message into the tool's own result shape (shell reports a
    failed command, not a string), so the model reads a rejected call the way it reads any
    other failure. ``params_json_schema`` overrides the schema derived from ``args_model``
    for tools that hand-write one.
    """
    assert name, "A tool needs a name: it keys the rejection cache and every metric."
    assert retry_hint, (
        f"{name} has no retry_hint. The hint is the guard's whole point - a model that has "
        "confused two tools cannot tell from a pydantic dump alone which one it wanted."
    )

    if params_json_schema is not None:
        # The model is TOLD params_json_schema but is VALIDATED against args_model, so the
        # two are one contract written twice. Let them drift - a field added to the model
        # but not the schema - and the tool advertises arguments that can never validate:
        # every call rejected, every turn, with the model unable to see why. Pin it at
        # construction (import time) rather than mid-run.
        advertised = set(params_json_schema.get("properties", {}))
        declared = set(args_model.model_fields)
        assert advertised == declared, (
            f"{name}: hand-written schema and {args_model.__name__} disagree on fields "
            f"(schema-only: {sorted(advertised - declared)}, "
            f"model-only: {sorted(declared - advertised)})."
        )
        required_by_schema = set(params_json_schema.get("required", []))
        required_by_model = {
            field
            for field, spec in args_model.model_fields.items()
            if spec.is_required()
        }
        assert required_by_schema == required_by_model, (
            f"{name}: hand-written schema and {args_model.__name__} disagree on which "
            f"fields are required (schema: {sorted(required_by_schema)}, "
            f"model: {sorted(required_by_model)}). A field the model is not told to send "
            "but is validated on rejects every call it makes."
        )

    if rejection is not None:
        rejection.validate_args_model(args_model)

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> Any:
        if rejection is not None:
            # Replay a recorded rejection BEFORE re-validating: the verdict from when the
            # run was recorded wins over the current rules, so a later change to what
            # counts as an invalid call cannot alter an already-recorded run.
            replayed = rejection.replay(name, args_json)
            if replayed is not None:
                assert isinstance(replayed, str), (
                    f"{name}: replayed rejection is {type(replayed).__name__}, not the "
                    "tool message that was recorded - the cache entry is corrupt."
                )
                return replayed if render_error is None else render_error(replayed)

        try:
            args = args_model.model_validate_json(args_json)
        except ValidationError as e:
            logger.warning("%s received arguments that failed validation: %s", name, e)
            message = (
                f"Error: {name} arguments failed validation. {retry_hint}\nDetails: {e}"
            )
            if rejection is not None:
                rejection.record(
                    name, args_json, summarize_validation_error(e), message
                )
            return message if render_error is None else render_error(message)

        return await handler(ctx, args)

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=params_json_schema or args_model.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,
    )
