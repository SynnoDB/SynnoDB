"""ProcessEngine adapter: unit tests for the CSV->Arrow + casting + health logic.

The full HotpatchProc execution path is integration-tested against a real generated
engine separately (it requires a compiled engine + data).
"""
from __future__ import annotations

import pyarrow as pa

from synnodb.router.process_engine import ProcessEngine, _arrow_type_for


def test_arrow_type_mapping():
    assert _arrow_type_for("BIGINT") == pa.int64()
    assert _arrow_type_for("DECIMAL(15,2)") == pa.float64()
    assert _arrow_type_for("DOUBLE") == pa.float64()
    assert _arrow_type_for("VARCHAR") == pa.string()


def test_health_false_without_binary(tmp_path):
    eng = ProcessEngine("e", tmp_path, "/data/sf20")
    assert eng.health() is False
    (tmp_path / "db").write_text("")  # pretend a compiled binary exists
    assert eng.health() is True


def test_read_csv_to_arrow(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "result_x.csv").write_text(
        'l_returnflag,sum_qty,avg_price\n"A",37,1.5\n"N",99,2.25\n'
    )
    eng = ProcessEngine("e", tmp_path, "/data/sf20")
    table = eng._read_csv(results / "result_x.csv")
    assert table.column("l_returnflag").to_pylist() == ["A", "N"]
    assert table.column("sum_qty").to_pylist() == [37, 99]
    assert table.column("avg_price").to_pylist() == [1.5, 2.25]


def test_read_csv_with_schema_cast(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    # sum_qty arrives as int from pandas; cast to the declared types.
    (results / "result_y.csv").write_text("flag,sum_qty,avg_price\nA,37,1.5\n")
    eng = ProcessEngine(
        "e", tmp_path, "/data/sf20",
        output_schema=[("flag", "VARCHAR"), ("sum_qty", "DECIMAL(15,2)"), ("avg_price", "DOUBLE")],
    )
    table = eng._read_csv(results / "result_y.csv")
    assert table.schema.field("sum_qty").type == pa.float64()  # cast from int to decimal->float64
    assert table.schema.field("flag").type == pa.string()
    assert table.column("sum_qty").to_pylist() == [37.0]
