from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.llm.sdk.agents_sdk.guarded_tool import make_guarded_function_tool
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

    async def run(ctx: RunContextWrapper[Any], args: RunArgs) -> str:
        if run_tool_offer_trace_option:
            # The guard validated against args_model, which only carries trace_mode when
            # the trace option is on. Reading it off a plain RunArgs would be an
            # AttributeError from inside the tool body - past the guard, so it kills the
            # run rather than coming back as a tool result.
            assert isinstance(args, RunArgsTrace), (
                "run offers the trace option but was built with args model "
                f"{type(args).__name__}, which has no trace_mode."
            )
            return run_tool(
                mode=args.mode,
                optimize=args.optimize,
                query_ids=args.query_ids,
                trace_mode=args.trace_mode,
            )
        else:
            return run_tool(
                mode=args.mode,
                optimize=args.optimize,
                query_ids=args.query_ids,
            )

    retry_hint = (
        "Retry with mode (str: fast_check, exhaustive, benchmark or ingest), optimize "
        "(bool) and, if provided, query_ids (list of str)."
    )
    if run_tool_offer_trace_option:
        # Named only when it is actually offered: a hint for an argument this build of the
        # tool does not accept would send the model straight into another rejected call.
        retry_hint += " trace_mode (bool) is optional."

    return make_guarded_function_tool(
        name="run",
        description="Runs the database and executes a query by query-id",
        args_model=args_model,
        handler=run,
        retry_hint=retry_hint,
        defer_loading=defer_loading,  # loaded when needed
    )
