"""Assemble the generated parts of a Rust engine workspace.

The Rust counterpart of ``assemble_query_impl.py`` + ``assemble_args_parser.py``.
The *logic* (which queries exist, what their placeholders are) is the same in
both languages; only the emitted text differs, so this module deliberately
mirrors those two file-for-file rather than inventing a new shape.
"""

import string
from pathlib import Path
from typing import Callable

_TEMPLATES = Path(__file__).parent / "templates" / "rust"

# The Rust type each placeholder value maps to, mirroring _CPP_TYPE.
_RUST_TYPE = {str: "String", int: "i64", float: "f64"}

# How a value of that type is read off the request line (synno_rt::args::ArgScanner).
_RUST_SCAN = {str: "string", int: "int", float: "float"}


def assemble_query_lib_file(query_list: list[str]) -> str:
    """query/src/lib.rs: the module list and the dispatch arm per query."""
    template = (_TEMPLATES / "query" / "lib.rs").read_text()

    modules = "\n".join(f"pub mod q{qid};" for qid in query_list)
    template = _replace_marker(template, "// <<query_modules>>", modules)

    # One dispatch arm per query: parse the args, then run it. The arms all
    # evaluate to Result<RecordBatch>, which the caller writes out.
    arms = "\n".join(
        f'            "{qid}" => args::parse_q{qid}(&req.line)\n'
        f"                .and_then(|a| q{qid}::run_q{qid}(db, &a)),"
        for qid in query_list
    )
    # Every arm evaluates to Result<RecordBatch>; the `?` after the match is in
    # the template, so the arms must not each carry one.
    template = _replace_marker(template, "            // <<impl_fn_calls>>", arms)
    return template


def assemble_args_file(
    query_ids: list[str],
    gen_placeholders_fn: Callable,
    query_name_fn: Callable[[str], str] | None = None,
) -> str:
    """query/src/args.rs: a struct + parser per query.

    ``query_name_fn`` only controls the key used to look up a query's placeholder
    spec (``Q1`` for TPC-H, ``STQ1`` for single-table TPC-H); the emitted struct
    names always use the bare query id, to match ``run_q<id>``.
    """
    if query_name_fn is None:
        query_name_fn = lambda q_id: f"Q{q_id}"  # noqa: E731

    template = (_TEMPLATES / "query" / "args.rs").read_text()
    blocks = "\n".join(
        _gen_query_block(q_id, gen_placeholders_fn(query_name=query_name_fn(q_id)))
        for q_id in query_ids
    )
    return string.Template(template).substitute(query_structs_and_parsers=blocks)


def assemble_loader_file(table_names: list[str]) -> str:
    """loader/src/lib.rs: the ParquetTables fields, plus the shm/parquet plane branch.

    Mirrors ``prepare_workspace_olap._gen_table_reads``: one binary serves both
    planes, and the choice is made at run time by the ``SYNNODB_SHM_INGEST`` env.
    """
    template = (_TEMPLATES / "loader" / "lib.rs").read_text()

    defs = "\n".join(
        f"    pub {name}: std::sync::Arc<RecordBatch>," for name in table_names
    )

    shm_reads = "\n".join(
        f'            {name}: synno_rt::shm::read_table('
        f'&synno_rt::shm::ingest_path_for("{name}"))?,'
        for name in table_names
    )
    parquet_reads = "\n".join(
        f'            {name}: read_parquet_table(&format!("{{path}}{name}.parquet"))?,'
        for name in table_names
    )
    reads = (
        "    if synno_rt::shm::ingest_enabled() {\n"
        "        Ok(Box::new(ParquetTables {\n"
        f"{shm_reads}\n"
        "        }))\n"
        "    } else {\n"
        "        Ok(Box::new(ParquetTables {\n"
        f"{parquet_reads}\n"
        "        }))\n"
        "    }"
    )

    template = replace_marked_block(template, "table-defs", defs)
    template = replace_marked_block(template, "table-reads", reads)
    return template


def assemble_query_files(
    query_ids: list[str], sql_dict: dict[str, str]
) -> dict[str, str]:
    """query/src/q<N>.rs, one per query, from the template."""
    template = string.Template((_TEMPLATES / "query" / "qX.rs").read_text())
    out: dict[str, str] = {}
    for qid in query_ids:
        assert not qid.startswith("Q"), f"Query id should not start with 'Q': {qid}"
        # The SQL goes into a `//` comment block, so a blank line inside it would
        # end the comment and break the file.
        sql = sql_dict[f"Q{qid}"].strip().replace("\n", "\n// ")
        out[f"query/src/q{qid}.rs"] = template.substitute(qid=qid, query_sql=sql)
    return out


# ------------------------------------------------------------------ internals --
def _replace_marker(text: str, marker: str, replacement: str) -> str:
    assert marker in text, f"marker not found in the Rust template: {marker!r}"
    return text.replace(marker, replacement)


def replace_marked_block(text: str, marker_name: str, replacement: str) -> str:
    """Replace the body between ``// start: NAME`` and ``// end: NAME``.

    The same convention as the C++ scaffold (``//`` is a comment in both), so the
    marker blocks read identically in either language.
    """
    from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import (
        replace_cpp_marked_block,
    )

    return replace_cpp_marked_block(text, marker_name, replacement)


def _field_decl(placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f"    pub {placeholder}: Vec<String>,"
    return f"    pub {placeholder}: {_RUST_TYPE[type(value)]},"


def _field_parser(placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f'        {placeholder}: scan.in_list("{placeholder}")?,'
    scan = _RUST_SCAN[type(value)]
    return f'        {placeholder}: scan.{scan}("{placeholder}")?,'


def _gen_query_block(q_id: str, placeholders_dict: dict) -> str:
    fields = "\n".join(_field_decl(p, v) for p, v in placeholders_dict.items())
    parsers = "\n".join(_field_parser(p, v) for p, v in placeholders_dict.items())
    return f"""\
// Q{q_id}
#[derive(Debug, Default, Clone)]
pub struct Q{q_id}Args {{
{fields}
}}

pub fn parse_q{q_id}(line: &str) -> Result<Q{q_id}Args> {{
    let mut scan = ArgScanner::new(line, "{q_id}");
    Ok(Q{q_id}Args {{
{parsers}
    }})
}}
"""
