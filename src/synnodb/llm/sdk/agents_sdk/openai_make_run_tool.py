from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.tools.run import RunTool


class RunArgs(BaseModel):
    mode: str = Field(
        ...,
        description="Mode of operation e.g., 'fast_check' (quick validation check), 'exhaustive' (full validation), 'benchmark' (performance testing), 'ingest' (ingestion time measurement)",
    )
    optimize: bool = Field(..., description="Enable compiler optimization")
    query_ids: list[str] | None = Field(
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
                mode=args.mode,
                optimize=args.optimize,
                query_ids=args.query_ids,
                trace_mode=args.trace_mode,  # type: ignore
            )
        else:
            return run_tool(
                mode=args.mode,
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
