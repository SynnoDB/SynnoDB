from synnodb.observability.benchmark.systems.bespoke import BespokeRunner
from synnodb.observability.benchmark.systems.clickhouse import ClickHouseRunner
from synnodb.observability.benchmark.systems.duckdb import DuckDBRunner
from synnodb.observability.benchmark.systems.umbra import UmbraRunner

# Lower-case key -> runner class.
SYSTEM_REGISTRY: dict[str, type] = {
    "bespoke": BespokeRunner,
    "clickhouse": ClickHouseRunner,
    "duckdb": DuckDBRunner,
    "umbra": UmbraRunner,
}
