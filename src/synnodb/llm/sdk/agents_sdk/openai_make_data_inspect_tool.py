from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from synnodb.tools.data_inspect import DataInspectTool


class QueryDataArgs(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "Any read-only SQL query to execute against the actual benchmark data via DuckDB. "
            "You can also run SUMMARIZE to get data statistics and DESCRIBE to show a table's "
            "schema. Reference tables by their real names."
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
    "Runs a strictly read-only SQL query against the real benchmark data using DuckDB, at the "
    "workload's benchmark scale factor - the same data the correctness oracle uses. Use it to "
    "inspect the data you are building an engine for: row counts, value distributions, distinct "
    "counts, null density, min/max ranges, and join fan-out, so physical-design choices (element "
    "types, encodings, partitioning, join order) are grounded in the actual data. Only "
    "SELECT-family statements are allowed; it cannot modify data."
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
