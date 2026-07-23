from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.llm.sdk.agents_sdk.guarded_tool import make_guarded_function_tool
from synnodb.tools.compile import CompileTool


class CompileArgs(BaseModel):
    optimize: bool = Field(..., description="Enable compiler optimization")


def make_openai_compile_tool(
    compile_tool: CompileTool,
    defer_loading: bool = False,
) -> FunctionTool:
    async def run(ctx: RunContextWrapper[Any], args: CompileArgs) -> str:
        return compile_tool(optimize=args.optimize)

    return make_guarded_function_tool(
        name="compile",
        description="Compiles the database",
        args_model=CompileArgs,
        handler=run,
        retry_hint="Retry with optimize (bool).",
        defer_loading=defer_loading,  # loaded when needed
    )
