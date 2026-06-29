"""Validate the delegation-based column-egress library (cpp_helpers/column_egress.hpp).

The library encodes NO Arrow types by hand: it builds one canonical array per value family
(with NULL support) and casts to the column's exact type via arrow::compute::Cast - the
symmetric, equally-total counterpart of column_ingest.hpp. These tests prove, end to end
through the real WriteArrowTableToShm egress path, that:
  * every output type round-trips with its EXACT Arrow type - narrowed integers (int32/int16),
    float32, BOOLEAN, decimal128 AND decimal256, DATE, TIMESTAMP, VARCHAR;
  * NULLs are emitted as real Arrow nulls, never substituted with 0 / "" / the epoch;
  * DuckDB reads the egress output with matching values and null semantics;
  * a value that does not fit the requested target fails LOUDLY, never truncates silently.

Skips without a C++ toolchain or the Arrow dev headers.
"""
from __future__ import annotations

import datetime
import decimal
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER = REPO / "tests" / "cpp" / "column_egress_test.cpp"

D = decimal.Decimal


def _toolchain_ok() -> bool:
    if not (shutil.which("g++") or shutil.which("clang++")):
        return False
    return shutil.which("pkg-config") is not None and (
        subprocess.run(["pkg-config", "--exists", "arrow"]).returncode == 0
    )


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    if not _toolchain_ok():
        pytest.skip("no C++ toolchain / Arrow dev headers")
    cc = "g++" if shutil.which("g++") else "clang++"
    cflags = subprocess.run(["pkg-config", "--cflags", "arrow"], capture_output=True, text=True).stdout.split()
    libs = subprocess.run(["pkg-config", "--libs", "arrow"], capture_output=True, text=True).stdout.split()
    out = tmp_path_factory.mktemp("ce") / "column_egress_test"
    cmd = [cc, "-std=c++20", "-O2", "-I", str(CPP_HELPERS), *cflags, str(DRIVER), *libs, "-o", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"compile failed:\n{proc.stderr}")
    return out


@pytest.fixture(scope="module")
def table(driver, tmp_path_factory):
    pa = pytest.importorskip("pyarrow")
    out = tmp_path_factory.mktemp("ce_data") / "out.arrow"
    proc = subprocess.run([str(driver), "build", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    with pa.memory_map(str(out), "r") as source:
        return pa.ipc.open_file(source).read_all()


def test_exact_arrow_types(table):
    """Each column carries the EXACT Arrow type DuckDB would, not a widened canonical type."""
    pa = pytest.importorskip("pyarrow")
    schema = {f.name: f.type for f in table.schema}
    assert schema["bigint"] == pa.int64()
    assert schema["integer"] == pa.int32()        # narrowed from canonical int64 via Cast
    assert schema["smallint"] == pa.int16()
    assert schema["tinyint"] == pa.int8()
    assert schema["ubigint"] == pa.uint64()
    assert schema["dbl"] == pa.float64()
    assert schema["real"] == pa.float32()          # narrowed from canonical float64 via Cast
    assert schema["flag"] == pa.bool_()
    assert schema["name"] == pa.string()
    assert schema["dec"] == pa.decimal128(38, 2)
    assert schema["hugeint"] == pa.decimal128(38, 0)
    assert schema["wide"] == pa.decimal256(50, 2)  # precision > 38 -> decimal256, from int128
    assert schema["d"] == pa.date32()
    assert schema["ts"] == pa.timestamp("us")
    assert schema["nul_dec"] == pa.decimal128(38, 2)
    assert schema["nul_ts"] == pa.timestamp("us")


def test_exact_values(table):
    col = {name: table.column(name).to_pylist() for name in table.column_names}
    assert col["bigint"] == [1, 2, -3, 4]
    assert col["integer"] == [10, 20, 30, 40]
    assert col["smallint"] == [1, 2, 3, 4]
    assert col["tinyint"] == [1, 2, -3, 4]
    assert col["ubigint"] == [0, 9223372036854775808, 18446744073709551615, 4]
    assert col["dbl"] == [1.5, 2.5, 3.0, 4.0]
    assert col["real"] == [1.5, 2.5, 3.0, 4.0]
    assert col["flag"] == [True, False, True, True]
    assert col["name"] == ["a", "b", "c", "d"]
    # Exact decimals, including a negative (exercises sign handling in decimal128 AND decimal256).
    assert col["dec"] == [D("1.50"), D("-2.25"), D("0.00"), D("10.10")]
    assert col["hugeint"] == [
        D("1267650600228229401496703205376"),
        D("-1267650600228229401496703205376"),
        D("0"),
        D("42"),
    ]
    assert col["wide"] == [D("1.50"), D("-2.25"), D("0.00"), D("10.10")]
    assert col["d"] == [datetime.date(2023, 1, 2), datetime.date(2023, 1, 3),
                        datetime.date(2023, 1, 4), datetime.date(2023, 1, 5)]


def test_timestamp_round_trips_exactly(table):
    pa = pytest.importorskip("pyarrow")
    # Compare the underlying int64 micros directly - no timezone arithmetic in the assertion.
    assert table.column("ts").cast(pa.int64()).to_pylist() == [1000000, 2000000, 3000000, 4000000]


def test_nulls_are_real_nulls(table):
    """A NULL result is a real Arrow null, not 0 / "" / the epoch."""
    pa = pytest.importorskip("pyarrow")
    assert table.column("nul_str").to_pylist() == ["x", None, "z", None]
    assert table.column("nul_int").to_pylist() == [5, None, 7, None]
    assert table.column("nul_dec").to_pylist() == [D("1.00"), None, D("3.00"), None]
    assert table.column("nul_ts").cast(pa.int64()).to_pylist() == [1000000, None, 3000000, None]
    assert table.column("nul_int").null_count == 2


def test_duckdb_reads_egress_output(table):
    """DuckDB ingests the egress output with matching values and null semantics."""
    duckdb = pytest.importorskip("duckdb")
    # decimal256(50,2) exceeds DuckDB's max DECIMAL precision (38); drop it for the DuckDB read.
    sub = table.select([c for c in table.column_names if c != "wide"])
    con = duckdb.connect()
    con.register("t", sub)
    got = con.execute(
        "select sum(bigint), sum(dec), count(nul_int), sum(nul_int), sum(flag::int) from t"
    ).fetchone()
    assert got[0] == 4                 # 1 + 2 - 3 + 4
    assert got[1] == D("9.35")         # 1.50 - 2.25 + 0.00 + 10.10, exact DECIMAL(38,2)
    assert got[2] == 2                 # NULLs excluded from COUNT
    assert got[3] == 12                # 5 + 7, NULLs excluded from SUM
    assert got[4] == 3                 # three TRUEs


def test_overflow_is_loud_not_silent(driver, tmp_path):
    """A BIGINT value that does not fit the requested INTEGER target must THROW, not truncate."""
    out = tmp_path / "ovf.arrow"
    proc = subprocess.run([str(driver), "overflow", str(out)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "cannot cast" in proc.stderr  # loud, explicit failure from cast_to


def test_decimal_precision_overflow_is_loud(driver, tmp_path):
    """A value that overflows the column's DECIMAL precision must THROW (the one builder that does
    not go through Cast's range check); it must not emit an out-of-range decimal silently."""
    out = tmp_path / "decovf.arrow"
    proc = subprocess.run([str(driver), "decimal_overflow", str(out)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "does not fit DECIMAL" in proc.stderr


def test_make_table_length_mismatch_is_loud(driver, tmp_path):
    """Columns of different lengths must be rejected at the source, naming the offending column,
    not silently assembled into a structurally invalid Arrow table."""
    out = tmp_path / "mismatch.arrow"
    proc = subprocess.run([str(driver), "length_mismatch", str(out)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "same number of rows" in proc.stderr and "'b'" in proc.stderr
