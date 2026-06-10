from __future__ import annotations

import argparse
import os
from typing import Dict, List, Tuple

import duckdb


def _log(message: str) -> None:
    print(message)


def _parse_scale_dir(name: str) -> Tuple[float | None, str]:
    if not name.startswith("sf"):
        return None, name
    try:
        return float(name[2:]), name
    except ValueError:
        return None, name


def _list_scale_dirs(base_path: str) -> List[str]:
    if not os.path.isdir(base_path):
        raise FileNotFoundError(f"Base path does not exist: {base_path}")
    entries = []
    for name in os.listdir(base_path):
        full = os.path.join(base_path, name)
        if os.path.isdir(full) and name.startswith("sf"):
            entries.append(name)
    if not entries:
        raise FileNotFoundError(
            f"No scale factor directories found under: {base_path} (expected sf*)"
        )
    entries.sort(
        key=lambda n: (_parse_scale_dir(n)[0] is None, _parse_scale_dir(n)[0], n)
    )
    return entries


def _list_parquet_tables(scale_dir: str) -> List[str]:
    tables = []
    for name in os.listdir(scale_dir):
        if name.endswith(".parquet"):
            tables.append(name[:-8])
    tables.sort()
    return tables


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


def _load_table_columns(con: duckdb.DuckDBPyConnection, table: str) -> List[str]:
    rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [str(row[1]) for row in rows]


def _load_parquet_schema(
    con: duckdb.DuckDBPyConnection, parquet_path: str
) -> List[Tuple[str, str]]:
    safe_path = parquet_path.replace("'", "''")
    rows = con.execute(
        "SELECT column_name, column_type "
        f"FROM (DESCRIBE SELECT * FROM read_parquet('{safe_path}'))"
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _schema_signature(
    schema: List[Tuple[str, str]],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    names = tuple(col for col, _ in schema)
    types = tuple(col_type.upper() for _, col_type in schema)
    return names, types


def _compare_schemas(
    reference: Dict[str, List[Tuple[str, str]]],
    target: Dict[str, List[Tuple[str, str]]],
    target_label: str,
) -> List[str]:
    mismatches: List[str] = []
    ref_tables = set(reference.keys())
    tgt_tables = set(target.keys())

    missing_tables = sorted(ref_tables - tgt_tables)
    extra_tables = sorted(tgt_tables - ref_tables)

    for table in missing_tables:
        mismatches.append(f"{target_label}: missing table {table}")
    for table in extra_tables:
        mismatches.append(f"{target_label}: extra table {table}")

    for table in sorted(ref_tables & tgt_tables):
        ref_schema = reference[table]
        tgt_schema = target[table]
        ref_names, ref_types = _schema_signature(ref_schema)
        tgt_names, tgt_types = _schema_signature(tgt_schema)

        if ref_names != tgt_names:
            mismatches.append(f"{target_label}.{table}: column names/order mismatch")
            continue
        if ref_types != tgt_types:
            diffs = []
            for idx, (ref_type, tgt_type) in enumerate(zip(ref_types, tgt_types)):
                if ref_type != tgt_type:
                    diffs.append(f"{ref_names[idx]}: {ref_type} != {tgt_type}")
            if diffs:
                mismatches.append(
                    f"{target_label}.{table}: dtype mismatch -> " + "; ".join(diffs)
                )

    return mismatches


def _collect_schemas(scale_dir: str) -> Dict[str, List[Tuple[str, str]]]:
    con = duckdb.connect()
    try:
        tables = _list_parquet_tables(scale_dir)
        schemas: Dict[str, List[Tuple[str, str]]] = {}
        for table in tables:
            parquet_path = os.path.join(scale_dir, f"{table}.parquet")
            if not os.path.exists(parquet_path):
                continue
            schemas[table] = _load_parquet_schema(con, parquet_path)
        return schemas
    finally:
        con.close()


def _count_table_stats(
    con: duckdb.DuckDBPyConnection, table_expr: str, columns: List[str]
) -> Tuple[int, Dict[str, int]]:
    select_parts = ["COUNT(*) AS row_count"]
    aliases: List[str] = []
    for idx, col in enumerate(columns):
        alias = f"nn_{idx}"
        aliases.append(alias)
        select_parts.append(f"COUNT({_quote_ident(col)}) AS {alias}")
    sql = f"SELECT {', '.join(select_parts)} FROM {table_expr}"
    row = con.execute(sql).fetchone()
    assert row is not None
    row_count = int(row[0])
    non_null: Dict[str, int] = {}
    for idx, col in enumerate(columns):
        non_null[col] = int(row[idx + 1])
    return row_count, non_null


def _expected_counts_for_scale_down(
    source_counts: Dict[str, int],
    scale: float,
) -> Tuple[int, Dict[str, int]]:
    if not source_counts:
        return 0, {}
    largest_size = max(source_counts.values())
    max_size = int(largest_size * scale)
    expected: Dict[str, int] = {}
    for table, count in source_counts.items():
        if count >= max_size:
            expected[table] = int(round(count * scale))
        else:
            expected[table] = count
    return max_size, expected


def _q1a_sql() -> str:
    return """SELECT COUNT(*) FROM title as t,
kind_type as kt,
movie_info as mi1,
info_type as it1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND it1.id = '3'
AND it2.id = '4'
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND mi1.info IN ('Adventure','Crime','Documentary','Drama','Short','Sport','Thriller')
AND mi2.info IN ('English','Italian','Japanese','Spanish')
AND kt.kind IN ('tv series','video game','video movie')
AND rt.role IN ('cinematographer')
AND n.gender IN ('f')
AND t.production_year <= 1975
AND 1875 < t.production_year"""


def _run_query_scalar(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    row = con.execute(sql).fetchone()
    if row is None:
        raise ValueError("Query returned no rows.")
    return row[0]


def _register_parquet_views(
    con: duckdb.DuckDBPyConnection, parquet_dir: str, tables: List[str]
) -> None:
    for table in tables:
        parquet_path = os.path.join(parquet_dir, f"{table}.parquet")
        if not os.path.exists(parquet_path):
            continue
        safe_path = parquet_path.replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW {_quote_ident(table)} AS "
            f"SELECT * FROM read_parquet('{safe_path}')"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check parquet schemas and data similarity against source DuckDB."
    )
    parser.add_argument(
        "--datasets-path",
        default="/mnt/labstore/bespoke_olap/imdb_parquet/",
        help="Base path containing sf* parquet directories and the source DuckDB.",
    )
    parser.add_argument(
        "--source-db",
        default=None,
        help="Path to source DuckDB (default: datasets-path/imdb.duckdb).",
    )
    parser.add_argument(
        "--null-delta",
        type=float,
        default=0.05,
        help="Max allowed absolute increase in null fraction per column.",
    )
    parser.add_argument(
        "--sample-tol",
        type=float,
        default=0.05,
        help="Relative tolerance for scale-down row/non-null counts.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    datasets_path = args.datasets_path
    source_db_path = args.source_db or os.path.join(datasets_path, "imdb.duckdb")
    null_delta = float(args.null_delta)
    sample_tol = float(args.sample_tol)

    if not os.path.exists(source_db_path):
        raise FileNotFoundError(f"Source DuckDB not found: {source_db_path}")

    scale_dirs = _list_scale_dirs(datasets_path)
    if len(scale_dirs) < 2:
        raise ValueError(
            f"Need at least two sf* directories to compare under: {datasets_path}"
        )

    schemas: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}
    for scale_dir in scale_dirs:
        full_dir = os.path.join(datasets_path, scale_dir)
        schema = _collect_schemas(full_dir)
        if not schema:
            raise ValueError(f"{scale_dir}: no parquet files found")
        schemas[scale_dir] = schema

    schema_mismatches: List[str] = []
    base = scale_dirs[0]
    for other in scale_dirs[1:]:
        schema_mismatches.extend(_compare_schemas(schemas[base], schemas[other], other))

    if schema_mismatches:
        msg = "Parquet schema mismatches found:\n  " + "\n  ".join(schema_mismatches)
        raise ValueError(msg)

    _log("Schema check passed: all parquet headers match across scales.")

    q1a = _q1a_sql()
    src_con = duckdb.connect(source_db_path, read_only=True)
    pq_con = duckdb.connect()
    try:
        source_tables = _list_tables(src_con)
        if not source_tables:
            raise ValueError(f"No tables found in source DB: {source_db_path}")
        source_q1a = _run_query_scalar(src_con, q1a)

        source_columns: Dict[str, List[str]] = {}
        source_counts: Dict[str, int] = {}
        source_non_null: Dict[str, Dict[str, int]] = {}

        for table in source_tables:
            cols = _load_table_columns(src_con, table)
            if not cols:
                continue
            source_columns[table] = cols
            table_expr = _quote_ident(table)
            row_count, non_null_counts = _count_table_stats(src_con, table_expr, cols)
            source_counts[table] = row_count
            source_non_null[table] = non_null_counts

        if not source_counts:
            raise ValueError("No table stats available from source DB.")

        for scale_dir in scale_dirs:
            scale_val, _ = _parse_scale_dir(scale_dir)
            if scale_val is None:
                continue
            full_dir = os.path.join(datasets_path, scale_dir)
            parquet_tables = _list_parquet_tables(full_dir)
            parquet_set = set(parquet_tables)
            source_set = set(source_tables)

            data_mismatches: List[str] = []

            missing_tables = sorted(source_set - parquet_set)
            extra_tables = sorted(parquet_set - source_set)
            for table in missing_tables:
                data_mismatches.append(f"{scale_dir}: missing table {table}")
            for table in extra_tables:
                data_mismatches.append(f"{scale_dir}: extra table {table}")

            if scale_val < 1:
                max_size, expected_counts = _expected_counts_for_scale_down(
                    source_counts, scale_val
                )
            else:
                max_size = 0
                expected_counts = {
                    table: int(count * int(scale_val))
                    for table, count in source_counts.items()
                }

            scaled_db_path = os.path.join(full_dir, "imdb.duckdb")
            if not os.path.exists(scaled_db_path):
                data_mismatches.append(f"{scale_dir}: missing scaled duckdb file")
            else:
                scaled_con = duckdb.connect(scaled_db_path, read_only=True)
                try:
                    scaled_q1a = _run_query_scalar(scaled_con, q1a)
                finally:
                    scaled_con.close()

                _register_parquet_views(pq_con, full_dir, source_tables)
                parquet_q1a = _run_query_scalar(pq_con, q1a)
                if scaled_q1a != parquet_q1a:
                    data_mismatches.append(
                        f"{scale_dir}: q1a mismatch (duckdb={scaled_q1a}, parquet={parquet_q1a})"
                    )

                if scale_val == 1.0 and scaled_q1a != source_q1a:
                    data_mismatches.append(
                        f"{scale_dir}: q1a mismatch vs source "
                        f"(source={source_q1a}, scaled_db={scaled_q1a})"
                    )

            for table in source_tables:
                if table not in parquet_set:
                    continue
                parquet_path = os.path.join(full_dir, f"{table}.parquet")
                if not os.path.exists(parquet_path):
                    data_mismatches.append(f"{scale_dir}.{table}: parquet missing")
                    continue

                safe_path = parquet_path.replace("'", "''")
                parquet_schema = _load_parquet_schema(pq_con, parquet_path)
                parquet_cols = [name for name, _ in parquet_schema]
                parquet_col_set = set(parquet_cols)
                source_cols = source_columns.get(table, [])

                missing_cols = [c for c in source_cols if c not in parquet_col_set]
                extra_cols = [c for c in parquet_cols if c not in source_cols]
                for col in missing_cols:
                    data_mismatches.append(
                        f"{scale_dir}.{table}.{col}: missing column in parquet"
                    )
                for col in extra_cols:
                    data_mismatches.append(
                        f"{scale_dir}.{table}.{col}: extra column in parquet"
                    )

                if missing_cols:
                    continue

                table_expr = f"read_parquet('{safe_path}')"
                row_count, non_null_counts = _count_table_stats(
                    pq_con, table_expr, source_cols
                )

                expected = expected_counts.get(table, 0)
                if scale_val < 1 and source_counts.get(table, 0) >= max_size:
                    tolerance = max(1.0, expected * sample_tol)
                    if abs(row_count - expected) > tolerance:
                        data_mismatches.append(
                            f"{scale_dir}.{table}: row count {row_count} "
                            f"outside expected ~{expected} (+/-{tolerance:.1f})"
                        )
                else:
                    if row_count != expected:
                        data_mismatches.append(
                            f"{scale_dir}.{table}: row count {row_count} "
                            f"!= expected {expected}"
                        )

                src_row_count = source_counts.get(table, 0)
                for col in source_cols:
                    src_nn = source_non_null.get(table, {}).get(col, 0)
                    pq_nn = non_null_counts.get(col, 0)

                    if src_nn > 0 and pq_nn == 0:
                        data_mismatches.append(
                            f"{scale_dir}.{table}.{col}: all values NULL after scaling"
                        )
                        continue

                    if src_row_count > 0 and row_count > 0:
                        src_null_frac = 1.0 - (src_nn / src_row_count)
                        pq_null_frac = 1.0 - (pq_nn / row_count)
                        if (
                            src_null_frac < 0.99
                            and (pq_null_frac - src_null_frac) > null_delta
                        ):
                            data_mismatches.append(
                                f"{scale_dir}.{table}.{col}: null fraction increased "
                                f"from {src_null_frac:.3f} to {pq_null_frac:.3f}"
                            )

                    if scale_val < 1 and source_counts.get(table, 0) >= max_size:
                        expected_nn = src_nn * scale_val
                        tolerance = max(1.0, expected_nn * sample_tol)
                        if abs(pq_nn - expected_nn) > tolerance:
                            data_mismatches.append(
                                f"{scale_dir}.{table}.{col}: non-null count {pq_nn} "
                                f"outside expected ~{expected_nn:.1f} (+/-{tolerance:.1f})"
                            )
                    else:
                        expected_nn = src_nn * (int(scale_val) if scale_val >= 1 else 1)
                        if pq_nn != expected_nn:
                            data_mismatches.append(
                                f"{scale_dir}.{table}.{col}: non-null count {pq_nn} "
                                f"!= expected {expected_nn}"
                            )

            if data_mismatches:
                msg = f"{scale_dir}: content mismatches found:\n  " + "\n  ".join(
                    data_mismatches
                )
                raise ValueError(msg)

        _log(
            "Content check passed: parquet data resembles source DB (nulls/counts ok)."
        )
    finally:
        src_con.close()
        pq_con.close()


if __name__ == "__main__":
    main()
