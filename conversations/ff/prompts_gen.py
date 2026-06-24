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


def base_ff_impl_storage(
    builder_cpp_path: str,
    builder_hpp_path: str,
    query_impl_path: str,
    base_impl_todo_file: str,
    args_path: str,
) -> str:
    prompt_path = _PROMPTS_DIR / "base_ff_impl_storage.txt"
    template = Template(_load_txt(prompt_path))
    return template.substitute(
        builder_cpp_path=builder_cpp_path,
        builder_hpp_path=builder_hpp_path,
        query_impl_path=query_impl_path,
        base_impl_todo_file=base_impl_todo_file,
        args_path=args_path,
    )


def base_ff_impl_query_prompt(
    is_first_query: bool,
    sample_query_args_dict: dict | None,
    query_id: str,
    args_path: str,
    builder_path: str,
    query_impl_path: str,
    sql: str,
) -> str:
    prompt_path = _PROMPTS_DIR / "base_ff_impl_query.txt"
    template = Template(_load_txt(prompt_path))

    if is_first_query:
        prefix = "Lets start implementing the query execution logic. Implement all queries in the next steps step by step. Start with"
    else:
        prefix = "Next, continue implementing the query execution logic for"

    if sample_query_args_dict is not None and query_id in sample_query_args_dict:
        sample_args_str = f" Example instantiation of the query placeholders are:\n{sample_query_args_dict[query_id]}\nNULL values might appear in IN-Lists and are represented with the string '<<NULL>>'."
    else:
        sample_args_str = ""

    return template.substitute(
        prefix=prefix,
        query_id=query_id,
        sample_args_str=sample_args_str,
        args_path=args_path,
        builder_path=builder_path,
        query_impl_path=query_impl_path,
        sql=sql,
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
        storage_hint = f"The bespoke file-format plan is described in the file `{storage_plan_path}`. It specifies the on-disk layout: per-column physical types and encodings, row-group/page organization, footer metadata, and the pruning structures (zone maps, bitsets, dictionaries, ...) that fit within the RAM budget. Declare the matching on-disk handles in `bff_format.hpp` and make the writer stream each table's Arrow row batches into the `.bff` files exactly as the plan describes, recording the footer/page metadata the reader needs. "

    else:
        storage_hint = """Choose a compact per-column physical type and encoding for each column the queries touch, and lay each table out as row groups of column pages with a self-describing footer carrying the schema plus per-row-group and per-page min/max/null stats (the footer is the reader's main pruning surface). Write one `.bff` file per table, struct-of-arrays / columnar, with variable-length strings stored as offsets + bytes (optionally dictionary-encoded). The writer must stream the Arrow tables row-group by row-group rather than assuming the whole dataset fits in RAM, and must record enough footer/page metadata for the read API to prune row groups/pages and locate every column buffer."""

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
