"""Typed output-struct codegen: pure-Python generation checks plus a real C++
compile+run that proves the generated SoA struct converts to a typed Arrow table and
round-trips through shm with types locked to DuckDB.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pyarrow as pa
import pytest

from synnodb.cpp_runner.prepare_repo.assemble_output_struct import (
    _sanitize,
    assemble_output_struct_file,
    gen_output_block,
    map_type,
)
from synnodb.router.shm_transport import read_table

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER_SRC = REPO / "tests" / "cpp" / "output_struct_test.cpp"

# Must match tests/cpp/output_struct_test.cpp.
COLUMNS = [
    ("l_returnflag", "VARCHAR"),
    ("sum_qty", "BIGINT"),
    ("avg_price", "DOUBLE"),
    ("count_order", "BIGINT"),
]


# --------------------------------------------------------------------------- #
# Pure-Python codegen (no compiler needed)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "duck,cpp,arrow",
    [
        ("BIGINT", "int64_t", "arrow::int64()"),
        ("INTEGER", "int32_t", "arrow::int32()"),
        ("DOUBLE", "double", "arrow::float64()"),
        ("DECIMAL(15,2)", "std::string", "arrow::utf8()"),
        ("VARCHAR", "std::string", "arrow::utf8()"),
        ("BOOLEAN", "bool", "arrow::boolean()"),
        ("SOMETHING_WEIRD", "std::string", "arrow::utf8()"),  # fallback
    ],
)
def test_map_type(duck, cpp, arrow):
    cpp_elem, arrow_expr, _builder = map_type(duck)
    assert cpp_elem == cpp and arrow_expr == arrow


def test_sanitize_identifiers():
    used: set = set()
    assert _sanitize("count(*)", used) == "count___"
    assert _sanitize("l_returnflag", used) == "l_returnflag"
    assert _sanitize("2nd", used).startswith("c_")
    a = _sanitize("dup", used)
    b = _sanitize("dup", used)
    assert a != b  # de-duplicated
    assert _sanitize("int", used) == "int_"  # reserved word avoided


def test_gen_output_block_structure():
    block = gen_output_block("7", COLUMNS)
    assert "struct Q7Out {" in block
    assert "std::vector<std::string> l_returnflag;" in block
    assert "std::vector<int64_t> sum_qty;" in block
    assert "std::vector<double> avg_price;" in block
    assert "to_arrow_q7" in block
    # Arrow schema keeps DuckDB's original column names.
    assert 'arrow::field("count_order", arrow::int64())' in block


def test_assemble_file_has_header_and_all_queries():
    src = assemble_output_struct_file({"1": COLUMNS, "2": [("x", "INTEGER")]})
    assert src.startswith("#pragma once")
    assert "#include <arrow/api.h>" in src
    assert "struct Q1Out" in src and "struct Q2Out" in src


# --------------------------------------------------------------------------- #
# Real C++: generated struct -> typed Arrow -> shm -> Python (types locked)
# --------------------------------------------------------------------------- #
def _have_toolchain() -> bool:
    if not (shutil.which("g++") or shutil.which("clang++")):
        return False
    return shutil.which("pkg-config") is not None and (
        subprocess.run(["pkg-config", "--exists", "arrow"]).returncode == 0
    )


def test_generated_struct_compiles_and_egresses_typed_arrow(tmp_path):
    if not _have_toolchain():
        pytest.skip("no C++ toolchain / Arrow dev headers")
    # 1. generate query_out.hpp for the fixed columns the driver expects.
    (tmp_path / "query_out.hpp").write_text(assemble_output_struct_file({"1": COLUMNS}))

    # 2. compile the driver against the generated header + shm writer.
    cc = "g++" if shutil.which("g++") else "clang++"
    cflags = subprocess.run(["pkg-config", "--cflags", "arrow"], capture_output=True, text=True).stdout.split()
    libs = subprocess.run(["pkg-config", "--libs", "arrow"], capture_output=True, text=True).stdout.split()
    binary = tmp_path / "driver"
    cmd = [cc, "-std=c++17", "-O2", "-I", str(tmp_path), "-I", str(CPP_HELPERS), *cflags, str(DRIVER_SRC), *libs, "-o", str(binary)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"compile failed:\n{proc.stderr}"

    # 3. run it (writes typed Arrow to shm) and read back in Python.
    result_path = tmp_path / "result.arrow"
    run = subprocess.run([str(binary), str(result_path)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    table = read_table(result_path)

    # types are locked to the DuckDB-derived schema, values exact.
    assert [(f.name, str(f.type)) for f in table.schema] == [
        ("l_returnflag", "string"),
        ("sum_qty", "int64"),
        ("avg_price", "double"),
        ("count_order", "int64"),
    ]
    assert table.to_pylist() == [
        {"l_returnflag": "A", "sum_qty": 37, "avg_price": 1.5, "count_order": 3},
        {"l_returnflag": "N", "sum_qty": 99, "avg_price": 2.25, "count_order": 5},
        {"l_returnflag": "R", "sum_qty": 12, "avg_price": 3.0, "count_order": 2},
    ]
