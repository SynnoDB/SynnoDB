"""ProcessEngine adapter: unit tests for reading the engine's exact Arrow result + health.

The full HotpatchProc execution path is integration-tested against a real generated
engine separately (it requires a compiled engine + data).
"""
from __future__ import annotations

from decimal import Decimal

import pyarrow as pa

from synnodb.router.process_engine import ProcessEngine


def test_health_false_without_binary(tmp_path):
    eng = ProcessEngine("e", tmp_path, "/data/sf20")
    assert eng.health() is False
    (tmp_path / "db").write_text("")  # pretend a compiled binary exists
    assert eng.health() is True


def test_read_arrow_result_is_exact(tmp_path):
    # The engine writes its result as Arrow built from exact int128 (column_egress); the runtime
    # reads it back with the exact decimal128 - no CSV/double round-trip.
    results = tmp_path / "results"
    results.mkdir()
    table = pa.table({
        "sum_base_price": pa.array([Decimal("56586554400.73")], pa.decimal128(38, 2)),
        "count_order": pa.array([1478493], pa.int64()),
    })
    path = results / "result_x.arrow"
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
    eng = ProcessEngine("e", tmp_path, "/data/sf20")
    got = eng._read_arrow(path)
    assert got.schema.field("sum_base_price").type == pa.decimal128(38, 2)
    assert got.column("sum_base_price").to_pylist() == [Decimal("56586554400.73")]
    assert got.column("count_order").to_pylist() == [1478493]
