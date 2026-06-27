from __future__ import annotations

import argparse
import os
from typing import Dict, List, Tuple

import duckdb


def _log(message: str) -> None:
    print(message)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _list_tables(con: duckdb.DuckDBPyConnection) -> List[str]:
    try:
        rows = con.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_type = 'BASE TABLE'
            """
        ).fetchall()
        tables = [row[0] for row in rows]
        if tables:
            return tables
    except Exception:
        pass

    rows = con.execute("PRAGMA show_tables").fetchall()
    return [row[0] for row in rows]


def _load_table_info(
    con: duckdb.DuckDBPyConnection, table: str
) -> Dict[str, Dict[str, object]]:
    df = con.execute(f"PRAGMA table_info({_quote_ident(table)})").df()
    info: Dict[str, Dict[str, object]] = {}
    for _, row in df.iterrows():
        info[str(row["name"])] = {
            "pk": bool(row.get("pk", 0)),
            "type": str(row.get("type", "")),
        }
    return info


def _load_foreign_keys(
    con: duckdb.DuckDBPyConnection, table: str
) -> List[Tuple[str, List[str], str, List[str]]]:
    try:
        fk_df = con.execute(f"PRAGMA foreign_key_list({_quote_ident(table)})").df()
    except Exception:
        return []

    if fk_df.empty:
        return []

    relationships: List[Tuple[str, List[str], str, List[str]]] = []
    if "id" in fk_df.columns:
        grouped = fk_df.groupby("id", sort=False)
        for _, group in grouped:
            ref_table = str(group["table"].iloc[0])
            from_cols = [str(val) for val in group["from"].tolist()]
            to_cols = [str(val) for val in group["to"].tolist()]
            relationships.append((table, from_cols, ref_table, to_cols))
    else:
        for _, row in fk_df.iterrows():
            relationships.append(
                (
                    table,
                    [str(row["from"])],
                    str(row["table"]),
                    [str(row["to"])],
                )
            )

    return relationships


def infer_schema_from_duckdb(duckdb_path: str) -> Dict[str, object]:
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        tables = _list_tables(con)
        table_col_info: Dict[str, Dict[str, Dict[str, object]]] = {}
        relationships: List[Tuple[str, List[str], str, List[str]]] = []

        for table in tables:
            table_col_info[table] = _load_table_info(con, table)
            relationships.extend(_load_foreign_keys(con, table))

        return {
            "tables": tables,
            "relationships": relationships,
            "table_col_info": table_col_info,
        }
    finally:
        con.close()


def extract_scale_columns(schema: Dict[str, object]) -> Dict[str, set]:
    """Extract columns that should be scaled (PK/FK columns)."""
    scale_columns: Dict[str, set] = {}

    # Add FK columns
    relationships: List = schema.get("relationships", [])  # type: ignore
    for table_l, col_l, table_r, col_r in relationships:
        if not isinstance(col_l, list):
            col_l = [col_l]
            col_r = [col_r]
        for table, columns in [(table_l, col_l), (table_r, col_r)]:
            for c in columns:
                scale_columns.setdefault(table, set()).add(c)

    # Add PK columns
    table_col_info: Dict = schema.get("table_col_info", {})  # type: ignore
    for table, cols in table_col_info.items():
        for column, meta in cols.items():
            if meta.get("pk"):
                scale_columns.setdefault(table, set()).add(column)

    return scale_columns


def find_numeric_offset(
    column: str,
    table: str,
    schema: Dict[str, object],
    column_stats: Dict[str, Dict[str, Dict[str, float]]],
) -> float:
    """Find the numeric offset for scaling a column."""
    relationships: List = schema.get("relationships", [])  # type: ignore

    # Check if this column references another table
    for t_out, col_out, t_in, col_in in relationships:
        if t_out != table:
            continue
        if not isinstance(col_out, list):
            col_out = [col_out]
            col_in = [col_in]
        if column not in col_out:
            continue

        c_idx = col_out.index(column)
        ref_max = column_stats.get(t_in, {}).get(col_in[c_idx], {}).get("max", 0)
        return float(ref_max) + 1

    # Use max value from current table
    max_val = column_stats.get(table, {}).get(column, {}).get("max", 0)
    return float(max_val) + 1


def _is_numeric_type(type_name: str) -> bool:
    type_name = type_name.lower()
    numeric_tokens = [
        "int",
        "decimal",
        "numeric",
        "real",
        "float",
        "double",
        "hugeint",
        "smallint",
        "bigint",
        "tinyint",
        "ubigint",
        "usmallint",
        "utinyint",
    ]
    return any(token in type_name for token in numeric_tokens)


def compute_column_stats(
    duckdb_path: str,
    schema: Dict[str, object],
    scale_columns: Dict[str, set],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        stats: Dict[str, Dict[str, Dict[str, float]]] = {}
        table_col_info = schema.get("table_col_info", {})
        assert isinstance(table_col_info, dict)
        for table, cols in scale_columns.items():
            col_info = table_col_info.get(table, {})
            for col in cols:
                type_name = str(col_info.get(col, {}).get("type", ""))
                if type_name and not _is_numeric_type(type_name):
                    continue

                max_val = con.execute(
                    f"SELECT MAX({_quote_ident(col)}) FROM {_quote_ident(table)}"
                ).fetchone()[0]  # type: ignore
                if max_val is None:
                    continue
                try:
                    max_float = float(max_val)
                except (TypeError, ValueError):
                    continue
                stats.setdefault(table, {})[col] = {"max": max_float}
        return stats
    finally:
        con.close()


def _numeric_scale_columns(schema: Dict[str, object]) -> Dict[str, set]:
    table_col_info = schema.get("table_col_info", {})
    assert isinstance(table_col_info, dict)
    scale_columns: Dict[str, set] = {}
    for table, cols in table_col_info.items():
        for col, meta in cols.items():
            type_name = str(meta.get("type", ""))
            if _is_numeric_type(type_name):
                scale_columns.setdefault(table, set()).add(col)
    return scale_columns


def _build_scaled_select_sql(
    table: str,
    columns: List[str],
    scale_columns: set,
    numeric_scale_columns: set,
    offsets: Dict[str, float],
    scale: int,
    col_types: Dict[str, str] | None = None,
) -> str:
    exprs: List[str] = []
    for col in columns:
        col_ident = _quote_ident(col)
        if col in scale_columns:
            if col in numeric_scale_columns:
                offset = offsets.get(col, 0.0)
                orig_type = (col_types or {}).get(col)
                scaled = f"({col_ident} + scale_idx * {offset})"
                if orig_type:
                    expr = f"CAST({scaled} AS {orig_type}) AS {col_ident}"
                else:
                    expr = f"{scaled} AS {col_ident}"
            else:
                expr = (
                    "CASE WHEN scale_idx = 0 THEN "
                    f"{col_ident} ELSE CAST({col_ident} AS VARCHAR) || '_' || "
                    "CAST(scale_idx - 1 AS VARCHAR) END AS "
                    f"{col_ident}"
                )
        else:
            expr = f"{col_ident} AS {col_ident}"
        exprs.append(expr)

    table_ident = _quote_ident(table)
    expr_list = ", ".join(exprs)
    return (
        f"SELECT {expr_list} "
        f"FROM {table_ident} "
        f"CROSS JOIN range(0, {scale}) AS s(scale_idx)"
    )


def _scale_up(
    duckdb_path: str,
    output_dir: str,
    schema: Dict[str, object],
    scale: int,
    scale_columns: Dict[str, set],
    column_stats: Dict[str, Dict[str, Dict[str, float]]],
) -> None:
    """Scale up dataset by multiplying rows with offset adjustments."""
    table_col_info: Dict = schema.get("table_col_info", {})  # type: ignore
    con = duckdb.connect(duckdb_path)
    os.makedirs(output_dir, exist_ok=True)

    output_duckdb_path = f"{output_dir}/imdb.duckdb"
    assert not os.path.exists(output_duckdb_path), (
        f"Output DuckDB already exists: {output_duckdb_path}"
    )
    safe_db_path = output_duckdb_path.replace("'", "''")
    con.execute(f"ATTACH '{safe_db_path}' AS scaled")

    try:
        tables: List[str] = schema.get("tables", [])  # type: ignore
        for table in tables:
            columns = list(table_col_info.get(table, {}).keys())
            if not columns:
                continue

            col_info = table_col_info.get(table, {})
            numeric_scale_columns = {
                col
                for col in scale_columns.get(table, set())
                if _is_numeric_type(str(col_info.get(col, {}).get("type", "")))
            }

            offsets: Dict[str, float] = {}
            for col in numeric_scale_columns:
                offsets[col] = find_numeric_offset(col, table, schema, column_stats)

            col_types = {
                col: str(meta.get("type", ""))
                for col, meta in col_info.items()
                if meta.get("type")
            }
            select_sql = _build_scaled_select_sql(
                table=table,
                columns=columns,
                scale_columns=scale_columns.get(table, set()),
                numeric_scale_columns=numeric_scale_columns,
                offsets=offsets,
                scale=scale,
                col_types=col_types,
            )

            output_path = os.path.join(output_dir, f"{table}.parquet")
            assert not os.path.exists(output_path), (
                f"Output file already exists: {output_path}"
            )

            _log(f"Scaling up {table} -> {output_path} (scale={scale}x)")
            safe_path = output_path.replace("'", "''")
            # Create the duckdb table first, then export to parquet from it
            con.execute(f"CREATE TABLE scaled.{_quote_ident(table)} AS {select_sql}")

            con.execute(
                f"COPY (SELECT * FROM scaled.{_quote_ident(table)}) TO '{safe_path}' (FORMAT 'parquet')"
            )
    finally:
        con.close()


def scale_up_duckdb_to_parquet(
    duckdb_path: str,
    output_dir: str,
    scale: int,
    output_duckdb_path: str | None = None,
) -> None:
    """Scale up a DuckDB database and output to Parquet files."""
    schema = infer_schema_from_duckdb(duckdb_path)
    scale_columns = extract_scale_columns(schema)

    if not any(scale_columns.values()):
        _log("No PK/FK columns found; scaling all numeric columns instead.")
        scale_columns = _numeric_scale_columns(schema)

    column_stats = compute_column_stats(duckdb_path, schema, scale_columns)

    _scale_up(
        duckdb_path=duckdb_path,
        output_dir=output_dir,
        schema=schema,
        scale=scale,
        scale_columns=scale_columns,
        column_stats=column_stats,
    )


def scale_down_duckdb_to_parquet(
    duckdb_path: str,
    output_dir: str,
    scale: float,
) -> None:
    """Scale down a DuckDB database by sampling and output to Parquet files.

    Args:
        duckdb_path: Path to DuckDB database
        output_dir: Output directory for Parquet files
        scale: Scale factor (0.0-1.0, e.g., 0.1 = keep 10% of data)
    """
    if not (0.0 < scale <= 1.0):
        raise ValueError(f"Scale must be between 0 and 1, got {scale}")

    schema = infer_schema_from_duckdb(duckdb_path)
    con = duckdb.connect(duckdb_path)
    os.makedirs(output_dir, exist_ok=True)

    output_duckdb_path = f"{output_dir}/imdb.duckdb"
    assert not os.path.exists(output_duckdb_path), (
        f"Output DuckDB already exists: {output_duckdb_path}"
    )
    safe_db_path = output_duckdb_path.replace("'", "''")
    con.execute(f"ATTACH '{safe_db_path}' AS scaled")

    try:
        # Find largest table size
        table_counts = dict()
        largest_size = 0
        tables: List[str] = schema.get("tables", [])  # type: ignore
        for table in tables:
            count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]  # type: ignore
            table_counts[table] = int(count)
            largest_size = max(largest_size, int(count))

        # calculate max size rate (clamp to at least 1 to avoid all tables being sampled)
        max_size = max(1, int(largest_size * scale))

        for table in tables:
            output_path = os.path.join(output_dir, f"{table}.parquet")
            assert not os.path.exists(output_path), (
                f"Output file already exists: {output_path}"
            )

            if table_counts[table] >= max_size:
                # do not sample down to 0
                actual_sample_rate = max(scale, max_size / table_counts[table])
            else:
                actual_sample_rate = 1.0

            _log(
                f"Scaling down {table} -> {output_path} (rate={actual_sample_rate:.2%})"
            )
            safe_path = output_path.replace("'", "''")

            if actual_sample_rate >= 1.0:
                # Copy entire table - create duckdb table first, then export to parquet
                con.execute(
                    f"CREATE TABLE scaled.{_quote_ident(table)} AS "
                    f"SELECT * FROM {_quote_ident(table)}"
                )

                con.execute(
                    f"COPY (SELECT * FROM scaled.{_quote_ident(table)}) TO '{safe_path}' (FORMAT 'parquet')"
                )
            else:
                # Calculate target row count
                if table_counts[table] == 0:
                    _log("  skipping empty table")
                else:
                    target_rows = int(round(table_counts[table] * scale))
                    if target_rows == 0:
                        target_rows = 1  # Keep at least 1 row from non-empty tables

                    sample_sql = f"SELECT * FROM {_quote_ident(table)} USING SAMPLE {actual_sample_rate * 100} PERCENT"
                    _log(f"  sampling method=RANDOM rows={target_rows}")
                    # Create the duckdb table first, then export to parquet from it.
                    # This ensures both contain exactly the same data.
                    con.execute(
                        f"CREATE TABLE scaled.{_quote_ident(table)} AS {sample_sql}"
                    )

                    con.execute(
                        f"COPY (SELECT * FROM scaled.{_quote_ident(table)}) TO '{safe_path}' (FORMAT 'parquet')"
                    )
    finally:
        con.close()


def validate_output_dtypes(duckdb_path: str, output_dir: str) -> None:
    """Check that parquet output dtypes match the source DuckDB dtypes."""
    src_con = duckdb.connect(duckdb_path, read_only=True)
    pq_con = duckdb.connect()
    try:
        tables = _list_tables(src_con)
        mismatches: List[str] = []
        for table in tables:
            parquet_path = os.path.join(output_dir, f"{table}.parquet")
            if not os.path.exists(parquet_path):
                mismatches.append(f"{table}: parquet file missing")
                continue

            src_info = _load_table_info(src_con, table)
            safe_path = parquet_path.replace("'", "''")
            pq_cols = pq_con.execute(
                f"SELECT column_name, column_type "
                f"FROM (DESCRIBE SELECT * FROM read_parquet('{safe_path}'))"
            ).fetchall()
            pq_types = {row[0]: row[1] for row in pq_cols}

            for col, meta in src_info.items():
                src_type = str(meta.get("type", "")).upper()
                pq_type = pq_types.get(col, "MISSING").upper()
                if pq_type == "MISSING":
                    mismatches.append(f"{table}.{col}: missing in parquet")
                elif src_type != pq_type:
                    mismatches.append(
                        f"{table}.{col}: expected {src_type}, got {pq_type}"
                    )

        if mismatches:
            msg = "Output dtype mismatches:\n  " + "\n  ".join(mismatches)
            raise ValueError(msg)
        _log("Dtype validation passed: all output parquet types match source.")
    finally:
        src_con.close()
        pq_con.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scale DuckDB datasets and output Parquet files."
    )
    parser.add_argument("--duckdb", required=True, help="Path to the DuckDB file.")
    parser.add_argument(
        "--output-dir", required=True, help="Directory for Parquet output files."
    )

    parser.add_argument(
        "--scale",
        type=float,
        required=True,
        help="Scale factor multiplier (e.g., 2 = double rows, 0.1 = keep 10% of data).",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    output_dir = args.output_dir
    assert "/sf" not in output_dir, "Output dir should not contain scale factor subdir."

    direction = "up" if args.scale >= 1 else "down"
    _log(f"Starting scale-{direction} operation... (scale={args.scale})")

    if direction == "up":
        # make sure scale is int for up
        assert int(args.scale) == args.scale, (
            "Scale factor must be an integer for scale-up."
        )
        formatted_scale = int(args.scale)
    else:
        formatted_scale = args.scale

    output_dir = f"{output_dir}/sf{formatted_scale}"

    # create output dir if not exists
    os.makedirs(output_dir, exist_ok=True)

    if direction == "up":
        scale_up_duckdb_to_parquet(
            duckdb_path=args.duckdb,
            output_dir=output_dir,
            scale=formatted_scale,
        )
        _log("Scale-up complete.")
    elif direction == "down":
        scale_down_duckdb_to_parquet(
            duckdb_path=args.duckdb,
            output_dir=output_dir,
            scale=formatted_scale,
        )
        _log("Scale-down complete.")

    validate_output_dtypes(args.duckdb, output_dir)


if __name__ == "__main__":
    main()
