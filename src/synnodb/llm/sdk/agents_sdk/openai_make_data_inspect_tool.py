import logging
from typing import Any

from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool
from pydantic import BaseModel, Field, ValidationError

from synnodb.tools.data_inspect import DataInspectTool
from synnodb.workloads.workload_spec import format_subset_menu

logger = logging.getLogger(__name__)


class QueryDataArgs(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "A simple, cheap read-only SQL query (DuckDB). One statement per call - send a batch "
            "like `SUMMARIZE a; SUMMARIZE b` as separate calls. Prefer SUMMARIZE/DESCRIBE, WHERE, "
            "or LIMIT over scanning or joining large tables. Reference tables by their real names."
        ),
    )
    max_rows: int | None = Field(
        None,
        description=(
            "Maximum number of result rows to return (default 100, capped at 1000). Use "
            "aggregation or LIMIT for large results."
        ),
    )
    full_dataset: bool = Field(
        False,
        description=(
            "Run against the full dataset instead of the small sample. Default false (the sample), "
            "which is far cheaper - prefer it. Set true only for numbers a sample cannot give you: "
            "real row counts, min/max ranges and distinct counts, which a sample understates."
        ),
    )


DESCRIPTION = (
    "Read-only SQL over the benchmark data (DuckDB) - quick look-ups to ground physical-design "
    "choices (types, encodings, partitioning, join order): row counts, distributions, "
    "distinct/null counts, min/max ranges, join fan-out. Keep queries simple (SUMMARIZE/DESCRIBE, "
    "WHERE, LIMIT); one that scans or joins large tables and runs too long is cancelled. "
    "SELECT-family only; it cannot modify data."
)


def _description(data_inspect_tool: DataInspectTool) -> str:
    """The tool description with the sample-vs-full-dataset note appended. Built per run rather
    than kept as a module constant: whether both datasets are materialized depends on the workload
    and on what is on disk, and the agent must not be offered one it cannot read."""
    menu = format_subset_menu(
        available=data_inspect_tool.available_subsets(),
        sample_sf=data_inspect_tool.sample_sf,
        full_sf=data_inspect_tool.full_sf,
    )
    return f"{DESCRIPTION} {menu}" if menu else DESCRIPTION


def make_openai_data_inspect_tool(
    data_inspect_tool: DataInspectTool,
    defer_loading: bool = False,
) -> FunctionTool:
    async def on_invoke(ctx: RunContextWrapper[Any], args_json: str) -> str:
        try:
            args = QueryDataArgs.model_validate_json(args_json)
        except ValidationError as e:
            # Report a malformed call back to the model as a tool result - matching how this tool
            # surfaces every other failure (bad SQL, a write, a timeout) - so it can retry instead
            # of the ValidationError propagating and killing the run. See
            # ``openai_sdk_tools.make_custom_openai_apply_patch_tool`` for the same guard.
            logger.warning(
                "query_data received arguments that failed schema validation: %s", e
            )
            return (
                "Error: query_data arguments failed validation. Retry with sql (str) and, if "
                "provided, an integer max_rows and a boolean full_dataset.\n"
                f"Details: {e}"
            )
        return data_inspect_tool(
            sql=args.sql, max_rows=args.max_rows, full_dataset=args.full_dataset
        )

    return FunctionTool(
        name="query_data",
        description=_description(data_inspect_tool),
        params_json_schema=QueryDataArgs.model_json_schema(),
        on_invoke_tool=on_invoke,
        defer_loading=defer_loading,  # loaded when needed
    )
