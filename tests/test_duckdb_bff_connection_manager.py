from pathlib import Path
import sys
import types


sys.modules.setdefault(
    "duckdb",
    types.SimpleNamespace(
        connect=lambda database=":memory:": None,
        DuckDBPyConnection=object,
    ),
)

from observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from utils.utils import DBStorage
from workloads.workload_provider_bff import BFFWorkload


class RecordingDuckDBConnection(DuckDBConnectionManager):
    def __init__(self, **kwargs):
        self.statements = []
        super().__init__(**kwargs)

    def _connect(self, database=None):
        class FakeConnection:
            def __init__(self, outer):
                self.outer = outer

            def execute(self, sql):
                self.outer.statements.append(sql)
                return self

            def close(self):
                return None

        self.con = FakeConnection(self)
        return self.con


def test_bff_mode_registers_views_over_read_bff_table(tmp_path: Path):
    manager = RecordingDuckDBConnection(
        pre_load_duckdb_tables=True,
        dataset_tables=["lineitem", "orders"],
        parquet_path=tmp_path / "parquet",
        benchmark=BFFWorkload.TPCH_ST,
        db_storage=DBStorage.IN_MEMORY,
        run_duckdb_on_parquet=False,
        run_duckdb_on_bff=True,
        bff_store_path=tmp_path / "bff_store",
        bff_extension_path=tmp_path / "duckdb-bff" / "build/debug/extension/bff/bff.duckdb_extension",
    )

    assert manager.statements[0].startswith("PRAGMA threads=")
    assert (
        "LOAD '"
        + (tmp_path / "duckdb-bff" / "build/debug/extension/bff/bff.duckdb_extension").as_posix()
        + "'"
    ) in manager.statements
    assert (
        "CREATE VIEW lineitem AS SELECT * FROM read_bff_table('"
        + (tmp_path / "bff_store").as_posix()
        + "', 'lineitem')"
    ) in manager.statements
    assert (
        "CREATE VIEW orders AS SELECT * FROM read_bff_table('"
        + (tmp_path / "bff_store").as_posix()
        + "', 'orders')"
    ) in manager.statements
