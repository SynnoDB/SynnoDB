import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
from tqdm import tqdm

from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from synnodb.observability.benchmark.systems.umbra import UmbraRunner
from synnodb.utils.sql_utils import extract_order_by_columns
from synnodb.utils.utils import (
    DBStorage,
    create_dir_and_set_permissions,
    dump_pickle,
    load_pickle,
    sha256,
    stable_json,
)

logger = logging.getLogger(__name__)


@dataclass
class QueryInstantiation:
    """Represents a single query instantiation with its metadata."""

    query_id: str
    sql: str
    placeholders: Dict[str, str]
    order_by_info: List[Tuple[str, str]]
    duckdb_result: pd.DataFrame
    duckdb_exec_time_ms: float
    duckdb_plan: Dict
    scale_factor: float
    num_threads: int
    umbra_plan: Optional[Dict] = None
    umbra_exec_time_ms: Optional[float] = None
    db_storage: DBStorage = DBStorage.IN_MEMORY


class QueryCache:
    """
    Pre-generates query instantiations and caches DuckDB results.
    """

    def __init__(
        self,
        gen_query_fn: Callable,
        query_ids: List[str],
        sf_list: List[float],
        num_instantiations_per_query: int,
        duckdb_managers: Optional[Dict[float, DuckDBConnectionManager]],
        cache_dir: Path,
        db_storage: DBStorage,
        run_umbra_plans: bool = False,
        umbra_runner: UmbraRunner | None = None,  # type: ignore  # noqa: F821
        only_from_cache: bool = False,
        do_not_cache: bool = False,
        num_threads: int = 1,  # 1=single threaded
    ):
        """
        Initialize the query cache.

        Parameters
        ----------
        query_ids : List[str]
            List of query IDs to pre-generate
        sf_list : List[int]
            List of scale factors to pre-generate for
        num_instantiations_per_query : int
            Number of instantiations to generate per query
        duckdb_managers : Dict[int, DuckDBConnectionManager]
            DuckDB connection managers keyed by scale factor
        cache_dir : Path
            Directory to store cache files (default: "cache")
        num_threads : Optional[int]
            Thread count of the DuckDB managers provided. Included in the cache key
            so ST (num_threads=None/1) and MT (num_threads=N) caches are stored separately.
        """
        self.gen_query_fn = gen_query_fn
        self.query_ids = query_ids
        self.sf_list = sf_list
        self.num_instantiations_per_query = num_instantiations_per_query
        self.duckdb_managers = duckdb_managers
        self.db_storage = db_storage

        # check that all provided duckdb managers have the same in_memory/disk_source as the QueryCache
        if self.duckdb_managers is not None:
            for mgr in self.duckdb_managers.values():
                assert mgr.db_storage == self.db_storage, (
                    f"All DuckDB managers must have the same db_storage as the QueryCache (expected {self.db_storage}, got {mgr.db_storage})"
                )

        self.cache_dir = cache_dir
        self._run_umbra_plans = run_umbra_plans
        self.umbra_runner = umbra_runner
        self.only_from_cache = only_from_cache
        self.do_not_cache = do_not_cache
        self.num_threads = num_threads
        create_dir_and_set_permissions(self.cache_dir)

        if self._run_umbra_plans and self.umbra_runner is None:
            raise ValueError("umbra_runner must be provided when run_umbra_plans=True")

        # Cache structure: {scale_factor: {query_id: [QueryInstantiation, ...]}}
        self.cache: Dict[float, Dict[str, List[QueryInstantiation]]] = {}

        # Pre-generate all query instantiations
        self._pregenerate_queries()

    def _pregenerate_queries(self):
        """Pre-generate all query instantiations and cache DuckDB results."""

        rnd = random.Random(42)

        for sf in self.sf_list:
            self.cache[sf] = {}

            if self.duckdb_managers is not None:
                if sf not in self.duckdb_managers:
                    logger.warning(f"No DuckDB manager found for SF{sf}, skipping")
                    continue

                duckdb_con = self.duckdb_managers[sf]
            else:
                duckdb_con = None

            # keep for first two sf positions the same number of instantiations, but for larger sfs only generate 1 instantiation to save time and disk space
            max_pos = 2 if len(self.sf_list) > 2 else len(self.sf_list) - 1

            if (
                sf >= sorted(self.sf_list)[max_pos]
            ):  # Only generate 1 instantiation for the largest SF
                num_instantiations = 1
            else:
                num_instantiations = self.num_instantiations_per_query

            # check which duckdb queries do not exist in cache
            dd_tasks = []
            for query_id in self.query_ids:
                query_id_str = str(query_id)

                # Try to load from cache first
                cached_instantiations = self._load_from_disk(
                    sf, query_id_str, num_instantiations
                )

                # check that num_threads matches if loaded from cache
                for inst in cached_instantiations or []:
                    # check if has attr num_threads
                    if hasattr(inst, "num_threads"):
                        assert inst.num_threads == self.num_threads, (
                            f"Cached instantiations for SF{sf} and query {query_id_str} have num_threads={inst.num_threads}, but QueryCache was initialized with num_threads={self.num_threads}"
                        )

                if cached_instantiations is not None:
                    self.cache[sf][query_id_str] = cached_instantiations
                    continue

                else:
                    # not found in cache - has to be executed
                    dd_tasks.append(query_id)

            if self.only_from_cache:
                assert len(dd_tasks) == 0, (
                    f"Queries {dd_tasks} not found in cache, but only_from_cache=True"
                )

            if len(dd_tasks) > 0:
                # execute the queries
                self._exec_queries_not_cached(
                    sf=sf,
                    num_instantiations=num_instantiations,
                    duckdb_con=duckdb_con,
                    rnd=rnd,
                )

            # Patch up cached instantiations that pre-date umbra: when umbra
            # is requested but a cached QueryInstantiation has no umbra time,
            # run umbra now, mutate the instantiation in place, and re-save.
            if self._run_umbra_plans:
                self._patch_missing_umbra_for_sf(sf, num_instantiations)

        logger.info(
            "Query pre-generation complete (loaded from cache or executed again)"
        )

    def _patch_missing_umbra_for_sf(self, sf: float, num_instantiations: int) -> None:
        """Fill in missing umbra_exec_time_ms/umbra_plan on cached instantiations
        for this scale factor, then re-save affected cache files."""
        qids_to_resave: List[str] = []
        for query_id_str, instantiations in self.cache[sf].items():
            patched = False
            for inst in instantiations:
                if inst.umbra_exec_time_ms is not None:
                    continue
                try:
                    umbra_plan, umbra_time = self._run_umbra_for_plan_and_runtime(
                        inst.sql, sf=sf
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to patch UMBRA for Q{query_id_str} sf={sf}: {e}"
                    )
                    continue
                inst.umbra_plan = umbra_plan
                inst.umbra_exec_time_ms = umbra_time
                patched = True
            if patched:
                qids_to_resave.append(query_id_str)

        if not qids_to_resave:
            return

        logger.info(
            f"Patched UMBRA times for {len(qids_to_resave)} queries at SF{sf}: "
            f"{qids_to_resave}"
        )
        if self.do_not_cache:
            return
        for query_id_str in qids_to_resave:
            self._save_to_disk(
                sf, query_id_str, num_instantiations, allow_overwrite=True
            )

    def _exec_queries_not_cached(
        self,
        sf: float,
        num_instantiations: int,
        duckdb_con: Optional[DuckDBConnectionManager],
        rnd: random.Random,
    ):
        for query_id in tqdm(
            self.query_ids,
            desc=f"Gen and exec {num_instantiations} queries for SF{sf}",
        ):
            # only cached versions work without duckdb managers
            assert duckdb_con is not None, "DuckDB managers must be provided"

            # check that num_threads matches
            assert duckdb_con.num_threads == self.num_threads, (
                f"DuckDB manager for SF{sf} has num_threads={duckdb_con.num_threads}, but QueryCache was initialized with num_threads={self.num_threads}"
            )

            query_id_str = str(query_id)
            self.cache[sf][query_id_str] = []

            instantiations_generated = 0
            seen_sqls = set()

            # Generate unique query instantiations
            max_attempts = num_instantiations * 10
            attempts = 0

            while (
                instantiations_generated < num_instantiations
                and attempts < max_attempts
            ):
                attempts += 1

                # Generate a random instantiation
                template, sql, placeholders = self.gen_query_fn(
                    query_name=f"Q{query_id_str}", rnd=rnd
                )

                # Skip duplicates
                if sql in seen_sqls:
                    continue

                seen_sqls.add(sql)

                # Extract order by information
                order_by_info = extract_order_by_columns(sql)

                # Execute with DuckDB and cache result
                try:
                    duckdb_time, duckdb_df, duckdb_plan = duckdb_con.duckdb_sql(sql)
                except Exception as e:
                    logger.error(
                        f"Failed to execute Q{query_id_str} with DuckDB: {e}\n{sql}"
                    )
                    raise e
                    continue

                try:
                    umbra_plan = None
                    umbra_time = None
                    if self._run_umbra_plans:
                        try:
                            umbra_plan, umbra_time = (
                                self._run_umbra_for_plan_and_runtime(sql, sf=sf)
                            )

                        except Exception as e:
                            logger.warning(
                                f"Failed to get UMBRA plan for Q{query_id_str}: {e}"
                            )

                    # Create instantiation object
                    instantiation = QueryInstantiation(
                        query_id=query_id_str,
                        sql=sql,
                        placeholders=placeholders,
                        order_by_info=order_by_info,
                        duckdb_result=duckdb_df.copy(),
                        duckdb_exec_time_ms=duckdb_time,
                        duckdb_plan=duckdb_plan,
                        scale_factor=sf,
                        num_threads=self.num_threads,
                        umbra_plan=umbra_plan,
                        umbra_exec_time_ms=umbra_time,
                        db_storage=self.db_storage,
                    )

                    self.cache[sf][query_id_str].append(instantiation)
                    instantiations_generated += 1
                except Exception as e:
                    logger.error(
                        f"Failed to execute Q{query_id_str} with Umbra: {e}\n{sql}"
                    )
                    continue

            # Save to disk after generating all instantiations for this query
            if not self.do_not_cache:
                if self.cache[sf][query_id_str]:
                    self._save_to_disk(sf, query_id_str, num_instantiations)

    def _get_cache_filepath(
        self,
        sf: float,
        query_id_str: str,
        num_instantiations: int,
    ) -> Path:
        """Get the cache file path for a specific query configuration."""
        hash_payload = {
            "sf": sf,
            "query_id": query_id_str,
            "num_instantiations": num_instantiations,
            "num_threads": self.num_threads,
            "db_storage": self.db_storage.value,
        }

        hash = sha256(stable_json(hash_payload))
        filename = f"{hash}.pkl"
        return self.cache_dir / filename

    def _save_to_disk(
        self,
        sf: float,
        query_id_str: str,
        num_instantiations: int,
        allow_overwrite: bool = False,
    ):
        """Save query instantiations to disk."""
        filepath = self._get_cache_filepath(sf, query_id_str, num_instantiations)
        try:
            instantiations = self.cache[sf][query_id_str]
            dump_pickle(
                filepath,
                instantiations,
                do_not_cache=self.do_not_cache,
                assert_not_exists=not allow_overwrite,
            )
        except Exception as e:
            logger.error(f"Failed to save cache to {filepath}: {e}")

    def _load_from_disk(
        self, sf: float, query_id_str: str, num_instantiations: int
    ) -> List[QueryInstantiation] | None:
        """Load query instantiations from disk if available."""
        filepath = self._get_cache_filepath(sf, query_id_str, num_instantiations)
        if not filepath.exists():
            return None

        try:
            instantiations = load_pickle(filepath, expected=list)
            return instantiations
        except Exception as e:
            logger.error(f"Failed to load cache from {filepath}: {e}")
            return None

    def _run_umbra_for_plan_and_runtime(
        self, sql: str, sf: float
    ) -> Tuple[Dict, float]:
        """Run EXPLAIN (ANALYZE, FORMAT JSON) on Umbra and return the parsed plan dict."""

        assert self.umbra_runner is not None

        if not self.umbra_runner.setup_done:
            self.umbra_runner.setup()

        if self.umbra_runner._db_storage == DBStorage.IN_MEMORY:
            self.umbra_runner._switch_sf(sf)
        else:
            # Disk-based: restart container + drop OS page caches before every
            # query so the EXPLAIN ANALYZE reflects a cold buffer pool and cold
            # page cache (same semantics as run_scale_factor with in_memory=False).
            self.umbra_runner._cold_restart_for_query(sf)

        assert self.umbra_runner._con is not None, "Umbra connection not initialized."

        cur = self.umbra_runner._con.cursor()
        try:
            cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
            row = cur.fetchone()
            if row is None:
                raise ValueError("EXPLAIN returned no rows.")
            plan = json.loads(row[0])

            analyze_pipelines = plan["analyzePlanPipelines"]
            runtime_ms = sum(
                [p["duration"] for p in analyze_pipelines]
            )  # this is wrongly annotated here - it is nano seconds, not ms. To prevent downstream issues, this is not renamed for now, but should be kept in mind when analyzing the results.

            return plan, runtime_ms

        finally:
            cur.close()

    def get_instantiations(
        self,
        scale_factor: float,
        query_id: str | List[str] | None = None,
        num_samples: int | None = None,
    ) -> List[QueryInstantiation]:
        """
        Get query instantiations from cache.

        Parameters
        ----------
        scale_factor : float
            Scale factor to retrieve instantiations for
        query_id : str | List[str] | None
            Query ID(s) to retrieve. If None, retrieve all queries.
        num_samples : int | None
            Number of samples to retrieve per query. If None, retrieve all.

        Returns
        -------
        List[QueryInstantiation]
            List of query instantiations
        """
        if scale_factor not in self.cache:
            logger.error(f"Scale factor {scale_factor} not found in cache")
            return []

        # Determine which query IDs to retrieve
        if isinstance(query_id, list):
            query_ids_to_get = [str(qid) for qid in query_id]
        elif query_id is not None:
            query_ids_to_get = [str(query_id)]
        else:
            query_ids_to_get = list(self.cache[scale_factor].keys())

        # Collect instantiations
        instantiations = []
        rnd = random.Random(42)  # Use consistent seed for sampling

        for qid in query_ids_to_get:
            if qid.startswith("q") or qid.startswith("Q"):
                # strip the leading q
                qid = qid[1:]
            if qid not in self.cache[scale_factor]:
                logger.warning(f"Query {qid} not found in cache for SF{scale_factor}")
                continue

            available = self.cache[scale_factor][qid]

            if num_samples is None or num_samples >= len(available):
                # Return all available instantiations
                instantiations.extend(available)
            else:
                # Sample without replacement
                sampled = rnd.sample(available, num_samples)
                instantiations.extend(sampled)

        return instantiations

    def get_cache_stats(self) -> Dict:
        """Get statistics about the cache."""
        stats = {}
        for sf in self.cache:
            stats[sf] = {}
            for qid in self.cache[sf]:
                stats[sf][qid] = len(self.cache[sf][qid])
        return stats
