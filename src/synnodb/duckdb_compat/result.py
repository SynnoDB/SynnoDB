"""``SynnoResult`` — a bespoke engine result that quacks like a DuckDB cursor result.

Built from a single ``pyarrow.Table`` (the engine's typed output), it exposes the
same fetch surface DuckDB does — ``fetchone/all/many``, ``df``, ``arrow``, ``pl``,
``fetchnumpy``, ``description`` — so ``SynnoConnection`` can hand it back wherever
DuckDB would return its own result, transparently.

Cursor semantics match DuckDB: ``fetch*`` consume rows progressively; once consumed,
``fetchone`` returns ``None`` and ``fetchall`` returns ``[]``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pyarrow as pa


class SynnoResult:
    """A DuckDB-compatible result view over a ``pyarrow.Table``."""

    __slots__ = ("_table", "_rows", "_pos", "_duckdb_types")

    def __init__(
        self, table: pa.Table, duckdb_types: Optional[Sequence[str]] = None
    ) -> None:
        self._table = table
        # Materialize rows as positional tuples once (DuckDB returns tuples, not dicts).
        columns = [col.to_pylist() for col in table.columns]
        self._rows: List[Tuple[Any, ...]] = list(zip(*columns)) if columns else []
        self._pos = 0
        # Optional canonical DuckDB type strings (from the template's description),
        # used for `description` so dtypes match DuckDB exactly; falls back to Arrow.
        self._duckdb_types = list(duckdb_types) if duckdb_types is not None else None

    # ---- introspection --------------------------------------------------
    @property
    def description(self) -> List[Tuple[Any, ...]]:
        """DBAPI-style 7-tuples ``(name, type, None, None, None, None, None)``."""
        names = self._table.schema.names
        if self._duckdb_types is not None and len(self._duckdb_types) == len(names):
            types = self._duckdb_types
        else:
            types = [str(f.type) for f in self._table.schema]
        return [
            (name, type_, None, None, None, None, None)
            for name, type_ in zip(names, types)
        ]

    @property
    def columns(self) -> List[str]:
        return list(self._table.schema.names)

    def __len__(self) -> int:
        return self._table.num_rows

    # ---- cursor-style fetching -----------------------------------------
    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1) -> List[Tuple[Any, ...]]:
        if size < 0:
            return self.fetchall()
        end = self._pos + size
        rows = self._rows[self._pos : end]
        self._pos = min(end, len(self._rows))
        return rows

    def fetchall(self) -> List[Tuple[Any, ...]]:
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Tuple[Any, ...]]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    # ---- bulk egress (independent of cursor position) ------------------
    # Mirror DuckDB 1.5.x exactly: `arrow()` yields a RecordBatchReader, while
    # `to_arrow_table()`/`fetch_arrow_table()` yield a materialized Table.
    def arrow(
        self, batch_size: int = 1_000_000, *_a: Any, **_kw: Any
    ) -> pa.RecordBatchReader:
        return self._table.to_reader(max_chunksize=batch_size)

    def to_arrow_table(self, *_a: Any, **_kw: Any) -> pa.Table:
        return self._table

    fetch_arrow_table = to_arrow_table

    def df(self, *_a: Any, **_kw: Any):
        return self._table.to_pandas()

    fetchdf = df
    fetch_df = df
    to_df = df

    def pl(self, *_a: Any, **_kw: Any):
        import polars as pl  # lazy: polars is not a core dep

        return pl.from_arrow(self._table)

    def fetchnumpy(self) -> Dict[str, Any]:
        return {
            name: self._table.column(name).to_numpy(zero_copy_only=False)
            for name in self._table.schema.names
        }

    def fetch_record_batch(self, rows_per_batch: int = 1_000_000):
        return self._table.to_reader(max_chunksize=rows_per_batch)
