"""Download the MusicBrainz full data export and load it into a single DuckDB file.

MusicBrainz publishes its production database as a set of PostgreSQL ``COPY`` dumps under
``https://data.metabrainz.org/pub/musicbrainz/data/fullexport/``. Each ``mbdump*.tar.bz2``
archive extracts to an ``mbdump/`` directory of tab-separated files named after the tables
they hold. The dump files carry **no header row and no schema**, so column names and types
come from the MusicBrainz schema DDL (``admin/sql/CreateTables.sql`` and friends).

This script:

1. Resolves the newest export via the ``LATEST`` pointer.
2. Downloads the requested archives (default: ``mbdump``, ``mbdump-editor``, ``mbdump-derived``)
   with HTTP-range resume and SHA256 verification against the export's ``SHA256SUMS``.
3. Extracts the ``mbdump/`` table files.
4. Parses the schema DDL to recover per-table column names and types. Each table's DDL column
   count is verified against the actual field count in its dump file; on a mismatch the table
   is loaded with generic column names instead of being skipped, so a schema drift never loses
   data.
5. Loads every table into ``<storage_dir>/musicbrainz/musicbrainz.duckdb`` - staged as text and
   then cast to typed columns, with PostgreSQL ``COPY`` backslash-escaping undone in text fields.

Every stage is idempotent: a verified archive is not re-downloaded, an extracted archive is not
re-extracted, and a table already present in the DuckDB file is not reloaded. The DuckDB file is
built in a ``.partial`` sibling and atomically renamed into place only once every table loads, so
an interrupted run never leaves a half-populated database behind.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import duckdb
import requests

FULLEXPORT_URL = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"

# Archives requested for this workload: the core catalog, editor data, and derived tables
# (tags, ratings, annotations, meta). Everything a query workload touches lives in these three.
DEFAULT_ARCHIVES: tuple[str, ...] = ("mbdump", "mbdump-editor", "mbdump-derived")

# MusicBrainz schema DDL lives across several files by PostgreSQL schema. The three archives above
# only touch the main ``musicbrainz`` schema, but we merge the rest so the loader keeps working if
# other archives are added via --archives.
DDL_FILES: tuple[str, ...] = (
    "admin/sql/CreateTables.sql",
    "admin/sql/statistics/CreateTables.sql",
    "admin/sql/caa/CreateTables.sql",
    "admin/sql/eaa/CreateTables.sql",
    "admin/sql/documentation/CreateTables.sql",
    "admin/sql/wikidocs/CreateTables.sql",
)
DDL_REPO_RAW = "https://raw.githubusercontent.com/metabrainz/musicbrainz-server"

_DOWNLOAD_CHUNK = 1 << 20  # 1 MiB


# --------------------------------------------------------------------------------------------- #
# Resource limits (mirrors gen_tpc_h_data.py so generation and loading behave the same way).
# --------------------------------------------------------------------------------------------- #
def _memory_limit_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                return max(1, int(total_kb / 1024 / 1024 * 0.65))
    raise RuntimeError("Could not read MemTotal from /proc/meminfo")


def _thread_count() -> int:
    return os.cpu_count() or 8


# --------------------------------------------------------------------------------------------- #
# Export resolution + download.
# --------------------------------------------------------------------------------------------- #
def resolve_export_dir(export: str | None) -> str:
    """Return the timestamped export directory name (e.g. ``20260718-002132``).

    ``export`` pins a specific directory; ``None`` follows the ``LATEST`` pointer.
    """
    if export:
        return export
    latest = requests.get(f"{FULLEXPORT_URL}/LATEST", timeout=60)
    latest.raise_for_status()
    name = latest.text.strip()
    if not re.fullmatch(r"\d{8}-\d{6}", name):
        raise RuntimeError(f"Unexpected LATEST pointer contents: {name!r}")
    return name


def fetch_sha256sums(base_url: str) -> dict[str, str]:
    """Parse the export's ``SHA256SUMS`` into ``{filename: hexdigest}``."""
    resp = requests.get(f"{base_url}/SHA256SUMS", timeout=60)
    resp.raise_for_status()
    sums: dict[str, str] = {}
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition(" ")
        sums[name.strip().lstrip("*")] = digest
    return sums


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_DOWNLOAD_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download_archive(base_url: str, name: str, dest: Path, expected_sha: str | None) -> None:
    """Download ``<base_url>/<name>`` to ``dest``, resuming a partial file and verifying SHA256.

    A ``dest`` that already matches ``expected_sha`` is left untouched (idempotent re-run). A
    partial download resumes via an HTTP ``Range`` request when the server supports it.
    """
    if dest.exists() and expected_sha and _sha256(dest) == expected_sha:
        print(f"  {name}: present and verified, skipping download")
        return

    url = f"{base_url}/{name}"
    resume_from = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    mode = "ab" if resume_from else "wb"
    with requests.get(url, headers=headers, stream=True, timeout=300) as resp:
        if resume_from and resp.status_code == 200:
            # Server ignored the Range request; restart from scratch.
            resume_from, mode = 0, "wb"
        elif resume_from and resp.status_code == 416:
            # Already have the whole file; fall through to verification.
            resp.close()
        else:
            resp.raise_for_status()

        if not (resume_from and resp.status_code == 416):
            total = resume_from + int(resp.headers.get("Content-Length", 0))
            done = resume_from
            last_pct = -1
            with open(dest, mode) as f:
                for chunk in resp.iter_content(_DOWNLOAD_CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if pct != last_pct:
                        print(
                            f"\r  {name}: {done / 1e9:.2f} GB"
                            + (f" / {total / 1e9:.2f} GB ({pct}%)" if total else ""),
                            end="",
                            flush=True,
                        )
                        last_pct = pct
            print()

    if expected_sha:
        print(f"  {name}: verifying SHA256 ...")
        actual = _sha256(dest)
        if actual != expected_sha:
            raise RuntimeError(
                f"{name}: SHA256 mismatch (expected {expected_sha}, got {actual}). "
                "Delete the file and retry."
            )
        print(f"  {name}: verified")


def _parallel_bunzip2() -> str | None:
    """Return a parallel bzip2 decompressor on PATH, or ``None``.

    ``lbzip2`` decompresses *any* bzip2 stream across all cores (it finds block boundaries), so
    it is preferred. ``pbzip2`` only parallelizes streams it produced itself and otherwise falls
    back to a single thread, so it is a distant second and skipped here in favor of the built-in
    tarfile path when ``lbzip2`` is absent.
    """
    return shutil.which("lbzip2")


def extract_archive(archive: Path, extract_dir: Path) -> None:
    """Extract ``archive`` into ``extract_dir`` unless a marker says it is already done.

    bzip2 decompression is the slow part of the whole pipeline: the ~7 GB core dump expands to
    tens of GB and single-threaded ``libbz2`` (what Python's ``tarfile`` uses) pins one core. When
    ``lbzip2`` is available we shell out to ``tar -I lbzip2`` to decompress across every core -
    roughly a 50x speedup on a many-core host - and fall back to the pure-Python reader otherwise.
    """
    marker = extract_dir / f".extracted-{archive.name}"
    if marker.exists():
        print(f"  {archive.name}: already extracted, skipping")
        return
    extract_dir.mkdir(parents=True, exist_ok=True)

    par = _parallel_bunzip2()
    if par:
        print(f"  {archive.name}: extracting (parallel via {Path(par).name}) ...")
        subprocess.run(
            ["tar", "-I", par, "-xf", str(archive), "-C", str(extract_dir)],
            check=True,
        )
    else:
        print(
            f"  {archive.name}: extracting (single-threaded; install lbzip2 for a large speedup) ..."
        )
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(extract_dir, filter="data")
    marker.write_text("ok\n")


# --------------------------------------------------------------------------------------------- #
# Schema DDL parsing.
# --------------------------------------------------------------------------------------------- #
_NON_COLUMN = re.compile(
    r"^(CHECK|CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|EXCLUDE|LIKE)\b", re.IGNORECASE
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _duckdb_type(pg_type: str) -> str:
    """Map a PostgreSQL column type to the DuckDB type we cast the staged text into."""
    t = pg_type.upper()
    if "TIMESTAMP WITH TIME ZONE" in t or "TIMESTAMPTZ" in t:
        return "TIMESTAMPTZ"
    if "TIMESTAMP" in t:
        return "TIMESTAMP"
    if "BIGSERIAL" in t or "BIGINT" in t or "INT8" in t:
        return "BIGINT"
    if "SMALLINT" in t or "INT2" in t:
        return "SMALLINT"
    if "SERIAL" in t or "INTEGER" in t or re.search(r"\bINT4?\b", t):
        return "INTEGER"
    if "BOOLEAN" in t or t.strip() == "BOOL":
        return "BOOLEAN"
    if "UUID" in t:
        return "UUID"
    if "DATE" in t:
        return "DATE"
    if "NUMERIC" in t or "DECIMAL" in t:
        return "DOUBLE"
    if "JSON" in t:
        return "JSON"
    # VARCHAR, TEXT, CHAR, POINT, INTERVAL, and the schema's ENUM types all round-trip as text.
    return "VARCHAR"


def parse_ddl(sql: str) -> dict[str, list[tuple[str, str]]]:
    """Parse ``CREATE TABLE`` statements into ``{table: [(column, duckdb_type), ...]}``."""
    tables: dict[str, list[tuple[str, str]]] = {}
    create_re = re.compile(r"^\s*CREATE TABLE\s+(?:\w+\.)?(\w+)\s*\(", re.IGNORECASE)
    lines = sql.splitlines()
    i = 0
    while i < len(lines):
        m = create_re.match(lines[i])
        if not m:
            i += 1
            continue
        table = m.group(1).lower()
        columns: list[tuple[str, str]] = []
        i += 1
        while i < len(lines):
            raw = lines[i]
            i += 1
            body = raw.split("--", 1)[0].strip()
            if body.startswith(")"):
                break
            body = body.rstrip(",").strip()
            if not body or _NON_COLUMN.match(body):
                continue
            name, _, rest = body.partition(" ")
            name = name.strip().strip('"').lower()
            if not _IDENT.match(name) or not rest.strip():
                continue
            columns.append((name, _duckdb_type(rest.strip())))
        if columns:
            tables.setdefault(table, [])
            # Prefer the definition with more columns when a name appears in several schema files.
            if len(columns) > len(tables[table]):
                tables[table] = columns
    return tables


def load_schema(schema_ref: str) -> dict[str, list[tuple[str, str]]]:
    """Fetch and merge the MusicBrainz DDL files at ``schema_ref`` (a git ref)."""
    merged: dict[str, list[tuple[str, str]]] = {}
    for rel in DDL_FILES:
        url = f"{DDL_REPO_RAW}/{schema_ref}/{rel}"
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except requests.HTTPError:
            continue  # Optional schema files (caa/eaa/...) may not exist on every ref.
        for table, cols in parse_ddl(resp.text).items():
            if len(cols) > len(merged.get(table, [])):
                merged[table] = cols
    if not merged:
        raise RuntimeError(f"No tables parsed from MusicBrainz DDL at ref {schema_ref!r}")
    return merged


# --------------------------------------------------------------------------------------------- #
# DuckDB loading.
# --------------------------------------------------------------------------------------------- #
def _first_line_field_count(path: Path) -> int | None:
    """Number of tab-separated fields in the first line, or ``None`` for an empty file."""
    with open(path, "rb") as f:
        line = f.readline()
    if not line:
        return None
    return line.rstrip(b"\n").count(b"\t") + 1


def _unescape_expr(col: str) -> str:
    """SQL that undoes PostgreSQL ``COPY`` text-format backslash escaping in a text column.

    ``COPY`` escapes ``\\``, tab, newline, CR, and a few control chars. We first hide escaped
    backslashes behind a sentinel (``chr(1)``, which never occurs in MusicBrainz text) so the
    remaining backslashes are unambiguous escape leads, translate the escapes, then restore the
    literal backslashes - matching PostgreSQL's single left-to-right pass.
    """
    q = f'"{col}"'
    expr = f"replace({q}, '\\\\', chr(1))"
    for seq, code in (("\\t", 9), ("\\n", 10), ("\\r", 13), ("\\b", 8), ("\\f", 12), ("\\v", 11)):
        expr = f"replace({expr}, '{seq}', chr({code}))"
    return f"replace({expr}, chr(1), chr(92))"


def _select_expr(col: str, duckdb_type: str, unescape: bool) -> str:
    """Cast expression turning the staged text column into its typed, aliased form."""
    q = f'"{col}"'
    if duckdb_type == "BOOLEAN":
        cast = f"CASE WHEN {q} = 't' THEN TRUE WHEN {q} = 'f' THEN FALSE ELSE NULL END"
    elif duckdb_type in ("VARCHAR", "JSON"):
        cast = _unescape_expr(col) if unescape else q
        if duckdb_type == "JSON":
            cast = f"TRY_CAST({cast} AS JSON)"
    else:
        cast = f"TRY_CAST({q} AS {duckdb_type})"
    return f"{cast} AS {q}"


def load_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    dump_name: str,
    data_file: Path,
    schema: dict[str, list[tuple[str, str]]],
    *,
    unescape: bool,
) -> str:
    """Load a single dump file into ``table``. Returns a one-word status for the run summary.

    ``dump_name`` is the raw dump filename (possibly schema-qualified, e.g.
    ``documentation.l_area_area_example``) used to resolve columns from the DDL; ``table`` is the
    sanitized DuckDB table name.
    """
    field_count = _first_line_field_count(data_file)
    # Core tables have plain filenames (artist, release); tables from non-default PostgreSQL
    # schemas are dumped schema-qualified (documentation.l_area_area_example) - strip the
    # qualifier so both resolve against the unqualified DDL table names.
    cols = schema.get(dump_name) or schema.get(dump_name.rsplit(".", 1)[-1])

    if cols is not None and field_count is not None and len(cols) != field_count:
        print(
            f"  {table}: DDL has {len(cols)} columns but dump has {field_count}; "
            "loading with generic column names"
        )
        cols = None
    if cols is None:
        # No DDL match (or a count mismatch): fall back to generic all-text columns so the table
        # is never dropped. An empty file with no DDL is the only case we cannot type or shape.
        if field_count is None:
            print(f"  {table}: empty and no schema, creating empty single-column table")
            con.execute(f'CREATE TABLE "{table}" ("col_0" VARCHAR)')
            return "empty"
        cols = [(f"col_{i}", "VARCHAR") for i in range(field_count)]

    columns_map = ", ".join(f"'{name}': 'VARCHAR'" for name, _ in cols)
    read_csv = (
        f"read_csv('{data_file.as_posix()}', delim='\t', header=false, quote='', escape='', "
        f"nullstr='\\N', new_line='\\n', auto_detect=false, columns={{{columns_map}}})"
    )
    select = ", ".join(_select_expr(name, dtype, unescape) for name, dtype in cols)
    con.execute(f'CREATE TABLE "{table}" AS SELECT {select} FROM {read_csv}')
    (rows,) = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()
    print(f"  {table}: loaded {rows:,} rows ({len(cols)} columns)")
    return "loaded"


def load_into_duckdb(
    duckdb_path: Path,
    mbdump_dir: Path,
    schema: dict[str, list[tuple[str, str]]],
    *,
    spill_dir: Path,
    unescape: bool,
) -> None:
    """Load every table file under ``mbdump_dir`` into ``duckdb_path`` (built via a .partial file)."""
    data_files = sorted(p for p in mbdump_dir.iterdir() if p.is_file())
    if not data_files:
        raise RuntimeError(f"No dump table files found under {mbdump_dir}")

    from synnodb.duckdb_compat.db_io import list_tables

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    spill_dir.mkdir(parents=True, exist_ok=True)

    # Resume onto an existing .partial so an interrupted load re-uses already-loaded tables.
    tmp_path = duckdb_path.with_name(duckdb_path.name + ".partial")
    con = duckdb.connect(str(tmp_path))
    try:
        con.execute(f"SET memory_limit='{_memory_limit_gb()}GB';")
        con.execute(f"SET threads={_thread_count()};")
        con.execute(f"SET temp_directory='{spill_dir.as_posix()}';")
        con.execute("PRAGMA disable_progress_bar")
        present = set(list_tables(con))

        for data_file in data_files:
            dump_name = data_file.name.lower()
            # A dot in a DuckDB identifier is read as schema.table; dump files from non-default
            # PostgreSQL schemas are qualified, so flatten the dot into the table name.
            table = dump_name.replace(".", "_")
            if table in present:
                print(f"  {table}: already loaded, skipping")
                continue
            load_table(con, table, dump_name, data_file, schema, unescape=unescape)
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        raise
    con.close()
    tmp_path.replace(duckdb_path)
    wal = tmp_path.with_name(tmp_path.name + ".wal")
    wal.unlink(missing_ok=True)


def write_schema_sql(duckdb_path: Path, schema_sql_path: Path) -> None:
    """Write ``CREATE TABLE`` DDL for every table in ``duckdb_path`` to ``schema_sql_path``.

    Reflects the loaded database exactly (names and DuckDB types), read straight from the catalog
    so it never drifts from what was materialized.
    """
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type "
            "FROM duckdb_columns() WHERE schema_name = 'main' "
            "ORDER BY table_name, column_index"
        ).fetchall()
    finally:
        con.close()

    columns: dict[str, list[tuple[str, str]]] = {}
    for table_name, column_name, data_type in rows:
        columns.setdefault(table_name, []).append((column_name, data_type))

    lines = [
        "-- MusicBrainz DuckDB schema.",
        f"-- Generated from {duckdb_path.name}; {len(columns)} tables.",
        "",
    ]
    for table in sorted(columns):
        cols = columns[table]
        body = ",\n".join(f'    "{name}" {dtype}' for name, dtype in cols)
        lines.append(f'CREATE TABLE "{table}" (\n{body}\n);\n')
    schema_sql_path.write_text("\n".join(lines))
    print(f"  wrote schema to {schema_sql_path} ({len(columns)} tables)")


# --------------------------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------------------------- #
def build_musicbrainz_duckdb(
    storage_dir: Path,
    *,
    archives: tuple[str, ...] = DEFAULT_ARCHIVES,
    export: str | None = None,
    schema_ref: str = "master",
    unescape: bool = True,
    keep_archives: bool = True,
) -> Path:
    """Download the MusicBrainz export and load it into ``<storage_dir>/musicbrainz/musicbrainz.duckdb``."""
    root = storage_dir / "musicbrainz"
    downloads = root / "_download"
    extract_dir = root / "_extract"
    spill_dir = root / "_duckdb_spill"
    duckdb_path = root / "musicbrainz.duckdb"
    schema_sql_path = root / "schema.sql"
    downloads.mkdir(parents=True, exist_ok=True)

    if duckdb_path.exists():
        print(f"{duckdb_path} already exists; delete it to rebuild.")
        if not schema_sql_path.exists():
            write_schema_sql(duckdb_path, schema_sql_path)
        return duckdb_path

    export_name = resolve_export_dir(export)
    base_url = f"{FULLEXPORT_URL}/{export_name}"
    print(f"Using MusicBrainz export {export_name}")
    print(f"  {base_url}")

    sums = fetch_sha256sums(base_url)

    print("Downloading archives ...")
    archive_paths: list[Path] = []
    for name in archives:
        fname = f"{name}.tar.bz2"
        dest = downloads / fname
        download_archive(base_url, fname, dest, sums.get(fname))
        archive_paths.append(dest)

    print("Extracting archives ...")
    for archive in archive_paths:
        extract_archive(archive, extract_dir)

    schema_seq = (extract_dir / "SCHEMA_SEQUENCE")
    if schema_seq.exists():
        print(f"Dump SCHEMA_SEQUENCE: {schema_seq.read_text().strip()}")
    print(f"Loading schema DDL from musicbrainz-server@{schema_ref} ...")
    schema = load_schema(schema_ref)
    print(f"  parsed {len(schema)} table definitions")

    mbdump_dir = extract_dir / "mbdump"
    print(f"Loading tables into {duckdb_path} ...")
    load_into_duckdb(
        duckdb_path, mbdump_dir, schema, spill_dir=spill_dir, unescape=unescape
    )

    print("Writing schema.sql ...")
    write_schema_sql(duckdb_path, schema_sql_path)

    if not keep_archives:
        for archive in archive_paths:
            archive.unlink(missing_ok=True)

    print(f"\nDone. MusicBrainz DuckDB written to: {duckdb_path}")
    print(f"Schema written to: {schema_sql_path}")
    return duckdb_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "STORAGE_DIR", "/mnt/labstore/learned_db/synno_data/workloads/"
            )
        ),
        help="Root storage directory; the DB is written to <storage-dir>/musicbrainz/.",
    )
    parser.add_argument(
        "--archives",
        nargs="+",
        default=list(DEFAULT_ARCHIVES),
        help="Archive base names to download and load (without the .tar.bz2 suffix).",
    )
    parser.add_argument(
        "--export",
        default=None,
        help="Pin a specific export directory (e.g. 20260718-002132); default follows LATEST.",
    )
    parser.add_argument(
        "--schema-ref",
        default="master",
        help="git ref of metabrainz/musicbrainz-server to read the schema DDL from.",
    )
    parser.add_argument(
        "--no-unescape",
        action="store_true",
        help="Keep PostgreSQL COPY backslash escapes in text columns instead of decoding them.",
    )
    parser.add_argument(
        "--delete-archives",
        action="store_true",
        help="Delete the downloaded .tar.bz2 archives after a successful load.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    build_musicbrainz_duckdb(
        args.storage_dir,
        archives=tuple(args.archives),
        export=args.export,
        schema_ref=args.schema_ref,
        unescape=not args.no_unescape,
        keep_archives=not args.delete_archives,
    )


if __name__ == "__main__":
    main()
