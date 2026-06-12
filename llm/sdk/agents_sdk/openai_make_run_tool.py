from typing import Any, List

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from tools.run import RunTool


class RunArgs(BaseModel):
    fast_check: bool = Field(
        ...,
        description="Whether to perform a fast check run (e.g., using a smaller scale factor or a subset of queries). Turn off for performance measurements with the full dataset and all queries.",
    )
    optimize: bool = Field(..., description="Enable compiler optimization")
    query_ids: List[str] | None = Field(
        None,
        description="List of Query-IDs to execute. None means all queries. Example: ['1', '2b', '5']",
    )


trace_flag_description = "Whether to set TRACE flag for the run (setting cxx flag -DTRACE, e.g. enables collecting execution statistics for code optimization if implemented in the codebase)"


class RunArgsTrace(RunArgs):
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
        return RunArgsTrace if run_tool_offer_trace_option else RunArgs

    args_model = get_args_model()

    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        args = args_model.model_validate_json(args_json)

        if run_tool_offer_trace_option:
            return run_tool(
                fast_check=args.fast_check,
                optimize=args.optimize,
                query_ids=args.query_ids,
                trace_mode=args.trace_mode,  # type: ignore
            )
        else:
            return run_tool(
                fast_check=args.fast_check,
                optimize=args.optimize,
                query_ids=args.query_ids,
            )

    return FunctionTool(
        name="run",
        description="Runs the database and executes a query by query-id",
        params_json_schema=args_model.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,  # loaded when needed
    )
