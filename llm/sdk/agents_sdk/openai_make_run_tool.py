from typing import Any, List

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from tools.run import RunTool


class RunArgs(BaseModel):
    scale_factor: int = Field(..., ge=1, description="Scale factor (>= 1)")
    optimize: bool = Field(..., description="Enable compiler optimization")
    query_id: List[str] | None = Field(
        None,
        description="List of Query-IDs to execute. None means all queries.",
    )


class IMDBRunArgs(BaseModel):
    scale_factor: float = Field(..., gt=0, description="Scale factor (> 0)")
    optimize: bool = Field(..., description="Enable compiler optimization")
    query_id: List[str] | None = Field(
        None,
        description="List of Query-IDs to execute. None means all queries.",
    )


trace_flag_description = "Whether to set TRACE flag for the run (setting cxx flag -DTRACE, e.g. enables collecting execution statistics for code optimization if implemented in the codebase)"


class RunArgsTrace(RunArgs):
    trace_mode: bool = Field(
        False,
        description=trace_flag_description,
    )


class IMDBRunArgsTrace(IMDBRunArgs):
    trace_mode: bool = Field(
        False,
        description=trace_flag_description,
    )


def make_openai_run_tool(
    run_tool: RunTool,
    run_tool_offer_trace_option: bool = False,
    defer_loading: bool = False,
) -> FunctionTool:
    def get_args_model():
        if run_tool.dataset_name == "imdb":
            return IMDBRunArgsTrace if run_tool_offer_trace_option else IMDBRunArgs
        else:
            return RunArgsTrace if run_tool_offer_trace_option else RunArgs

    args_model = get_args_model()

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        args = args_model.model_validate_json(args_json)

        if run_tool_offer_trace_option:
            return run_tool(
                scale_factor=args.scale_factor,
                optimize=args.optimize,
                query_id=args.query_id,
                trace_mode=args.trace_mode,  # type: ignore
            )
        else:
            return run_tool(
                scale_factor=args.scale_factor,
                optimize=args.optimize,
                query_id=args.query_id,
            )

    return FunctionTool(
        name="run",
        description="Runs the database and executes a query by query-id",
        params_json_schema=args_model.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,  # loaded when needed
    )
