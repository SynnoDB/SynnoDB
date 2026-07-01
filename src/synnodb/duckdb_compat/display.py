"""Human-readable result display: a compact ASCII table plus a query-time / speedup footer, and a
performance-safe loading spinner.

Design constraint (load-bearing): none of this may cost anything on the hot path. The spinner is
**interactive-only** - off a TTY (scripts, pipes, benchmarks) ``Spinner.for_stream`` returns a
no-op, so ``execute`` runs byte-for-byte as before. Timing is read from the route trace the router
already produced, so there is no extra measurement, and rendering happens only when a human asks
for it (``repr``/``show``), never during execution.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any, List, Optional

import pyarrow as pa


@dataclass
class QueryTiming:
    """What the last query did, for the footer. Times are milliseconds."""

    served_by: str  # "engine" | "duckdb"
    engine_ms: Optional[float] = None
    duckdb_ms: Optional[float] = (
        None  # measured this query (cross-check or fallback run)
    )
    duckdb_ms_estimated: Optional[float] = (
        None  # from a prior cross-check of the same template
    )
    rows: int = 0


# ---- table rendering -------------------------------------------------------
def _short_type(t: "pa.DataType") -> str:
    s = str(t)
    return s.replace("timestamp[us]", "timestamp").replace("large_string", "string")


def _cell(v: Any, max_w: int) -> str:
    if v is None:
        return "NULL"
    s = str(v)
    return s if len(s) <= max_w else s[: max_w - 1] + "…"


def render_table(
    table: pa.Table, *, max_rows: int = 20, max_col_width: int = 40
) -> str:
    """A compact box-drawn table: column names, dtypes, the first *max_rows* rows, and a truncation
    note. Mirrors DuckDB's interactive feel without depending on it."""
    ncols = table.num_columns
    if ncols == 0:
        return "(0 columns)"
    names = list(table.column_names)
    types = [_short_type(table.schema.field(i).type) for i in range(ncols)]
    nrows = table.num_rows
    shown = min(nrows, max_rows)
    cols = [table.column(i).slice(0, shown).to_pylist() for i in range(ncols)]
    body = [
        [_cell(cols[c][r], max_col_width) for c in range(ncols)] for r in range(shown)
    ]

    widths = []
    for c in range(ncols):
        w = max(len(names[c]), len(types[c]))
        for r in range(shown):
            w = max(w, len(body[r][c]))
        widths.append(min(w, max_col_width))

    def row(cells: List[str], align_center: bool = False) -> str:
        out = []
        for c in range(ncols):
            cell = cells[c]
            out.append(
                cell.center(widths[c]) if align_center else cell.ljust(widths[c])
            )
        return "│ " + " │ ".join(out) + " │"

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (widths[c] + 2) for c in range(ncols)) + right

    lines = [
        rule("┌", "┬", "┐"),
        row(names, align_center=True),
        row(types, align_center=True),
        rule("├", "┼", "┤"),
        *(row(body[r]) for r in range(shown)),
        rule("└", "┴", "┘"),
    ]
    if nrows > shown:
        lines.append(
            f"  … {nrows - shown} more row(s) ({nrows} total, {ncols} column(s))"
        )
    elif nrows == 0:
        lines.append(f"  (0 rows, {ncols} column(s))")
    return "\n".join(lines)


# ---- timing / speedup footer ----------------------------------------------
def speed_badge(speedup: float) -> str:
    """An emoji for how much faster the engine was than DuckDB."""
    if speedup >= 50:
        return "\U0001f525"  # fire
    if speedup >= 10:
        return "\U0001f680"  # rocket
    if speedup >= 3:
        return "⚡"  # high voltage
    if speedup >= 1.2:
        return "\U0001f642"  # slightly smiling
    if speedup >= 0.9:
        return "➖"  # heavy minus (about the same)
    return "\U0001f422"  # turtle (slower)


def format_footer(t: QueryTiming) -> str:
    if t.served_by == "engine":
        head = f"⚡ synno engine · {t.engine_ms:.1f} ms"
        if t.duckdb_ms is not None and t.engine_ms and t.engine_ms > 0:
            sp = t.duckdb_ms / t.engine_ms
            return (
                f"{head} · {sp:.1f}× vs DuckDB ({t.duckdb_ms:.1f} ms) {speed_badge(sp)}"
            )
        if t.duckdb_ms_estimated is not None and t.engine_ms and t.engine_ms > 0:
            sp = t.duckdb_ms_estimated / t.engine_ms
            return f"{head} · ~{sp:.1f}× vs DuckDB {speed_badge(sp)} (est.)"
        return f"{head} · routed (cross-check sampled)"
    dt = f"{t.duckdb_ms:.1f} ms" if t.duckdb_ms is not None else "?"
    return f"\U0001f986 DuckDB · {dt}"


# ---- performance-safe spinner ---------------------------------------------
class _NullSpinner:
    """A no-op spinner for non-interactive streams: zero threads, zero overhead."""

    def __enter__(self) -> "_NullSpinner":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class Spinner:
    """A lazy, interactive-only spinner. It runs on a daemon thread that first waits ``delay`` -
    so a fast query finishes and stops it before anything is drawn - and otherwise sleeps between
    frames, so it cannot steal CPU from the query (whose heavy work runs with the GIL released in
    the engine subprocess / DuckDB)."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(
        self,
        stream: Any,
        *,
        delay: float = 0.15,
        interval: float = 0.08,
        message: str = "running query",
    ) -> None:
        self._stream = stream
        self._delay = delay
        self._interval = interval
        self._message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._drew = False

    @classmethod
    def for_stream(cls, stream: Any = None, **kwargs: Any):
        """A real spinner only for an interactive TTY; otherwise a no-op (the hot path pays
        nothing)."""
        stream = stream if stream is not None else sys.stderr
        try:
            interactive = bool(getattr(stream, "isatty", None) and stream.isatty())
        except Exception:
            interactive = False
        return cls(stream, **kwargs) if interactive else _NullSpinner()

    def __enter__(self) -> "Spinner":
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._drew:
            try:
                self._stream.write("\r\033[K")  # erase the spinner line
                self._stream.flush()
            except Exception:
                pass

    def _spin(self) -> None:
        if self._stop.wait(self._delay):
            return  # the query already finished; never draw for a fast query
        i = 0
        while not self._stop.is_set():
            try:
                self._stream.write(
                    f"\r{self.FRAMES[i % len(self.FRAMES)]} {self._message}…"
                )
                self._stream.flush()
                self._drew = True
            except Exception:
                return
            i += 1
            if self._stop.wait(self._interval):
                return
