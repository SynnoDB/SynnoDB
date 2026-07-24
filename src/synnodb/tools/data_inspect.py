"""A read-only SQL window into the actual benchmark data - for quick, cheap look-ups.

The agent designs a bespoke engine for a dataset it otherwise only sees as schema DDL.
``DataInspectTool`` gives it a DBA-style view of the *content*: cardinalities, value
distributions, null density, min/max ranges, distinct counts, join fan-out - the shape
that drives physical-design choices (element types, encodings, partitioning, join order).

It is a lightweight replacement for parquet-dump shell commands, meant for *simple* queries -
peeking at rows, ranges, and per-column stats. The agent chooses *what* each query reads with one
boolean, ``full_dataset``: the cheap **sample** (the default) or the **full dataset** - the scale
its design must actually serve. The workload's subset ladder can hold several rungs, but the agent
is not asked to reason about them; the tool maps the boolean onto two of them (the smallest
fast-check rung, and the benchmark subset). Both are spec-derived rather than read off the disk,
because the resolved subset is part of the cache key - a disk-dependent choice would resolve
differently on an ``only_from_cache`` replay (which runs with the data gone) than on the recording
run. Distribution shape carries over from the sample; row counts, min/max and distinct counts do
not, which is why the menu sends the agent to the full dataset for any number it sizes a design
from. Two guards keep a stray heavy query from stalling the whole conversation: every statement
runs under a wall-clock budget (:data:`QUERY_TIMEOUT_S`, the same for both datasets) and is
interrupted if it overruns - the message then offers the sample as a cheaper retry - and it runs on
an isolated cursor so that interrupt never touches the cached connection.

It is strictly read-only: every statement is gated by the same read-only classifier the
DuckDB-compat write guard uses, and DuckDB-native subsets are additionally opened
``read_only=True``. No statement can mutate the source data or leave scratch state behind.

Results are cached on disk keyed by the query, its row cap, and the identity of the inspected
subset (workload, scale factor, dataset version). The subset is read-only benchmark data that
never changes within a run, so a repeated inspection replays instantly - and, under
``only_from_cache``, a whole run replays deterministically without re-touching the data. This
mirrors the shell / compile / apply-patch tool caches. Every outcome the tool itself decides is
cached - a result set, a SQL error, a timeout, and the gate rejections (empty SQL, a
multi-statement batch, a write attempt) alike - so on replay the recorded verdict, not the
current code, decides what the agent sees (the ordering note in ``_inspect`` explains why). Only
outcomes tied to mutable disk state (an unmaterialized subset, a connection failure) stay
uncached.
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
# Leading text of the timeout message. Cache entries written before entries stored their status
# are classified from their rendered text, and this prefix is how such an entry is recognized as
# a timeout (see :func:`_status_from_output`).
_TIMEOUT_ERROR_PREFIX = "Error: query exceeded the"


def subset_menu_for(workload_provider: Any) -> str:
    """The ``query_data`` sample-vs-full-dataset note for a workload, for the planner /
    storage-plan prompts.

    The same text the tool description carries, built here so the prompts and the tool cannot
    disagree about which datasets exist or which one the boolean reaches. Empty when the workload
    cannot back the tool at all (the same guard ``main`` uses to decide whether to build it) or when
    nothing is materialized on disk - the prompts then simply say nothing about the data."""
    if not hasattr(workload_provider, "spec") or not hasattr(
        workload_provider, "benchmark_sf"
    ):
        return ""
    # Same order as the tool: prepare() first (it downscales a BYO workload's fractional rungs),
    # so the prompt cannot advertise data that differs from what the tool will honour.
    workload_provider.prepare()
    available = available_subsets(
        workload_provider.spec, Path(workload_provider.base_parquet_dir)
    )
    return format_subset_menu(
        available=available,
        sample_sf=_canon_sf(DataInspectTool._default_inspect_sf(workload_provider)),
        full_sf=_canon_sf(workload_provider.benchmark_sf),
    )


def _canon_sf(value: float) -> float:
    """One spelling per subset: integral values as ints (``1``, not ``1.0``), matching how the
    spec's SF ladders and :func:`available_subsets` spell them. The resolved subset is part of the
    cache key, so without this a workload spelling its benchmark scale ``1.0`` and one spelling it
    ``1`` would cache the identical inspection twice and re-run it."""
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
    """One cached inspection: the rendered output text, its status label (stored so a replay
    reports the recorded verdict even if the wording or the gate logic changes later, like the
    apply-patch cache's ``result_status``), the payload it was keyed on (for debugging cache hits)
    and the wall-clock it originally took (credited back to the runtime tracker as skipped time on
    a cache hit, exactly like the shell tool)."""

    def __init__(
        self, output: str, hash_payload: str, runtime_seconds: float, status: str
    ):
        self.output = output
        self.hash_payload = hash_payload
        self.runtime_seconds = runtime_seconds
        self.status = status


class DataInspectTool:
    """Runs a strictly read-only SQL query against the workload's benchmark-scale data."""

    def __init__(
        self,
        workload_provider: Any,
        sample_sf: float | None = None,
        cache_dir: Path | None = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        runtime_tracker: RuntimeTracker | None = None,
        run_stats_collector: RunStatsCollector | None = None,
    ):
        self.workload_provider = workload_provider
        self.spec = workload_provider.spec
        # The two subsets the agent's ``full_dataset`` boolean picks between. The sample is the
        # workload's smallest fast-check rung (the cheapest representative one); the full dataset is
        # the benchmark subset - the scale the design must actually serve. A workload with no
        # fast-check ladder collapses the two, and the menu then says the flag is a no-op.
        self.sample_sf: float = _canon_sf(
            sample_sf
            if sample_sf is not None
            else self._default_inspect_sf(workload_provider)
        )
        self.full_sf: float = _canon_sf(workload_provider.benchmark_sf)
        # One connection per inspected subset, opened on first use and reused after (the agent
        # typically hops between the sample and the full dataset).
        self._cons: dict[float, duckdb.DuckDBPyConnection] = {}

        # Disk cache, keyed by query + row cap + subset identity. Disabled (None) when no cache
        # dir is supplied, so the tool still works standalone. do_not_cache runs live but never
        # writes; only_from_cache reads an existing cache without creating/chmodding it and refuses
        # any live data access (deterministic replay).
        self.cache_dir = cache_dir
        self.do_not_cache = do_not_cache
        self.only_from_cache = only_from_cache
        self.runtime_tracker = runtime_tracker
        # Report every inspection to the live dashboard / supervisor activity log, exactly like the
        # shell / compile / run tools. Optional so the tool still works standalone (e.g. in tests).
        self.run_stats_collector = run_stats_collector
        if self._cache_writes_enabled:
            utils.create_dir_and_set_permissions(self.cache_dir)

    @property
    def _cache_writes_enabled(self) -> bool:
        """Whether this instance may create or add to the cache.

        Keep directory setup and pickle writes behind the same predicate so replay cannot mutate
        cache metadata during construction and then appear read-only at the call site."""
        return (
            self.cache_dir is not None
            and not self.do_not_cache
            and not self.only_from_cache
        )

    @staticmethod
    def _default_inspect_sf(workload_provider: Any) -> float:
        """The cheapest subset to inspect: the smallest fast-check rung, or the benchmark SF when a
        workload defines no fast-check ladder. Both are guaranteed on disk after ``prepare()``."""
        fast_check_sfs = workload_provider.spec.fast_check_sfs
        return min(fast_check_sfs) if fast_check_sfs else workload_provider.benchmark_sf

    def available_subsets(self) -> list[float]:
        """The subsets fully materialized on disk, ascending. The agent never sees this list - it
        only picks sample-or-full - but the tool checks the subset it resolved to against it.

        Read off the filesystem (after ``prepare()``, which downscales a BYO workload's fractional
        rungs) rather than off the spec's SF ladders, because nothing guarantees a spec rung exists
        - a built-in workload's ``sf<N>`` dirs come from an out-of-band dbgen step the user runs."""
        self.workload_provider.prepare()
        return available_subsets(
            self.spec, Path(self.workload_provider.base_parquet_dir)
        )

    def _subset_for(self, full_dataset: bool) -> float:
        """The subset one call reads. Spec-derived on both branches, so an ``only_from_cache``
        replay (which runs with the data gone) resolves the same cache key as the recording run."""
        return self.full_sf if full_dataset else self.sample_sf

    def _sample_retry_hint(self, full_dataset: bool) -> str:
        """The retry-on-something-cheaper half of the timeout message. A query that overran on the
        full dataset is often fine on the sample, and the distribution shape it is usually after
        carries over - so offer that explicitly rather than only telling it to simplify the SQL.
        Empty when it already ran on the sample, or when no sample is materialized: there is then
        nothing cheaper to suggest. Deliberately does not claim value ranges carry over - they do
        not, and the menu says so."""
        if not full_dataset or self.sample_sf == self.full_sf:
            return ""
        if self.sample_sf not in self.available_subsets():
            return ""
        return (
            " You ran this on the full dataset; re-running it on the sample (omit `full_dataset`) "
            "is far cheaper, and is usually enough for distribution shape."
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
        self, sql: str, max_rows: int | None = None, full_dataset: bool = False
    ) -> str:
        row_limit = (
            DEFAULT_MAX_ROWS
            if max_rows is None
            else max(1, min(int(max_rows), MAX_ROWS_CAP))
        )
        sql = (sql or "").strip()
        full_dataset = bool(full_dataset)
        subset = self._subset_for(full_dataset)
        output, status, cached = self._inspect(sql, row_limit, subset, full_dataset)
        self._report(sql, row_limit, subset, full_dataset, output, status, cached)
        return output

    def _inspect(
        self, sql: str, row_limit: int, sf: float, full_dataset: bool
    ) -> tuple[str, str, bool]:
        """Resolve one inspection to ``(output_text, status, served_from_cache)``. ``status`` is a
        short label (``ok`` / ``sql_error`` / ``timeout`` / ``rejected`` / ``multi_statement`` /
        ``bad_subset`` / ``empty`` / ``prep_error``) that drives the dashboard row and
        activity-summary line; the rendered text is what the agent sees. Every outcome returns
        here (no bare returns) so ``__call__`` reports exactly once."""
        # The cache lookup comes first - before the gates and before any disk check. Before the
        # disk check because an ``only_from_cache`` replay runs with the data gone, so it must not
        # depend on the subset still existing; before the gates because a gate verdict, though a
        # pure function of the SQL text, is a function of the *current* code - looked up second, a
        # later change to the gate logic or its wording would rewrite what a recorded conversation
        # saw when it is replayed.
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
                # Entries written before the status field existed carry only the text.
                status = getattr(cached, "status", None) or _status_from_output(
                    cached.output
                )
                return cached.output, status, True

        # Strict replay without a configured cache cannot be honoured. In particular, do not let
        # the missing cache_path bypass the miss check below and turn only_from_cache into a live
        # inspection mode.
        if self.only_from_cache and cache_path is None:
            raise ValueError(
                "query_data cannot honor only_from_cache because no cache_dir is configured."
            )

        # The gates: outcomes decided from the SQL text alone, never touching the data. Cached
        # like every other decided outcome (at zero runtime), and kept ahead of the
        # only_from_cache miss-check so a recording made before rejections were cached still
        # replays - the gate then re-derives the text live instead of raising. _finish keeps that
        # compatibility fallback read-only, so it cannot backfill or alter the recorded cache.
        if not sql:
            return self._finish(
                "Error: empty SQL query.", "empty", 0.0, cache_path, hash_payload
            )
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
            return self._finish(
                "Error: query_data runs one statement per call, and this is a batch of several. "
                "Send them as separate query_data calls (the results come back one per call).",
                "multi_statement",
                0.0,
                cache_path,
                hash_payload,
            )
        if not is_read_only_query(sql):
            logger.debug("query_data rejected non-read-only SQL: %s", _short_sql(sql))
            return self._finish(
                "Error: query_data is strictly read-only. Only SELECT / WITH / EXPLAIN / SHOW / "
                "DESCRIBE / SUMMARIZE / VALUES and read-only PRAGMA statements are allowed; this "
                "statement would write or change state.",
                "rejected",
                0.0,
                cache_path,
                hash_payload,
            )

        if cache_path is not None and self.only_from_cache:
            raise ValueError(
                "query_data result not found in cache and only_from_cache is enabled. "
                f"Cache path: {cache_path}\nPayload: {hash_payload}"
            )

        # About to actually run, so the subset must exist. Only now is it safe to touch the disk.
        # Neither dataset is guaranteed to be materialized (built-in sf<N> dirs come from an
        # out-of-band dbgen step), and telling the agent to flip the boolean beats the bare
        # FileNotFoundError the connect below would otherwise raise.
        available = self.available_subsets()
        if available and sf not in available:
            logger.debug(
                "query_data rejected an unmaterialized subset sf=%g (full_dataset=%s)",
                sf,
                full_dataset,
            )
            return (
                self._missing_dataset_message(full_dataset, available),
                "bad_subset",
                False,
            )

        try:
            con = self._connect(sf)
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the agent as text
            logger.warning("query_data could not prepare data (sf=%g): %s", sf, exc)
            return f"Error preparing data for inspection: {exc}", "prep_error", False

        output, status, elapsed = self._run_and_render(
            con, sql, row_limit, full_dataset
        )
        return self._finish(output, status, elapsed, cache_path, hash_payload)

    def _finish(
        self,
        output: str,
        status: str,
        elapsed: float,
        cache_path: Path | None,
        hash_payload: str,
    ) -> tuple[str, str, bool]:
        """Cache one live outcome - a result set, a SQL error, a timeout, or a gate rejection
        alike - and hand it back. The status is stored next to the text, so a replay reports the
        recorded verdict as-is."""
        if cache_path is not None and self._cache_writes_enabled:
            utils.dump_pickle(
                cache_path,
                DataInspectCacheType(
                    output=output,
                    hash_payload=hash_payload,
                    runtime_seconds=elapsed,
                    status=status,
                ),
                do_not_cache=self.do_not_cache,
            )
        return output, status, False

    def _missing_dataset_message(
        self, full_dataset: bool, available: list[float]
    ) -> str:
        """The dataset the call asked for is not on disk. Point the agent at the other one when
        that one *is* there (flipping the boolean is the whole fix), and otherwise say plainly that
        there is nothing to read rather than sending it round the same failure again."""
        requested, other = (
            ("full dataset", "sample") if full_dataset else ("sample", "full dataset")
        )
        other_sf = self.sample_sf if full_dataset else self.full_sf
        if other_sf == self._subset_for(full_dataset) or other_sf not in available:
            return (
                f"Error: the {requested} is not materialized on disk, and neither is the "
                f"{other} - query_data has no data to read for this workload."
            )
        flag = "false" if full_dataset else "true"
        return (
            f"Error: the {requested} is not materialized on disk. Re-run this query with "
            f"`full_dataset={flag}` to read the {other} instead."
        )

    def _report(
        self,
        sql: str,
        row_limit: int,
        sf: float,
        full_dataset: bool,
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
                "data_inspect/full_dataset": full_dataset,
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
        self,
        con: duckdb.DuckDBPyConnection,
        sql: str,
        row_limit: int,
        full_dataset: bool,
    ) -> tuple[str, str, float]:
        """Execute *sql* on a throwaway cursor under the wall-clock budget and render the result.

        Returns ``(output_text, status, elapsed_seconds)``. Interrupting the cursor (never the
        cached connection) means a late interrupt that races query completion cannot poison a
        later inspection; ``Timer.cancel()`` is a no-op once the timer has already fired. A result
        set, a SQL error, and a timeout are all returned as rendered text and cached by the
        caller."""
        dataset = "the full dataset" if full_dataset else "the sample"
        cur = con.cursor()
        timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
        timer.daemon = True
        timer.start()
        logger.debug("query_data (%s): %s", dataset, _short_sql(sql))
        started = time.perf_counter()
        try:
            cur.execute(sql)
            rows = cur.fetchmany(row_limit + 1)
            column_names = [d[0] for d in cur.description] if cur.description else []
        except duckdb.InterruptException:
            elapsed = time.perf_counter() - started
            logger.warning(
                "query_data exceeded the %gs budget on %s, cancelled after %.1fs: %s",
                QUERY_TIMEOUT_S,
                dataset,
                elapsed,
                _short_sql(sql),
            )
            return (
                f"{_TIMEOUT_ERROR_PREFIX} {QUERY_TIMEOUT_S:g}s inspection budget on {dataset} "
                "and was cancelled. query_data is for simple, cheap look-ups - narrow it "
                "with a WHERE filter, add LIMIT, aggregate fewer columns, or use "
                "SUMMARIZE/DESCRIBE on a single table instead of scanning or joining large "
                f"tables.{self._sample_retry_hint(full_dataset)}",
                "timeout",
                elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - return DuckDB's message so the agent can fix it
            elapsed = time.perf_counter() - started
            logger.debug("query_data SQL error after %.2fs: %s", elapsed, exc)
            return f"SQL error: {exc}", "sql_error", elapsed
        finally:
            timer.cancel()
        elapsed = time.perf_counter() - started
        logger.debug(
            "query_data ok in %.2fs: %d row(s), %d column(s)",
            elapsed,
            len(rows),
            len(column_names),
        )
        return _render_result(column_names, rows, row_limit), "ok", elapsed


def _status_from_output(output: str) -> str:
    """Classify a cache entry written before entries stored their status. Such an entry can only
    hold an executed outcome - gate rejections were not cached back then - so recognizing DuckDB's
    verbatim ``SQL error: ...`` echo and the timeout prefix is enough; everything else was a
    result set. New entries carry their status and never come through here."""
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
