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
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from synnodb.router.normalize import is_read_only_query
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


class DataInspectTool:
    """Runs a strictly read-only SQL query against the workload's benchmark-scale data."""

    def __init__(self, workload_provider: Any, sf: float | None = None):
        self.workload_provider = workload_provider
        self.spec = workload_provider.spec
        # Inspect the smallest representative subset by default (the smallest fast-check rung, which
        # prepare() always materializes). This tool is for cheap look-ups, not benchmark-scale
        # measurement: value domains and distributions carry over from the small subset, while a
        # full scan touches a fraction of the rows - so an unindexed aggregate stays sub-second
        # instead of chewing every core on tens of millions of rows.
        self.sf: float = sf if sf is not None else self._default_inspect_sf(workload_provider)
        self._con: duckdb.DuckDBPyConnection | None = None

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
                f"No subset directory for scale factor {self.sf:g} under {base}."
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

    def __call__(self, sql: str, max_rows: int | None = None) -> str:
        row_limit = (
            DEFAULT_MAX_ROWS
            if max_rows is None
            else max(1, min(int(max_rows), MAX_ROWS_CAP))
        )
        sql = (sql or "").strip()
        if not sql:
            return "Error: empty SQL query."
        if not is_read_only_query(sql):
            logger.debug("query_data rejected non-read-only SQL: %s", _short_sql(sql))
            return (
                "Error: query_data is strictly read-only. Only SELECT / WITH / EXPLAIN / SHOW / "
                "DESCRIBE / SUMMARIZE / VALUES and read-only PRAGMA statements are allowed; this "
                "statement would write or change state."
            )
        try:
            con = self._connect()
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the agent as text
            logger.warning("query_data could not prepare data (sf=%g): %s", self.sf, exc)
            return f"Error preparing data for inspection: {exc}"
        # Run on a throwaway cursor under a wall-clock budget: a timer interrupts the cursor if the
        # query overruns. Interrupting the cursor (never the cached connection) means a late
        # interrupt that races query completion cannot poison a later inspection; Timer.cancel()
        # is a no-op once the timer has already fired.
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
            logger.warning(
                "query_data exceeded the %gs budget, cancelled after %.1fs: %s",
                QUERY_TIMEOUT_S,
                time.perf_counter() - started,
                _short_sql(sql),
            )
            return (
                f"Error: query exceeded the {QUERY_TIMEOUT_S:g}s inspection budget and was "
                "cancelled. query_data is for simple, cheap look-ups - narrow it with a WHERE "
                "filter, add LIMIT, aggregate fewer columns, or use SUMMARIZE/DESCRIBE on a single "
                "table instead of scanning or joining large tables."
            )
        except Exception as exc:  # noqa: BLE001 - return DuckDB's message so the agent can fix it
            logger.info(
                "query_data SQL error after %.2fs: %s", time.perf_counter() - started, exc
            )
            return f"SQL error: {exc}"
        finally:
            timer.cancel()
        logger.debug(
            "query_data ok in %.2fs: %d row(s), %d column(s)",
            time.perf_counter() - started,
            len(rows),
            len(column_names),
        )
        return _render_result(column_names, rows, row_limit)


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
