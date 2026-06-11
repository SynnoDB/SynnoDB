from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from utils import utils
from workloads.workload_provider import ExecSettings, QueryBatch, QueryEntry


@dataclass
class QueryExecutionResult:
    system: str
    query_entry: QueryEntry
    exec_settings: ExecSettings  # e.g. scale-factor, storage medium, ...
    num_threads: int
    result: pd.DataFrame
    exec_time_ms: float
    plan: dict | None


class QueryExecutionCache:
    query_execution_cache_dir: Path

    in_mem_cache: dict[str, QueryExecutionResult]

    def __init__(self, query_execution_cache_dir: Path):
        self.query_execution_cache_dir = query_execution_cache_dir
        utils.create_dir_and_set_permissions(self.query_execution_cache_dir)

    def lookup_or_execute_query_batch(
        self,
        batch: QueryBatch,
        system: str,
        num_threads: int,
        do_not_cache: bool = False,
    ) -> list[QueryExecutionResult]:
        # lookup which entries are missing
        missing_entries = []

        for query_entry in batch.query_list:
            # first lookup might load from disk and populate in-mem cache, subsequent lookups will be faster
            exec_result = self._lookup_entry(
                system,
                query_entry,
                batch.exec_settings,
                num_threads=num_threads,
            )
            if exec_result is None:
                missing_entries.append(query_entry)

        # execute missing
        self._exec_missing_queries(
            system,
            missing_entries,
            batch.exec_settings,
            num_threads,
            do_not_cache=do_not_cache,
        )

        # after execution, all entries should be in cache, so we can lookup again to get the results
        results = []

        for query_entry in batch.query_list:
            res = self._lookup_entry(
                system, query_entry, batch.exec_settings, num_threads
            )
            assert res is not None, (
                f"After executing missing entries, expected all entries to be in cache, but {query_entry.query_id} is still missing"
            )
            results.append(res)

        return results

    def _lookup_entry(
        self,
        system: str,
        query_entry: QueryEntry,
        exec_settings: ExecSettings,
        num_threads: int,
    ) -> QueryExecutionResult | None:
        cache_filepath, hash = self._get_cache_filepath(
            system,
            query_entry,
            exec_settings,
            num_threads=num_threads,
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
        query_entry: QueryEntry,
        exec_settings: ExecSettings,
        num_threads: int,
    ) -> tuple[Path, str]:
        # Create a stable hash of the query entry and args by converting them to a JSON string with sorted keys
        entry_dict = {
            "system": system,
            "query_id": query_entry.query_id,
            "sql": query_entry.sql,
            "query_args": query_entry.query_args,
            "num_threads": num_threads,
            "exec_settings": utils.stable_json(asdict(exec_settings)),
        }
        hash_payload = utils.stable_json(entry_dict)
        hash = utils.sha256(hash_payload)

        cache_filepath = self.query_execution_cache_dir / f"{hash}.pkl"
        return cache_filepath, hash

    def _exec_missing_queries(
        self,
        system: str,
        missing_entries: list[QueryEntry],
        exec_settings: ExecSettings,
        num_threads: int,
        do_not_cache: bool = False,
    ):
        for query_entry in missing_entries:
            exec_result: QueryExecutionResult = None

            # save to cache
            cache_filepath, hash = self._get_cache_filepath(
                system,
                query_entry,
                exec_settings,
                num_threads=num_threads,
            )
            utils.dump_pickle(cache_filepath, exec_result, do_not_cache=do_not_cache)
