"""Move data in and out of a real DuckDB connection.

These helpers operate on the underlying ``DuckDBPyConnection`` (the ``.duckdb`` escape
hatch), so they are unaffected by the write block on the SynnoDB surface. They back two
things: loading an existing ``.db`` file into an in-memory connection (used by
``optimize_database``, and available for an explicit RAM-resident copy), and exporting a
connection's tables to a parquet snapshot (the self-contained, portable parquet plane / ETL
substrate).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Sequence

log = logging.getLogger("synnodb.duckdb_compat.db_io")


def _sql_str(value: Any) -> str:
    """A single-quoted SQL string literal (DuckDB has no bind parameter for ATTACH/COPY paths)."""
    return "'" + str(value).replace("'", "''") + "'"


def list_tables(inner: Any, *, schema: str = "main") -> List[str]:
    """The base tables in *schema* on the connection (views excluded)."""
    rows = inner.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ? AND table_type = 'BASE TABLE' ORDER BY table_name",
        [schema],
    ).fetchall()
    return [r[0] for r in rows]


def load_database_into_memory(
    inner: Any, db_path: "str | Path", *, alias: str = "_synno_src"
) -> List[str]:
    """Copy every base table of the DuckDB file *db_path* into *inner*'s in-memory catalog.

    Attaches the file read-only, materializes each ``main`` base table as an in-memory table,
    then detaches - so the connection holds the data in RAM and the file is left untouched.
    Returns the table names loaded.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB database not found: {db_path}")
    inner.execute(f"ATTACH {_sql_str(db_path)} AS {alias} (READ_ONLY)")
    try:
        rows = inner.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog = ? AND table_schema = 'main' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            [alias],
        ).fetchall()
        tables = [r[0] for r in rows]
        for t in tables:
            inner.execute(
                f'CREATE TABLE main."{t}" AS SELECT * FROM {alias}.main."{t}"'
            )
        log.info(
            "loaded %d table(s) into memory from %s: %s", len(tables), db_path, tables
        )
    finally:
        inner.execute(f"DETACH {alias}")
    return tables


def export_tables_to_parquet(
    inner: Any, tables: Sequence[str], out_dir: "str | Path"
) -> Path:
    """Write each of *tables* to ``<out_dir>/<table>.parquet`` (the engine's snapshot layout)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for t in tables:
        dest = out / f"{t}.parquet"
        inner.execute(
            f'COPY (SELECT * FROM "{t}") TO {_sql_str(dest)} (FORMAT PARQUET)'
        )
        log.debug("exported table %s -> %s", t, dest)
    return out
