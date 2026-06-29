"""The generation validator now holds an engine to bit-exact DECIMAL equality.

Two pieces make that work and are covered here: the DuckDB reference is fetched via Arrow (so
DECIMAL columns stay exact ``Decimal`` objects rather than being coerced to float by
``fetchdf()``), and the result comparison is exact for those object columns while staying
tolerant for genuine floats. Together a decimal that went through ``double`` (e.g.
``56586554400.730003``) is caught instead of masked by a tolerance.
"""
from __future__ import annotations

from decimal import Decimal

import duckdb
import pandas as pd
import pyarrow as pa
import pytest


def test_duckdb_reference_via_arrow_preserves_decimals():
    # This is exactly how duckdb_connection_manager.duckdb_sql now fetches the reference.
    df = duckdb.connect().execute(
        "select cast(56586554400.73 as decimal(38,2)) as r, 1.5::double as f"
    ).to_arrow_table().to_pandas()
    assert isinstance(df["r"][0], Decimal)
    assert df["r"][0] == Decimal("56586554400.73")
    # A fetchdf() reference would be float64 here and could never be compared exactly.
    assert duckdb.connect().execute(
        "select cast(56586554400.73 as decimal(38,2)) as r"
    ).fetchdf()["r"].dtype == "float64"


def _compare(reference: pd.DataFrame, bespoke: pd.DataFrame) -> None:
    # Mirrors run_and_check_queries.check_output_correctness.
    pd.testing.assert_frame_equal(
        reference, bespoke, check_dtype=False, check_column_type=False,
        check_index_type=False, check_exact=False, atol=1e-5, rtol=1e-5,
    )


def test_decimals_exact_doubles_tolerant():
    reference = pd.DataFrame({"sum_base_price": [Decimal("56586554400.73")], "avg_price": [35785.70930693735]})
    # An exact engine: decimal identical, AVG within float tolerance -> passes.
    exact = pd.DataFrame({"sum_base_price": [Decimal("56586554400.73")], "avg_price": [35785.709306937344]})
    _compare(reference, exact)
    # A decimal printed through double (off in the last places) -> caught, not masked.
    inexact = pd.DataFrame({"sum_base_price": [Decimal("56586554400.64")], "avg_price": [35785.70930693735]})
    with pytest.raises(AssertionError):
        _compare(reference, inexact)


def test_engine_arrow_result_reads_back_as_exact_decimal(tmp_path):
    # The validator reads the engine's result the same way (Arrow IPC -> pandas).
    table = pa.table({"sum_base_price": pa.array([Decimal("56586554400.73")], pa.decimal128(38, 2))})
    path = tmp_path / "result_x.arrow"
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
    bespoke_df = pa.ipc.open_file(pa.memory_map(str(path), "r")).read_all().to_pandas()
    assert bespoke_df["sum_base_price"][0] == Decimal("56586554400.73")
