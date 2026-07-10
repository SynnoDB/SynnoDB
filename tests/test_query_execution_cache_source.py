"""Regression tests for the data-source dimension of the query execution cache key.

DuckDB is the correctness oracle. Its reference answer depends on how the data is represented -
materialized flat tables vs parquet views - so ``OLAPExecSettings.data_source`` must participate
in the cache key. Because it is an exec-settings field, the generic key (asdict(exec_settings))
picks it up for every system, with no DuckDB-specific branch in the cache.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("SYNNO_DATA_DIR", "/tmp")

from synnodb.utils.utils import DataSource, DBStorage
from synnodb.workloads.query_execution_cache import QueryExecutionCache
from synnodb.workloads.system_factory import System
from synnodb.workloads.system_factory_olap import OLAPSystemFactory
from synnodb.workloads.workload_provider import (
    GeneralSystemConfig,
    QueryEntry,
    Workload,
)
from synnodb.workloads.workload_provider_olap import (
    OLAPExecSettings,
    validate_storage_combo,
)


class DummyWorkload(Workload):
    TEST = "test"


def _fixtures(data_source: DataSource = DataSource.FLAT, db_storage=DBStorage.SSD):
    entry = QueryEntry(
        benchmark=DummyWorkload.TEST,
        query_id="1",
        sql="select * from lineitem where l_orderkey = 1",
        query_args='1 req-1 "1"',
        placeholders={"orderkey": "1"},
        order_by_info=[],
    )
    exec_settings = OLAPExecSettings(
        scale_factor=1.0,
        db_storage=db_storage,
        parquet_dir=Path("/data/sf1"),
        disk_db_dir=None,
        data_source=data_source,
    )
    general_system_config = GeneralSystemConfig(
        memory_limit_mb=None, num_threads=1, core_ids=None
    )
    return entry, exec_settings, general_system_config


def _hash_for(
    system, exec_settings, entry, general_system_config, tmp_path: Path
) -> str:
    cache = QueryExecutionCache(
        query_execution_cache_dir=tmp_path,
        system_factory=OLAPSystemFactory(),
    )
    _, cache_hash = cache._get_cache_filepath(
        system,
        DummyWorkload.TEST,
        entry,
        exec_settings,
        general_system_config=general_system_config,
    )
    return cache_hash


def test_cache_key_distinguishes_data_sources(tmp_path: Path) -> None:
    # Same query and storage medium, different data source -> different cache entry, so a
    # flat/materialized reference answer is never replayed for a parquet-view run (and vice versa).
    entry, flat_settings, gsc = _fixtures(data_source=DataSource.FLAT)
    parquet_settings = replace(flat_settings, data_source=DataSource.PARQUET)

    flat_hash = _hash_for(System.DUCKDB, flat_settings, entry, gsc, tmp_path)
    parquet_hash = _hash_for(System.DUCKDB, parquet_settings, entry, gsc, tmp_path)
    assert flat_hash != parquet_hash


def test_data_source_participates_for_every_system(tmp_path: Path) -> None:
    # The discriminator lives in exec_settings, so it is keyed generically - not just for DuckDB.
    entry, flat_settings, gsc = _fixtures(data_source=DataSource.FLAT)
    bespoke_settings = replace(flat_settings, data_source=DataSource.BESPOKE)

    flat_hash = _hash_for(System.UMBRA, flat_settings, entry, gsc, tmp_path)
    bespoke_hash = _hash_for(System.UMBRA, bespoke_settings, entry, gsc, tmp_path)
    assert flat_hash != bespoke_hash


def test_bespoke_settings_reject_in_memory_parquet() -> None:
    # OLAPExecSettings drive a bespoke-engine run: in-memory can be flat or bespoke, but parquet
    # views need disk, so in-memory parquet is rejected.
    with pytest.raises(ValueError):
        _fixtures(data_source=DataSource.PARQUET, db_storage=DBStorage.IN_MEMORY)


def test_bespoke_settings_allow_in_memory_flat_and_bespoke() -> None:
    # Both legal in-memory bespoke sources construct without error.
    _fixtures(data_source=DataSource.FLAT, db_storage=DBStorage.IN_MEMORY)
    _fixtures(data_source=DataSource.BESPOKE, db_storage=DBStorage.IN_MEMORY)


def test_duckdb_cache_key_ignores_inert_config_fields(tmp_path: Path) -> None:
    # DuckDB applies only PRAGMA threads; core_ids and memory_limit_mb never reach the
    # reference connection, so they must not fragment its cache key. A baseline measured
    # on one machine (or one memory setting) is therefore reusable on another.
    entry, settings, gsc = _fixtures()
    other = replace(gsc, core_ids=[0, 1, 2, 3], memory_limit_mb=4096)

    base_hash = _hash_for(System.DUCKDB, settings, entry, gsc, tmp_path)
    other_hash = _hash_for(System.DUCKDB, settings, entry, other, tmp_path)
    assert base_hash == other_hash


def test_duckdb_cache_key_depends_on_num_threads(tmp_path: Path) -> None:
    # num_threads maps to PRAGMA threads and genuinely changes the measured runtime, so it
    # stays part of the DuckDB key - a serial baseline is never replayed for a parallel run.
    entry, settings, gsc = _fixtures()
    parallel = replace(gsc, num_threads=8)

    serial_hash = _hash_for(System.DUCKDB, settings, entry, gsc, tmp_path)
    parallel_hash = _hash_for(System.DUCKDB, settings, entry, parallel, tmp_path)
    assert serial_hash != parallel_hash


def test_umbra_cache_key_depends_on_core_ids(tmp_path: Path) -> None:
    # Umbra pins its container starting at core_ids[0], so core_ids does influence its
    # runtime and must remain in the Umbra key (unlike DuckDB).
    entry, settings, gsc = _fixtures()
    pinned = replace(gsc, core_ids=[8, 9, 10, 11])

    base_hash = _hash_for(System.UMBRA, settings, entry, gsc, tmp_path)
    pinned_hash = _hash_for(System.UMBRA, settings, entry, pinned, tmp_path)
    assert base_hash != pinned_hash


def test_umbra_cache_key_ignores_memory_limit(tmp_path: Path) -> None:
    # memory_limit_mb is not enforced on the Umbra reference, so it must not fragment its key.
    entry, settings, gsc = _fixtures()
    limited = replace(gsc, memory_limit_mb=2048)

    base_hash = _hash_for(System.UMBRA, settings, entry, gsc, tmp_path)
    limited_hash = _hash_for(System.UMBRA, settings, entry, limited, tmp_path)
    assert base_hash == limited_hash


def test_validate_storage_combo_is_system_specific() -> None:
    # DuckDB in-memory can only be flat; the bespoke engine in-memory can also be bespoke.
    validate_storage_combo(System.DUCKDB, DBStorage.IN_MEMORY, DataSource.FLAT)
    validate_storage_combo(System.BESPOKE, DBStorage.IN_MEMORY, DataSource.BESPOKE)
    with pytest.raises(ValueError):
        validate_storage_combo(System.DUCKDB, DBStorage.IN_MEMORY, DataSource.BESPOKE)
    # Parquet needs persistent storage for either system.
    validate_storage_combo(System.DUCKDB, DBStorage.SSD, DataSource.PARQUET)
    validate_storage_combo(System.BESPOKE, DBStorage.SSD, DataSource.PARQUET)
    with pytest.raises(ValueError):
        validate_storage_combo(System.DUCKDB, DBStorage.IN_MEMORY, DataSource.PARQUET)
