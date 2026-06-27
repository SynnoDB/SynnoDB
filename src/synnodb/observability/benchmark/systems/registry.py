from observability.benchmark.systems.bespoke import BespokeRunner
from observability.benchmark.systems.clickhouse import ClickHouseRunner
from observability.benchmark.systems.duckdb import DuckDBRunner
from observability.benchmark.systems.umbra import UmbraRunner

# Lower-case key -> runner class.
SYSTEM_REGISTRY: dict[str, type] = {
    "bespoke": BespokeRunner,
    "clickhouse": ClickHouseRunner,
    "duckdb": DuckDBRunner,
    "umbra": UmbraRunner,
}
