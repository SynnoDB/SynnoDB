import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from observability.benchmark.systems.umbra import UmbraRunner
from utils import utils
from workloads.system_factory import SystemFactory
from workloads.workload_provider import (
    ExecSettings,
    GeneralSystemConfig,
    QueryBatch,
    QueryEntry,
    Workload,
)
from workloads.workload_provider_olap import OLAPExecSettings

logger = logging.getLogger(__name__)


@dataclass
class QueryExecutionResult:
    system: str
    query_entry: QueryEntry
    exec_settings: ExecSettings  # e.g. scale-factor, storage medium, ...
    general_system_config: (
        GeneralSystemConfig  # e.g. memory limit, num threads, core ids, ...
    )
    result: pd.DataFrame | None
    exec_time_ms: float
    plan: dict | None


class QueryExecutionCache:
    in_mem_cache: dict[str, QueryExecutionResult]

    def __init__(self, query_execution_cache_dir: Path, system_factory: SystemFactory):
        self.query_execution_cache_dir = query_execution_cache_dir
        utils.create_dir_and_set_permissions(self.query_execution_cache_dir)

        self.system_factory = system_factory

    def lookup_or_execute_query_batch(
        self,
        batch: QueryBatch,
        system: str,
        do_not_cache: bool = False,
    ) -> list[QueryExecutionResult]:
        # lookup which entries are missing
        missing_entries = []

        for query_entry in batch.query_list:
            # first lookup might load from disk and populate in-mem cache, subsequent lookups will be faster
            exec_result = self._lookup_entry(
                system,
                benchmark=batch.benchmark,
                query_entry=query_entry,
                exec_settings=batch.exec_settings,
                general_system_config=batch.general_system_config,
            )
            if exec_result is None:
                missing_entries.append(query_entry)

        # execute missing
        self._exec_missing_queries(
            system,
            benchmark=batch.benchmark,
            missing_entries=missing_entries,
            exec_settings=batch.exec_settings,
            general_system_config=batch.general_system_config,
            do_not_cache=do_not_cache,
        )

        # after execution, all entries should be in cache, so we can lookup again to get the results
        results = []

        for query_entry in batch.query_list:
            res = self._lookup_entry(
                system,
                benchmark=batch.benchmark,
                query_entry=query_entry,
                exec_settings=batch.exec_settings,
                general_system_config=batch.general_system_config,
            )
            assert res is not None, (
                f"After executing missing entries, expected all entries to be in cache, but {query_entry.query_id} is still missing"
            )
            results.append(res)

        return results

    def _lookup_entry(
        self,
        system: str,
        benchmark: Workload,
        query_entry: QueryEntry,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ) -> QueryExecutionResult | None:
        cache_filepath, hash = self._get_cache_filepath(
            system,
            benchmark,
            query_entry,
            exec_settings,
            general_system_config=general_system_config,
        )

        # check in-mem cache first
        if hash in self.in_mem_cache:
            return self.in_mem_cache[hash]

        # check disk cache
        if cache_filepath.exists():
            loaded = utils.load_pickle(cache_filepath, QueryExecutionResult)
            assert loaded is not None

            # store in in-mem cache
            self.in_mem_cache[hash] = loaded

            return loaded

        return None

    def _get_cache_filepath(
        self,
        system: str,
        benchmark: Workload,
        query_entry: QueryEntry,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ) -> tuple[Path, str]:
        # Create a stable hash of the query entry and args by converting them to a JSON string with sorted keys
        entry_dict = {
            "system": system,
            "benchmark": benchmark.name,
            "query_entry": utils.stable_json(asdict(query_entry)),
            "general_system_config": utils.stable_json(asdict(general_system_config)),
            "exec_settings": utils.stable_json(asdict(exec_settings)),
        }
        hash_payload = utils.stable_json(entry_dict)
        hash = utils.sha256(hash_payload)

        cache_filepath = self.query_execution_cache_dir / f"{hash}.pkl"
        return cache_filepath, hash

    def _exec_missing_queries(
        self,
        system: str,
        benchmark: Workload,
        missing_entries: list[QueryEntry],
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
        do_not_cache: bool = False,
    ):
        for query_entry in missing_entries:
            system_instance = self.system_factory.get_system(
                system,
                benchmark=benchmark,
                exec_settings=exec_settings,
                general_system_config=general_system_config,
            )

            if system == "DuckDB":
                assert isinstance(system_instance, DuckDBConnectionManager)
                try:
                    duckdb_time, duckdb_df, duckdb_plan = system_instance.duckdb_sql(
                        query_entry.sql
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to execute Q{query_entry.query_id} with DuckDB: {e}\n{query_entry.sql}"
                    )
                    raise e

                exec_result = QueryExecutionResult(
                    system=system,
                    query_entry=query_entry,
                    exec_settings=exec_settings,
                    general_system_config=general_system_config,
                    result=duckdb_df,
                    exec_time_ms=duckdb_time,
                    plan=duckdb_plan,
                )
            elif system == "Umbra":
                assert isinstance(system_instance, UmbraRunner)
                assert isinstance(exec_settings, OLAPExecSettings)
                results = system_instance.run_scale_factor(
                    scale_factor=exec_settings.scale_factor,
                    query_list=[query_entry.query_id],
                    sql_list=[query_entry.sql],
                    args_list=[query_entry.query_args],
                )
                assert len(results) == 1
                plan, umbra_time_ms = results[0]

                exec_result = QueryExecutionResult(
                    system=system,
                    query_entry=query_entry,
                    exec_settings=exec_settings,
                    general_system_config=general_system_config,
                    result=None,  # for Umbra we don't collect result
                    exec_time_ms=umbra_time_ms,
                    plan=plan,
                )

            else:
                raise ValueError(f"Unsupported system: {system}")

            # save to cache
            cache_filepath, hash = self._get_cache_filepath(
                system,
                benchmark,
                query_entry,
                exec_settings,
                general_system_config,
            )
            utils.dump_pickle(cache_filepath, exec_result, do_not_cache=do_not_cache)
