import string
from pathlib import Path
from typing import Callable

_ARGS_PARSER_TEMPLATE = Path(__file__).parent / "templates" / "args_parser.hpp"
_CPP_TYPE = {str: "std::string", int: "int", float: "float"}


def assemble_args_parser_file(
    query_ids: list[str], gen_placeholders_fn: Callable
) -> str:
    assert _ARGS_PARSER_TEMPLATE.is_file(), (
        f"Args parser template not found: {_ARGS_PARSER_TEMPLATE}"
    )
    args_parser_content = _ARGS_PARSER_TEMPLATE.read_text()

    query_blocks = "\n".join(
        _gen_query_block(q_id, gen_placeholders_fn(query_name=f"Q{q_id}"))
        for q_id in query_ids
    )
    out_str = string.Template(args_parser_content).substitute(
        query_structs_and_parsers=query_blocks
    )
    return out_str


def _field_decl(placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f"    std::vector<std::string> {placeholder};"
    return f"    {_CPP_TYPE[type(value)]} {placeholder};"


def _field_parser(q_id: str, placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f"\targs.{placeholder} = parse_in_list(iss);"
    access = (
        f"std::quoted(args.{placeholder})"
        if isinstance(value, str)
        else f"args.{placeholder}"
    )
    return (
        f"\tif (!(iss >> {access})) {{\n"
        f'\t\tthrow std::runtime_error("Q{q_id}: failed to parse {placeholder}");\n'
        f"\t}}"
    )


def _gen_query_block(q_id: str, placeholders_dict: dict) -> str:
    qn = f"Q{q_id}"
    fields = "\n".join(_field_decl(p, v) for p, v in placeholders_dict.items())
    parsers = "\n".join(_field_parser(q_id, p, v) for p, v in placeholders_dict.items())
    return f"""\
//{qn}
struct {qn}Args {{
{fields}
}};

inline {qn}Args parse_{qn.lower()}(const QueryRequest& request) {{
    {qn}Args args;
    std::istringstream iss(request.line);

{parsers}

    return args;
}}
"""
