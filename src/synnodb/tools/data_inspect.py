"""A read-only SQL window into the actual benchmark data - for quick, cheap look-ups.

The agent designs a bespoke engine for a dataset it otherwise only sees as schema DDL.
``DataInspectTool`` gives it a DBA-style view of the *content*: cardinalities, value
distributions, null density, min/max ranges, distinct counts, join fan-out - the shape
that drives physical-design choices (element types, encodings, partitioning, join order).

It is a lightweight replacement for parquet-dump shell commands, meant for *simple* queries -
peeking at rows, ranges, and per-column stats. To keep it cheap it reads the smallest
representative subset (the workload's smallest fast-check rung), not the full benchmark scale:
distributions and value domains carry over, but a scan touches a fraction of the rows. Two
guards keep a stray heavy query from stalling the whole conversation: every statement runs under
a wall-clock budget (:data:`QUERY_TIMEOUT_S`) and is interrupted if it overruns, and it runs on
an isolated cursor so that interrupt never touches the cached connection.

It is strictly read-only: every statement is gated by the same read-only classifier the
DuckDB-compat write guard uses, and DuckDB-native subsets are additionally opened
``read_only=True``. No statement can mutate the source data or leave scratch state behind.

Results are cached on disk keyed by the query, its row cap, and the identity of the inspected
subset (workload, scale factor, dataset version). The subset is read-only benchmark data that
never changes within a run, so a repeated inspection replays instantly - and, under
``only_from_cache``, a whole run replays deterministically without re-touching the data. This
mirrors the shell / compile / apply-patch tool caches. Timeouts are never cached: the wall-clock
budget is a host-dependent guard, not a property of the data.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.router.normalize import is_read_only_query
from synnodb.synth_framework.runtime_tracker import RuntimeTracker
from synnodb.utils import utils
from synnodb.utils.utils import ServeFrom
from synnodb.workloads.workload_spec import SUBSET_DUCKDB_FILENAME, find_sf_dir

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 100
MAX_ROWS_CAP = 1000
# Restrict output to ~10000 chars (~2.5k tokens), matching RunTool's truncation budget.
OUTPUT_CHAR_LIMIT = 10000
# Wall-clock budget for a single inspection query. This tool is for simple, cheap look-ups; a
# query that overruns this is interrupted and the agent is told to simplify. Comfortably above
# the sub-second cost of a well-scoped query on the small inspection subset, even on a busy host.
QUERY_TIMEOUT_S = 15.0
# Length cap for SQL echoed into log lines, so a pasted mega-query cannot bloat the logs.
LOG_SQL_LIMIT = 300


def _short_sql(sql: str) -> str:
    """Single-line, length-bounded rendering of a query for log lines."""
    collapsed = " ".join(sql.split())
    return collapsed if len(collapsed) <= LOG_SQL_LIMIT else collapsed[:LOG_SQL_LIMIT] + "..."


class DataInspectCacheType:
    """One cached inspection: the rendered output text plus the payload it was keyed on (for
    debugging cache hits) and the wall-clock it originally took (credited back to the runtime
    tracker as skipped time on a cache hit, exactly like the shell tool)."""

    def __init__(self, output: str, hash_payload: str, runtime_seconds: float):
        self.output = output
        self.hash_payload = hash_payload
        self.runtime_seconds = runtime_seconds


class DataInspectTool:
    """Runs a strictly read-only SQL query against the workload's benchmark-scale data."""

    def __init__(
        self,
        workload_provider: Any,
        sf: float | None = None,
        cache_dir: Path | None = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        runtime_tracker: RuntimeTracker | None = None,
        run_stats_collector: RunStatsCollector | None = None,
    ):
        self.workload_provider = workload_provider
        self.spec = workload_provider.spec
        # Inspect the smallest representative subset by default (the smallest fast-check rung, which
        # prepare() always materializes). This tool is for cheap look-ups, not benchmark-scale
        # measurement: value domains and distributions carry over from the small subset, while a
        # full scan touches a fraction of the rows - so an unindexed aggregate stays sub-second
        # instead of chewing every core on tens of millions of rows.
        self.sf: float = sf if sf is not None else self._default_inspect_sf(workload_provider)
        self._con: duckdb.DuckDBPyConnection | None = None

        # Disk cache, keyed by query + row cap + subset identity. Disabled (None) when no cache
        # dir is supplied, so the tool still works standalone. do_not_cache runs live but never
        # writes; only_from_cache refuses to run anything not already cached (deterministic replay).
        self.cache_dir = cache_dir
        self.do_not_cache = do_not_cache
        self.only_from_cache = only_from_cache
        self.runtime_tracker = runtime_tracker
        # Report every inspection to the live dashboard / supervisor activity log, exactly like the
        # shell / compile / run tools. Optional so the tool still works standalone (e.g. in tests).
        self.run_stats_collector = run_stats_collector
        if self.cache_dir is not None:
            utils.create_dir_and_set_permissions(self.cache_dir)

    @staticmethod
    def _default_inspect_sf(workload_provider: Any) -> float:
        """The cheapest subset to inspect: the smallest fast-check rung, or the benchmark SF when a
        workload defines no fast-check ladder. Both are guaranteed on disk after ``prepare()``."""
        fast_check_sfs = workload_provider.spec.fast_check_sfs
        return min(fast_check_sfs) if fast_check_sfs else workload_provider.benchmark_sf

    def _resolve_subset_dir(self) -> Path:
        # Fractional subsets are downscaled lazily; make sure the one we need exists. Idempotent
        # and cheap once present, and a no-op for built-ins / plain BYO-parquet.
        self.workload_provider.prepare()
        base = Path(self.workload_provider.base_parquet_dir)
        subset_dir = find_sf_dir(base, self.sf)
        if subset_dir is None:
            raise FileNotFoundError(
                f"No subset directory for fraction/SF {self.sf:g} under {base}."
            )
        return subset_dir

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if self._con is not None:
            return self._con
        subset_dir = self._resolve_subset_dir()
        if self.spec.serve_from == ServeFrom.DUCKDB:
            subset_db = subset_dir / SUBSET_DUCKDB_FILENAME
            if not subset_db.exists():
                raise FileNotFoundError(
                    f"No DuckDB-native subset database at {subset_db} "
                    f"(expected {SUBSET_DUCKDB_FILENAME})."
                )
            # Open the subset itself read-only: its tables resolve under their real names and the
            # storage layer rejects any write, so nothing the agent runs can mutate the source.
            con = duckdb.connect(subset_db.as_posix(), read_only=True)
        else:
            # A parquet subset has no single database file, so expose each table as a read-only
            # view over its parquet inside a throwaway in-memory database. The view setup is ours;
            # the agent's own SQL is still gated to read-only below, so it cannot add scratch state.
            con = duckdb.connect(":memory:")
            for table in self.spec.tables:
                parquet = (subset_dir / f"{table}.parquet").as_posix().replace("'", "''")
                con.execute(
                    f'CREATE VIEW "{table}" AS SELECT * FROM read_parquet(\'{parquet}\')'
                )
        self._con = con
        return con

    def _cache_key(self, sql: str, row_limit: int) -> tuple[str, str]:
        """Build the (payload, hash) the result is cached under. The payload pins the query text,
        its row cap, and the identity of the inspected subset - workload, scale factor, and dataset
        version - so an unrelated workload or a regenerated dataset can never collide with a stale
        entry, while re-running the same query against the same read-only subset always hits."""
        payload = {
            "sql": sql,
            "row_limit": row_limit,
            "sf": self.sf,
            "workload": getattr(self.spec, "name", None),
            "dataset_name": getattr(self.spec, "dataset_name", None),
            "dataset_version": getattr(self.spec, "dataset_version", None),
            "serve_from": getattr(self.spec.serve_from, "value", str(self.spec.serve_from)),
        }
        hash_payload = utils.stable_json(payload)
        return hash_payload, utils.sha256(hash_payload)

    def __call__(self, sql: str, max_rows: int | None = None) -> str:
        row_limit = (
            DEFAULT_MAX_ROWS
            if max_rows is None
            else max(1, min(int(max_rows), MAX_ROWS_CAP))
        )
        sql = (sql or "").strip()
        output, status, cached = self._inspect(sql, row_limit)
        self._report(sql, row_limit, output, status, cached)
        return output

    def _inspect(self, sql: str, row_limit: int) -> tuple[str, str, bool]:
        """Resolve one inspection to ``(output_text, status, served_from_cache)``. ``status`` is a
        short label (``ok`` / ``sql_error`` / ``timeout`` / ``rejected`` / ``empty`` / ``prep_error``)
        that drives the dashboard row and activity-summary line; the rendered text is what the agent
        sees. Every outcome returns here (no bare returns) so ``__call__`` reports exactly once."""
        if not sql:
            return "Error: empty SQL query.", "empty", False
        if not is_read_only_query(sql):
            logger.debug("query_data rejected non-read-only SQL: %s", _short_sql(sql))
            return (
                "Error: query_data is strictly read-only. Only SELECT / WITH / EXPLAIN / SHOW / "
                "DESCRIBE / SUMMARIZE / VALUES and read-only PRAGMA statements are allowed; this "
                "statement would write or change state.",
                "rejected",
                False,
            )

        hash_payload, cache_path = "", None
        if self.cache_dir is not None:
            hash_payload, hash = self._cache_key(sql, row_limit)
            cache_path = self.cache_dir / f"{hash}.pkl"
            if cache_path.exists():
                cached = utils.load_pickle(cache_path, DataInspectCacheType)
                assert cached is not None
                if self.runtime_tracker is not None:
                    self.runtime_tracker.add_skipped_time(cached.runtime_seconds)
                logger.debug("query_data served from cache: %s", cache_path.name)
                return cached.output, _status_from_output(cached.output), True
            if self.only_from_cache:
                raise ValueError(
                    "query_data result not found in cache and only_from_cache is enabled. "
                    f"Cache path: {cache_path}\nPayload: {hash_payload}"
                )

        try:
            con = self._connect()
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the agent as text
            logger.warning("query_data could not prepare data (sf=%g): %s", self.sf, exc)
            return f"Error preparing data for inspection: {exc}", "prep_error", False

        output, cacheable, elapsed = self._run_and_render(con, sql, row_limit)
        # Cache only deterministic outcomes (a result set or a SQL error - both fixed by the
        # query and the read-only subset). Timeouts are host-dependent, so caching one would
        # permanently pin a query that might succeed on a less busy host.
        if cache_path is not None and cacheable and not self.do_not_cache:
            utils.dump_pickle(
                cache_path,
                DataInspectCacheType(
                    output=output, hash_payload=hash_payload, runtime_seconds=elapsed
                ),
                do_not_cache=self.do_not_cache,
            )
        # A non-cacheable outcome is the wall-clock timeout; everything else is a result set or a
        # deterministic SQL error, both classified from the rendered text.
        status = "timeout" if not cacheable else _status_from_output(output)
        return output, status, False

    def _report(
        self, sql: str, row_limit: int, output: str, status: str, cached: bool
    ) -> None:
        """Surface this inspection to the live dashboard and the supervisor activity log, mirroring
        the shell / compile / run tools. A no-op when no collector is wired (standalone use)."""
        if self.run_stats_collector is None:
            return
        is_error = status != "ok"
        self.run_stats_collector.log_metrics_callback(
            {
                "type": "data_inspect",
                "data_inspect/sql": sql[:20000],
                "data_inspect/max_rows": row_limit,
                "data_inspect/sf": self.sf,
                "data_inspect/status": status,
                "data_inspect/error": is_error,
                "data_inspect/cached": cached,
                "data_inspect/output": output[:20000],
                "data_inspect/truncated": len(output) > 20000,
            },
            log_and_increment=True,
        )
        self.run_stats_collector.add_to_activity_summary(
            f"Data Inspect Tool called: {status}{' (cached)' if cached else ''}"
        )

    def _run_and_render(
        self, con: duckdb.DuckDBPyConnection, sql: str, row_limit: int
    ) -> tuple[str, bool, float]:
        """Execute *sql* on a throwaway cursor under the wall-clock budget and render the result.

        Returns ``(output_text, cacheable, elapsed_seconds)``. Interrupting the cursor (never the
        cached connection) means a late interrupt that races query completion cannot poison a later
        inspection; ``Timer.cancel()`` is a no-op once the timer has already fired. A timeout is
        reported as ``cacheable=False`` (the budget is a host-dependent guard); everything else is
        deterministic given the read-only subset and is cached."""
        cur = con.cursor()
        timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
        timer.daemon = True
        timer.start()
        logger.debug("query_data (sf=%g): %s", self.sf, _short_sql(sql))
        started = time.perf_counter()
        try:
            cur.execute(sql)
            rows = cur.fetchmany(row_limit + 1)
            column_names = [d[0] for d in cur.description] if cur.description else []
        except duckdb.InterruptException:
            elapsed = time.perf_counter() - started
            logger.warning(
                "query_data exceeded the %gs budget, cancelled after %.1fs: %s",
                QUERY_TIMEOUT_S,
                elapsed,
                _short_sql(sql),
            )
            return (
                f"Error: query exceeded the {QUERY_TIMEOUT_S:g}s inspection budget and was "
                "cancelled. query_data is for simple, cheap look-ups - narrow it with a WHERE "
                "filter, add LIMIT, aggregate fewer columns, or use SUMMARIZE/DESCRIBE on a single "
                "table instead of scanning or joining large tables.",
                False,
                elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - return DuckDB's message so the agent can fix it
            elapsed = time.perf_counter() - started
            logger.debug("query_data SQL error after %.2fs: %s", elapsed, exc)
            return f"SQL error: {exc}", True, elapsed
        finally:
            timer.cancel()
        elapsed = time.perf_counter() - started
        logger.debug(
            "query_data ok in %.2fs: %d row(s), %d column(s)",
            elapsed,
            len(rows),
            len(column_names),
        )
        return _render_result(column_names, rows, row_limit), True, elapsed


def _status_from_output(output: str) -> str:
    """Classify a rendered inspection result for reporting. A DuckDB error is echoed verbatim as
    ``SQL error: ...``; everything else that reaches here is a successful result set. Timeouts are
    classified by the caller (they are not cacheable and never round-trip through this)."""
    return "sql_error" if output.startswith("SQL error:") else "ok"


def _fmt_value(value: Any) -> str:
    return "NULL" if value is None else str(value)


def _render_result(column_names: list[str], rows: list, row_limit: int) -> str:
    """Render a result set as a compact, char-bounded text table."""
    if not column_names:
        return "OK (no result set)."
    truncated = len(rows) > row_limit
    shown = rows[:row_limit]
    header = " | ".join(column_names)
    lines = [header, "-" * min(len(header), 80)]
    for row in shown:
        lines.append(" | ".join(_fmt_value(v) for v in row))
    footer = f"({len(shown)} row{'' if len(shown) == 1 else 's'}"
    if truncated:
        footer += (
            f"; output capped at {row_limit}, more rows exist - "
            "refine with LIMIT or aggregation"
        )
    footer += ")"
    lines.append(footer)
    out = "\n".join(lines)
    if len(out) > OUTPUT_CHAR_LIMIT:
        out = out[:OUTPUT_CHAR_LIMIT] + "\n... (output truncated)"
    return out
