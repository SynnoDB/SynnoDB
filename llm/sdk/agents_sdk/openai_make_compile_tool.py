from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from tools.compile import CompileTool


class CompileArgs(BaseModel):
    optimize: bool = Field(..., description="Enable compiler optimization")


def make_openai_compile_tool(
    compile_tool: CompileTool,
    defer_loading: bool = False,
) -> FunctionTool:
    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        args = CompileArgs.model_validate_json(args_json)
        return compile_tool(optimize=args.optimize)

    return FunctionTool(
        name="compile",
        description="Compiles the database",
        params_json_schema=CompileArgs.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,  # loaded when needed
    )
