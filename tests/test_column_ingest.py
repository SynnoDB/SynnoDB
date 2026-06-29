"""Validate the delegation-based column-ingestion library (cpp_helpers/column_ingest.hpp).

The library decodes NO Arrow types itself — it casts any column to one canonical type
via arrow::compute::Cast (which handles 100% of source types) and reads that single
type. These tests prove that on:
  * synthetic data with the exact types a hand-written switch would have missed
    (BOOLEAN, DICTIONARY-encoded string, TIMESTAMP, decimal), and
  * real TPC-H parquet vs DuckDB (decimal/int/string/date end to end).

Durable fix for failure G2. Skips without a C++ toolchain or the TPC-H parquet.
"""
from __future__ import annotations

import datetime
import decimal
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
DRIVER = REPO / "tests" / "cpp" / "column_ingest_test.cpp"
LINEITEM = Path("/mnt/labstore/learneddb/synno_data/workloads/tpch/tpch_parquet/sf1/lineitem.parquet")


def _toolchain_ok() -> bool:
    if not (shutil.which("g++") or shutil.which("clang++")):
        return False
    return shutil.which("pkg-config") is not None and (
        subprocess.run(["pkg-config", "--exists", "arrow", "parquet"]).returncode == 0
    )


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    if not _toolchain_ok():
        pytest.skip("no C++ toolchain / Arrow+Parquet dev headers")
    cc = "g++" if shutil.which("g++") else "clang++"
    cflags = subprocess.run(["pkg-config", "--cflags", "arrow", "parquet"], capture_output=True, text=True).stdout.split()
    libs = subprocess.run(["pkg-config", "--libs", "arrow", "parquet"], capture_output=True, text=True).stdout.split()
    out = tmp_path_factory.mktemp("ci") / "column_ingest_test"
    cmd = [cc, "-std=c++20", "-O2", "-I", str(CPP_HELPERS), *cflags, str(DRIVER), *libs, "-o", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"compile failed:\n{proc.stderr}")
    return out


def _parse(stdout: str) -> dict:
    return {k: (int(v) if v.lstrip("-").isdigit() else float(v)) for k, v in re.findall(r"(\w+)=(-?[\d.]+)", stdout)}


def test_nullable_validity_roundtrip(driver):
    """Full nullable support: a NULL captured on ingest (via the Validity out-param) is re-emitted
    as a real Arrow NULL on egress - not silently turned into 0 - while the default path (no
    out-param) keeps the historical dense null->0 behaviour. This is the mechanism the generated
    engine uses to honour SQL null semantics (COUNT/AVG/IS NULL/propagation)."""
    proc = subprocess.run([str(driver), "nullable"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = _parse(proc.stdout)
    assert out["validity"] == 101         # [valid, NULL, valid] -> the NULL is recorded
    assert out["dense1"] == 0             # default path still reads the NULL as 0 (back-compat)
    assert out["egress_nulls"] == 1       # exactly one real Arrow NULL emitted
    assert out["egress_isnull1"] == 1     # at the NULL row
    assert out["egress_isnull0"] == 0     # and only there


def test_diverse_types_beyond_tpch(driver, tmp_path):
    """BOOLEAN, DICTIONARY-string, TIMESTAMP, decimal — the long tail, via delegation."""
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    days = (datetime.date(2023, 1, 2) - datetime.date(1970, 1, 1)).days  # 19359
    table = pa.table({
        "dec_col": pa.array([decimal.Decimal("1.50"), decimal.Decimal("2.25"), decimal.Decimal("0.00"), decimal.Decimal("10.10")], pa.decimal128(15, 2)),
        "int_col": pa.array([10, 20, 30, 40], pa.int32()),
        "bool_col": pa.array([True, False, True, True]),
        "dict_col": pa.array(["A", "B", "A", "C"]).dictionary_encode(),
        "ts_col": pa.array([datetime.datetime(2023, 1, 2, 5), datetime.datetime(2023, 1, 2, 23),
                            datetime.datetime(2024, 6, 1), datetime.datetime(2020, 12, 31)], pa.timestamp("us")),
        "date_col": pa.array([datetime.date(2023, 1, 2)] * 1 + [datetime.date(2023, 1, 2), datetime.date(2024, 6, 1), datetime.date(2020, 12, 31)], pa.date32()),
        "dbl_col": pa.array([1.5, 2.5, 3.0, 4.0], pa.float64()),
    })
    path = tmp_path / "synth.parquet"
    pq.write_table(table, path)

    out = subprocess.run([str(driver), "synth", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = _parse(out.stdout)
    assert got["dec"] == 1385          # decimal -> scaled int64 (150+225+0+1010)
    assert got["dec16"] == 1385        # same decimal -> narrow exact int16 storage
    assert got["int"] == 100           # int32 -> narrow int16 storage
    assert got["bool"] == 3            # BOOLEAN -> narrow uint8 storage (3 true)
    assert got["dictA"] == 2           # DICTIONARY<string> densified, "A" appears twice
    assert got["ts0"] == days          # TIMESTAMP -> days since epoch (truncated)
    assert got["date0"] == days        # DATE32 -> days
    assert got["dbl"] == 11.0          # double sum


def test_overflow_is_loud_not_silent(driver, tmp_path):
    """A decimal value too large for int64 at the requested scale must THROW, not truncate."""
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    huge = decimal.Decimal("123456789012345678.99")  # ~1.2e17 * 100 fits? 1.23e19 > int64 max -> overflow
    table = pa.table({
        "dec_col": pa.array([huge], pa.decimal128(38, 2)),
        "int_col": pa.array([1], pa.int64()), "bool_col": pa.array([True]),
        "dict_col": pa.array(["A"]).dictionary_encode(), "ts_col": pa.array([datetime.datetime(2023, 1, 1)], pa.timestamp("us")),
        "date_col": pa.array([datetime.date(2023, 1, 1)], pa.date32()), "dbl_col": pa.array([1.0]),
    })
    path = tmp_path / "ovf.parquet"
    pq.write_table(table, path)
    out = subprocess.run([str(driver), "synth", str(path)], capture_output=True, text=True)
    assert out.returncode != 0
    assert "does not fit" in out.stderr  # loud, explicit failure


def test_lineitem_matches_duckdb(driver):
    if not LINEITEM.exists():
        pytest.skip(f"TPC-H SF1 parquet not present at {LINEITEM}")
    import duckdb

    out = subprocess.run([str(driver), "lineitem", str(LINEITEM)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = _parse(out.stdout)
    row = duckdb.connect().execute(
        f"""select (sum(l_quantity)*100)::HUGEINT, (sum(l_extendedprice)*100)::HUGEINT,
                   sum(l_orderkey)::HUGEINT, count(*) filter (where l_returnflag='A'),
                   min(l_shipdate - DATE '1970-01-01'), max(l_shipdate - DATE '1970-01-01')
            from read_parquet('{LINEITEM}')"""
    ).fetchone()
    assert got["sum_qty"] == int(row[0])
    assert got["sum_ep"] == int(row[1])
    assert got["sum_okey"] == int(row[2])
    assert got["rf_A"] == int(row[3])
    assert got["sd_min"] == int(row[4]) and got["sd_max"] == int(row[5])
