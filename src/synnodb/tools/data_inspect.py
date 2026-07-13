"""A read-only SQL window into the actual benchmark data - for quick, cheap look-ups.

The agent designs a bespoke engine for a dataset it otherwise only sees as schema DDL.
``DataInspectTool`` gives it a DBA-style view of the *content*: cardinalities, value
distributions, null density, min/max ranges, distinct counts, join fan-out - the shape
that drives physical-design choices (element types, encodings, partitioning, join order).

It is a lightweight replacement for parquet-dump shell commands, meant for *simple* queries -
peeking at rows, ranges, and per-column stats. The agent picks *which* subset each query runs on
(``sf``): every subset materialized on disk is on the menu, including the benchmark one, and the
prompts steer it to the smallest subset that answers the question. Omitting ``sf`` reads the
cheapest representative subset (the workload's smallest fast-check rung). That default is
deliberately spec-derived rather than read off the disk, because the resolved subset is part of
the cache key - a disk-dependent default would resolve differently on an ``only_from_cache``
replay (which runs with the data gone) than on the recording run. Relative facts - distributions,
distinct ratios, null density, value ranges - carry over from a small subset; absolute row counts
do not, which is why the menu labels the benchmark subset. Two guards keep a stray heavy query
from stalling the whole conversation: every statement runs under a wall-clock budget
(:data:`QUERY_TIMEOUT_S`, flat across subsets) and is interrupted if it overruns - the message
then points at the smaller subsets it could retry on - and it runs on an isolated cursor so that
interrupt never touches the cached connection.

It is strictly read-only: every statement is gated by the same read-only classifier the
DuckDB-compat write guard uses, and DuckDB-native subsets are additionally opened
``read_only=True``. No statement can mutate the source data or leave scratch state behind.

Results are cached on disk keyed by the query, its row cap, and the identity of the inspected
subset (workload, scale factor, dataset version). The subset is read-only benchmark data that
never changes within a run, so a repeated inspection replays instantly - and, under
``only_from_cache``, a whole run replays deterministically without re-touching the data. This
mirrors the shell / compile / apply-patch tool caches. Every executed outcome is cached - a
result set, a SQL error, or a timeout alike - so both successful and unsuccessful executions
replay from disk and ``only_from_cache`` never misses on a query the recording run actually ran.
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
from synnodb.workloads.workload_spec import (
    SUBSET_DUCKDB_FILENAME,
    available_subsets,
    find_sf_dir,
    format_subset_menu,
)

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
# Stable leading text of the timeout message. Shared by the message the agent sees and the
# status classifier, so a cached timeout replays with the ``timeout`` status rather than ``ok``.
_TIMEOUT_ERROR_PREFIX = "Error: query exceeded the"


def subset_menu_for(workload_provider: Any) -> str:
    """The ``query_data`` subset menu for a workload, for the planner / storage-plan prompts.

    The same text the tool description carries, built here so the prompts and the tool cannot
    disagree about which subsets exist or which one is the default. Empty when the workload cannot
    back the tool at all (the same guard ``main`` uses to decide whether to build it) or when
    nothing is materialized on disk - the prompts then simply say nothing about subsets."""
    if not hasattr(workload_provider, "spec") or not hasattr(
        workload_provider, "benchmark_sf"
    ):
        return ""
    # Same order as the tool: prepare() first (it downscales a BYO workload's fractional rungs),
    # so the prompt cannot advertise a menu that differs from the one the tool will honour.
    workload_provider.prepare()
    available = available_subsets(
        workload_provider.spec, Path(workload_provider.base_parquet_dir)
    )
    return format_subset_menu(
        available=available,
        benchmark_sf=workload_provider.benchmark_sf,
        default_sf=DataInspectTool._default_inspect_sf(workload_provider),
    )


def _canon_sf(value: float) -> float:
    """One spelling per subset: integral values as ints (``1``, not ``1.0``), matching how the
    spec's SF ladders and :func:`available_subsets` spell them. The subset is part of the cache
    key, so without this the agent passing ``sf=1`` (JSON float) and the spec's own ``1`` (int)
    would cache the identical inspection twice and re-run it."""
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else as_float


def _is_read_only_batch(sql: str) -> bool:
    """True when *sql* is several statements and every one of them is read-only.

    Used only to pick the right refusal message: such a batch is refused for being a batch, while
    anything containing a write keeps falling through to the read-only gate and is refused as a
    write. DuckDB's own parser does the splitting, so a semicolon inside a string literal does not
    count as a separator; unparseable SQL is not a batch (DuckDB reports the syntax error itself)."""
    try:
        statements = duckdb.extract_statements(sql)
    except Exception:  # noqa: BLE001 - not parseable, so not a batch; DuckDB explains it below
        return False
    if len(statements) < 2:
        return False
    return all(is_read_only_query(statement.query) for statement in statements)


def _short_sql(sql: str) -> str:
    """Single-line, length-bounded rendering of a query for log lines."""
    collapsed = " ".join(sql.split())
    return (
        collapsed
        if len(collapsed) <= LOG_SQL_LIMIT
        else collapsed[:LOG_SQL_LIMIT] + "..."
    )


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
        # The subset a call that passes no ``sf`` reads: the smallest fast-check rung, which is the
        # cheapest representative one. A query that needs benchmark-scale row counts can ask for a
        # bigger subset explicitly; the prompts steer it to the smallest one that suffices.
        self.sf: float = (
            sf if sf is not None else self._default_inspect_sf(workload_provider)
        )
        # One connection per inspected subset, opened on first use and reused after (the agent
        # typically hops between the small subset and the benchmark one).
        self._cons: dict[float, duckdb.DuckDBPyConnection] = {}

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

    def available_subsets(self) -> list[float]:
        """The subsets the agent may query: every one fully materialized on disk, ascending.

        Read off the filesystem (after ``prepare()``, which downscales a BYO workload's fractional
        rungs) rather than off the spec's SF ladders, because nothing guarantees a spec rung exists
        - a built-in workload's ``sf<N>`` dirs come from an out-of-band dbgen step the user runs."""
        self.workload_provider.prepare()
        return available_subsets(
            self.spec, Path(self.workload_provider.base_parquet_dir)
        )

    def _smaller_subset_hint(self, sf: float) -> str:
        """The retry-on-something-cheaper half of the timeout message. A query that overran on a
        big subset is often fine on a small one, and the distributions it is after carry over -
        so name the cheaper subsets explicitly rather than only telling it to simplify the SQL.
        Empty when it already ran on the smallest subset (there is nothing cheaper to suggest)."""
        smaller = [value for value in self.available_subsets() if value < sf]
        if not smaller:
            return ""
        listed = ", ".join(f"{value:g}" for value in smaller)
        return (
            f" You ran this on subset {sf:g}; re-running it on a smaller subset ({listed}) is "
            "usually enough, since distributions and value ranges carry over."
        )

    def _resolve_subset_dir(self, sf: float) -> Path:
        # Fractional subsets are downscaled lazily; make sure the one we need exists. Idempotent
        # and cheap once present, and a no-op for built-ins / plain BYO-parquet.
        self.workload_provider.prepare()
        base = Path(self.workload_provider.base_parquet_dir)
        subset_dir = find_sf_dir(base, sf)
        if subset_dir is None:
            raise FileNotFoundError(
                f"No subset directory for fraction/SF {sf:g} under {base}."
            )
        return subset_dir

    def _connect(self, sf: float) -> duckdb.DuckDBPyConnection:
        con = self._cons.get(sf)
        if con is not None:
            return con
        subset_dir = self._resolve_subset_dir(sf)
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
                parquet = (
                    (subset_dir / f"{table}.parquet").as_posix().replace("'", "''")
                )
                con.execute(
                    f"CREATE VIEW \"{table}\" AS SELECT * FROM read_parquet('{parquet}')"
                )
        self._cons[sf] = con
        return con

    def _cache_key(self, sql: str, row_limit: int, sf: float) -> tuple[str, str]:
        """Build the (payload, hash) the result is cached under. The payload pins the query text,
        its row cap, and the identity of the inspected subset - workload, scale factor, and dataset
        version - so an unrelated workload or a regenerated dataset can never collide with a stale
        entry, while re-running the same query against the same read-only subset always hits."""
        payload = {
            "sql": sql,
            "row_limit": row_limit,
            "sf": sf,
            "workload": getattr(self.spec, "name", None),
            "dataset_name": getattr(self.spec, "dataset_name", None),
            "dataset_version": getattr(self.spec, "dataset_version", None),
            "serve_from": getattr(
                self.spec.serve_from, "value", str(self.spec.serve_from)
            ),
        }
        hash_payload = utils.stable_json(payload)
        return hash_payload, utils.sha256(hash_payload)

    def __call__(
        self, sql: str, max_rows: int | None = None, sf: float | None = None
    ) -> str:
        row_limit = (
            DEFAULT_MAX_ROWS
            if max_rows is None
            else max(1, min(int(max_rows), MAX_ROWS_CAP))
        )
        sql = (sql or "").strip()
        subset = _canon_sf(self.sf if sf is None else sf)
        output, status, cached = self._inspect(sql, row_limit, subset)
        self._report(sql, row_limit, subset, output, status, cached)
        return output

    def _inspect(self, sql: str, row_limit: int, sf: float) -> tuple[str, str, bool]:
        """Resolve one inspection to ``(output_text, status, served_from_cache)``. ``status`` is a
        short label (``ok`` / ``sql_error`` / ``timeout`` / ``rejected`` / ``bad_subset`` /
        ``empty`` / ``prep_error``) that drives the dashboard row and activity-summary line; the
        rendered text is what the agent sees. Every outcome returns here (no bare returns) so
        ``__call__`` reports exactly once."""
        if not sql:
            return "Error: empty SQL query.", "empty", False
        # A batch of read-only statements is refused for what it is - one statement per call - and
        # not as a write. The read-only classifier only passes a multi-statement batch when every
        # statement is a plain SELECT/WITH, so "SUMMARIZE a; SUMMARIZE b" would otherwise be
        # rejected with "this statement would write or change state", which is simply untrue: an
        # agent that trusts it goes looking for a write it never made. A batch containing an actual
        # write still falls through to the read-only gate below and is reported as a write.
        if _is_read_only_batch(sql):
            logger.debug(
                "query_data rejected a multi-statement batch: %s", _short_sql(sql)
            )
            return (
                "Error: query_data runs one statement per call, and this is a batch of several. "
                "Send them as separate query_data calls (the results come back one per call).",
                "multi_statement",
                False,
            )
        if not is_read_only_query(sql):
            logger.debug("query_data rejected non-read-only SQL: %s", _short_sql(sql))
            return (
                "Error: query_data is strictly read-only. Only SELECT / WITH / EXPLAIN / SHOW / "
                "DESCRIBE / SUMMARIZE / VALUES and read-only PRAGMA statements are allowed; this "
                "statement would write or change state.",
                "rejected",
                False,
            )

        # Cache lookup deliberately precedes any disk check on the subset: an ``only_from_cache``
        # replay runs with the data gone, so it must not depend on the subset still existing.
        hash_payload, cache_path = "", None
        if self.cache_dir is not None:
            hash_payload, hash = self._cache_key(sql, row_limit, sf)
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

        # About to actually run, so the subset must exist. Only now is it safe to touch the disk.
        # Catches the default too: the spec's smallest rung is not guaranteed to be materialized
        # (built-in sf<N> dirs come from an out-of-band dbgen step), and a listing of what *is*
        # there beats the bare FileNotFoundError the connect below would otherwise raise.
        available = self.available_subsets()
        if available and sf not in available:
            logger.debug("query_data rejected unknown subset sf=%g", sf)
            # Only offer the omit-sf route when the default is actually on disk, or the agent
            # would be sent straight back into this same error.
            fallback = (
                f", or omit sf to use the default ({self.sf:g})"
                if self.sf in available
                else ""
            )
            return (
                f"Error: no data subset {sf:g}. Available subsets: "
                f"{', '.join(f'{value:g}' for value in available)}. "
                f"Pass one of these as sf{fallback}.",
                "bad_subset",
                False,
            )

        try:
            con = self._connect(sf)
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the agent as text
            logger.warning("query_data could not prepare data (sf=%g): %s", sf, exc)
            return f"Error preparing data for inspection: {exc}", "prep_error", False

        output, elapsed = self._run_and_render(con, sql, row_limit, sf)
        # Cache every executed outcome - a result set, a SQL error, or a timeout alike. Both
        # successful and unsuccessful executions are keyed on the query and the read-only subset,
        # so a repeated inspection replays from disk and ``only_from_cache`` never misses on a
        # query the recording run actually ran.
        if cache_path is not None and not self.do_not_cache:
            utils.dump_pickle(
                cache_path,
                DataInspectCacheType(
                    output=output, hash_payload=hash_payload, runtime_seconds=elapsed
                ),
                do_not_cache=self.do_not_cache,
            )
        return output, _status_from_output(output), False

    def _report(
        self,
        sql: str,
        row_limit: int,
        sf: float,
        output: str,
        status: str,
        cached: bool,
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
                "data_inspect/sf": sf,
                "data_inspect/status": status,
                "data_inspect/error": is_error,
                "data_inspect/cached": cached,
                "data_inspect/output": output[:20000],
                "data_inspect/truncated": len(output) > 20000,
            },
            log_and_increment=True,
        )
        self.run_stats_collector.add_to_activity_summary(
            f"Data Inspect Tool called: {status}"
        )

    def _run_and_render(
        self, con: duckdb.DuckDBPyConnection, sql: str, row_limit: int, sf: float
    ) -> tuple[str, float]:
        """Execute *sql* on a throwaway cursor under the wall-clock budget and render the result.

        Returns ``(output_text, elapsed_seconds)``. Interrupting the cursor (never the cached
        connection) means a late interrupt that races query completion cannot poison a later
        inspection; ``Timer.cancel()`` is a no-op once the timer has already fired. A result set,
        a SQL error, and a timeout are all returned as rendered text and cached by the caller; the
        outcome is recovered from that text by :func:`_status_from_output`."""
        cur = con.cursor()
        timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
        timer.daemon = True
        timer.start()
        logger.debug("query_data (sf=%g): %s", sf, _short_sql(sql))
        started = time.perf_counter()
        try:
            cur.execute(sql)
            rows = cur.fetchmany(row_limit + 1)
            column_names = [d[0] for d in cur.description] if cur.description else []
        except duckdb.InterruptException:
            elapsed = time.perf_counter() - started
            logger.warning(
                "query_data exceeded the %gs budget on subset %g, cancelled after %.1fs: %s",
                QUERY_TIMEOUT_S,
                sf,
                elapsed,
                _short_sql(sql),
            )
            return (
                f"{_TIMEOUT_ERROR_PREFIX} {QUERY_TIMEOUT_S:g}s inspection budget on subset "
                f"{sf:g} and was cancelled. query_data is for simple, cheap look-ups - narrow it "
                "with a WHERE filter, add LIMIT, aggregate fewer columns, or use "
                "SUMMARIZE/DESCRIBE on a single table instead of scanning or joining large "
                f"tables.{self._smaller_subset_hint(sf)}",
                elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - return DuckDB's message so the agent can fix it
            elapsed = time.perf_counter() - started
            logger.debug("query_data SQL error after %.2fs: %s", elapsed, exc)
            return f"SQL error: {exc}", elapsed
        finally:
            timer.cancel()
        elapsed = time.perf_counter() - started
        logger.debug(
            "query_data ok in %.2fs: %d row(s), %d column(s)",
            elapsed,
            len(rows),
            len(column_names),
        )
        return _render_result(column_names, rows, row_limit), elapsed


def _status_from_output(output: str) -> str:
    """Classify a rendered inspection result for reporting. A DuckDB error is echoed verbatim as
    ``SQL error: ...``; a wall-clock timeout starts with :data:`_TIMEOUT_ERROR_PREFIX`; everything
    else is a successful result set. Driven purely by the rendered text so a cached outcome - a
    result set, a SQL error, or a timeout - classifies identically whether run live or replayed."""
    if output.startswith("SQL error:"):
        return "sql_error"
    if output.startswith(_TIMEOUT_ERROR_PREFIX):
        return "timeout"
    return "ok"


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
