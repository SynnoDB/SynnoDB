from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.tools.data_inspect import DataInspectTool


class QueryDataArgs(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "A simple, cheap read-only SQL query (DuckDB). Prefer SUMMARIZE/DESCRIBE, WHERE, or "
            "LIMIT over scanning or joining large tables. Reference tables by their real names."
        ),
    )
    max_rows: int | None = Field(
        None,
        description=(
            "Maximum number of result rows to return (default 100, capped at 1000). Use "
            "aggregation or LIMIT for large results."
        ),
    )


DESCRIPTION = (
    "Read-only SQL over a small, representative subset of the benchmark data (DuckDB) - quick "
    "look-ups to ground physical-design choices (types, encodings, partitioning, join order): row "
    "counts, distributions, distinct/null counts, min/max ranges, join fan-out. Keep queries "
    "simple (SUMMARIZE/DESCRIBE, WHERE, LIMIT); one that scans or joins large tables and runs too "
    "long is cancelled. SELECT-family only; it cannot modify data."
)


def make_openai_data_inspect_tool(
    data_inspect_tool: DataInspectTool,
    defer_loading: bool = False,
) -> FunctionTool:
    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        args = QueryDataArgs.model_validate_json(args_json)
        return data_inspect_tool(sql=args.sql, max_rows=args.max_rows)

    return FunctionTool(
        name="query_data",
        description=DESCRIPTION,
        params_json_schema=QueryDataArgs.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,  # loaded when needed
    )
