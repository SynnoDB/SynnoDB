"""Generate the typed per-query **output** struct (``Q<id>Out``) and its Arrow
conversion — the symmetric counterpart to ``assemble_args_parser.py``'s typed input
struct.

Today ``run_q<id>`` returns ``std::vector<std::vector<std::string>>`` and the result
is written as CSV — types are lost *inside* the query. The fix: the framework
generates, per query, a **struct of arrays** (one ``std::vector<T>`` per output
column, typed to the canonical DuckDB schema) that ``run_q<id>`` returns, and a
``to_arrow_q<id>`` that turns it into a typed ``arrow::Table`` (then written to shm
via ``WriteArrowTableToShm``). This makes egress typed (locked to DuckDB) and
near-zero-copy, with no CSV.

The column schema for each query is DuckDB's own ``description`` (name + type),
captured when the engine is built (and the runtime verifies it against the live DB).
"""
from __future__ import annotations

import keyword
import re
from typing import Dict, List, Sequence, Tuple

# DuckDB type (upper, base) -> (C++ element type, arrow type expr, arrow builder type)
_TYPE_MAP: Dict[str, Tuple[str, str, str]] = {
    "BIGINT": ("int64_t", "arrow::int64()", "arrow::Int64Builder"),
    "HUGEINT": ("int64_t", "arrow::int64()", "arrow::Int64Builder"),
    "LONG": ("int64_t", "arrow::int64()", "arrow::Int64Builder"),
    "INTEGER": ("int32_t", "arrow::int32()", "arrow::Int32Builder"),
    "INT": ("int32_t", "arrow::int32()", "arrow::Int32Builder"),
    "SMALLINT": ("int16_t", "arrow::int16()", "arrow::Int16Builder"),
    "TINYINT": ("int8_t", "arrow::int8()", "arrow::Int8Builder"),
    "DOUBLE": ("double", "arrow::float64()", "arrow::DoubleBuilder"),
    "FLOAT": ("double", "arrow::float64()", "arrow::DoubleBuilder"),
    "REAL": ("double", "arrow::float64()", "arrow::DoubleBuilder"),
    "BOOLEAN": ("bool", "arrow::boolean()", "arrow::BooleanBuilder"),
    "BOOL": ("bool", "arrow::boolean()", "arrow::BooleanBuilder"),
    "VARCHAR": ("std::string", "arrow::utf8()", "arrow::StringBuilder"),
    "TEXT": ("std::string", "arrow::utf8()", "arrow::StringBuilder"),
    "STRING": ("std::string", "arrow::utf8()", "arrow::StringBuilder"),
    # v1: DATE/DECIMAL carried as ISO/decimal *strings* (exact, DuckDB-comparable
    # under the tolerant cross-check) to avoid C++ decimal/date plumbing. Tighten later.
    "DATE": ("std::string", "arrow::utf8()", "arrow::StringBuilder"),
    "DECIMAL": ("std::string", "arrow::utf8()", "arrow::StringBuilder"),
}
_FALLBACK = ("std::string", "arrow::utf8()", "arrow::StringBuilder")


def _base_type(duckdb_type: str) -> str:
    return re.split(r"[(\s]", duckdb_type.strip().upper(), maxsplit=1)[0]


def map_type(duckdb_type: str) -> Tuple[str, str, str]:
    """Map a DuckDB type string to ``(cpp_elem, arrow_type_expr, arrow_builder)``."""
    return _TYPE_MAP.get(_base_type(duckdb_type), _FALLBACK)


def _sanitize(name: str, used: set) -> str:
    """Turn a DuckDB column name into a unique, valid C++ identifier."""
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = "c_" + ident
    if keyword.iskeyword(ident) or ident in {"int", "double", "float", "bool", "char"}:
        ident = ident + "_"
    candidate = ident
    i = 1
    while candidate in used:
        candidate = f"{ident}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def gen_output_block(query_id: str, columns: Sequence[Tuple[str, str]]) -> str:
    """Generate the ``Q<id>Out`` struct + ``to_arrow_q<id>`` for one query.

    ``columns`` is an ordered list of ``(column_name, duckdb_type)``.
    """
    qn = f"Q{query_id}"
    used: set = set()
    fields: List[str] = []
    builders: List[str] = []
    arrow_fields: List[str] = []
    arrays: List[str] = []
    for idx, (col_name, col_type) in enumerate(columns):
        cpp_field = _sanitize(col_name, used)
        cpp_elem, arrow_type, builder_type = map_type(col_type)
        fields.append(f"    std::vector<{cpp_elem}> {cpp_field};")
        builders.append(
            f"    {builder_type} b{idx};\n"
            f"    ARROW_RETURN_NOT_OK(b{idx}.AppendValues(out.{cpp_field}));\n"
            f"    std::shared_ptr<arrow::Array> a{idx};\n"
            f"    ARROW_RETURN_NOT_OK(b{idx}.Finish(&a{idx}));"
        )
        # Keep DuckDB's original column name in the Arrow schema (for parity).
        arrow_fields.append(f'arrow::field({_cpp_string(col_name)}, {arrow_type})')
        arrays.append(f"a{idx}")

    fields_block = "\n".join(fields) if fields else "    // (no columns)"
    builders_block = "\n".join(builders)
    schema_block = ",\n        ".join(arrow_fields)
    arrays_block = ", ".join(arrays)
    return f"""\
// {qn}
struct {qn}Out {{
{fields_block}
}};

inline arrow::Result<std::shared_ptr<arrow::Table>> to_arrow_{qn.lower()}(const {qn}Out& out) {{
{builders_block}
    auto schema = arrow::schema({{
        {schema_block}
    }});
    return arrow::Table::Make(schema, {{{arrays_block}}});
}}
"""


def _cpp_string(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_HEADER = '''\
#pragma once
// AUTO-GENERATED by assemble_output_struct.py — typed per-query output structs.
// Each run_q<id> populates its Q<id>Out (struct of arrays); to_arrow_q<id> turns it
// into a typed arrow::Table for zero-copy shm egress (WriteArrowTableToShm).

#include <memory>
#include <string>
#include <vector>

#include <arrow/api.h>
#include <arrow/result.h>

'''


def assemble_output_struct_file(query_outputs: Dict[str, Sequence[Tuple[str, str]]]) -> str:
    """Assemble the full ``query_out.hpp`` for all queries.

    ``query_outputs`` maps ``query_id -> [(column_name, duckdb_type), ...]``.
    """
    blocks = "\n".join(gen_output_block(qid, cols) for qid, cols in query_outputs.items())
    return _HEADER + blocks
