from pathlib import Path
from string import Template

from conversations.prompts_gen import _load_txt

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def gen_ff_plan_prompt(
    queries_filename: str,
    schema: str,
    file_format_plan_filename: str,
) -> str:
    prompt_path = _PROMPTS_DIR / "gen_ff_plan.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        queries_path=queries_filename,
        schema=schema,
        file_format_plan_filename=file_format_plan_filename,
    )


def base_ff_planner_prompt(
    queries_path: str,
    builder_path: str,
    num_queries: int,
    read_storage_plan: bool,
    storage_plan_path: str,
    query_impl_path: str,
    example_query: str,
    example_query_params: str,
    args_path: str,
    parquet_path: str,
    base_impl_todo_file: str,
) -> str:

    prompt_path = _PROMPTS_DIR / "base_ff_planner.txt"
    template_str = _load_txt(prompt_path)

    template = Template(template_str)

    query_str = f"{num_queries} {'query' if num_queries == 1 else 'queries'}"

    if read_storage_plan:
        storage_hint = f"The storage plan is described in the file `{storage_plan_path}`. It describes the SSD-backed columnar storage layout: which columns to serialize to binary files, their sort order, and any zone-map or acceleration structures that fit within the RAM budget. Implement the ColumnHandle<T> and StringColumnHandle fields in the Database struct according to this plan, and make sure build() streams Parquet row groups and writes/registers every referenced persisted column. "

    else:
        storage_hint = """Use ColumnHandle<T> (from column_handle.hpp) for page-safe fixed-width numeric columns and StringColumnHandle for variable-length string columns. The minimum should be one binary file per page-safe fixed-width column and offsets + bytes files for each string column, struct-of-arrays layout. Flat fixed-width storage is valid only when BP_PAGE_BYTES % sizeof(T) == 0; otherwise use StringColumnHandle or the page-aligned fixed-char helpers. The Database struct must declare a handle for every column needed by the queries, and build() must stream Parquet row groups, serialize, register, and assign each handle."""

    return template.substitute(
        queries_path=queries_path,
        query_str=query_str,
        builder_path=builder_path,
        storage_hint=storage_hint,
        query_impl_path=query_impl_path,
        example_query=example_query,
        example_query_params=example_query_params,
        args_path=args_path,
        parquet_path=parquet_path,
        base_impl_todo_file=base_impl_todo_file,
    )
