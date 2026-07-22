"""Materialize the ClickBench ``hits`` dataset (real recorded data, not something a generator can
synthesize - unlike TPC-H's ``dbgen``).

Two steps, both idempotent:

1. :func:`ensure_clickbench_parquet` downloads the official ``hits.parquet`` (100M rows, ~14.8 GB)
   from its public URL to local disk. A plain sequential download, not a streamed
   ``read_parquet('https://...')`` - the host serves every request (including ranged ones) as a
   full ``200 OK`` rather than ``206 Partial Content``, which breaks DuckDB httpfs's footer-seeking
   reads.
2. :func:`ensure_clickbench_duckdb` loads that local parquet into a single-table ``hits.duckdb``,
   using ClickBench's own column typing (the raw parquet stores dates/timestamps as bare integers;
   the upstream ``duckdb/create.sql`` + ``duckdb/load`` scripts in the ClickBench repo are the
   source of truth this mirrors).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import duckdb
import requests

DEFAULT_HITS_URL = "https://datasets.clickhouse.com/hits_compatible/hits.parquet"

# Verbatim from https://github.com/ClickHouse/ClickBench/blob/main/duckdb/create.sql - the typed
# schema the raw parquet (integer-encoded dates/timestamps, no NOT NULL/type fidelity) is loaded
# into.
CREATE_HITS_SQL = """
CREATE TABLE hits
(
    WatchID BIGINT NOT NULL,
    JavaEnable SMALLINT NOT NULL,
    Title TEXT,
    GoodEvent SMALLINT NOT NULL,
    EventTime TIMESTAMP NOT NULL,
    EventDate Date NOT NULL,
    CounterID INTEGER NOT NULL,
    ClientIP INTEGER NOT NULL,
    RegionID INTEGER NOT NULL,
    UserID BIGINT NOT NULL,
    CounterClass SMALLINT NOT NULL,
    OS SMALLINT NOT NULL,
    UserAgent SMALLINT NOT NULL,
    URL TEXT,
    Referer TEXT,
    IsRefresh SMALLINT NOT NULL,
    RefererCategoryID SMALLINT NOT NULL,
    RefererRegionID INTEGER NOT NULL,
    URLCategoryID SMALLINT NOT NULL,
    URLRegionID INTEGER NOT NULL,
    ResolutionWidth SMALLINT NOT NULL,
    ResolutionHeight SMALLINT NOT NULL,
    ResolutionDepth SMALLINT NOT NULL,
    FlashMajor SMALLINT NOT NULL,
    FlashMinor SMALLINT NOT NULL,
    FlashMinor2 TEXT,
    NetMajor SMALLINT NOT NULL,
    NetMinor SMALLINT NOT NULL,
    UserAgentMajor SMALLINT NOT NULL,
    UserAgentMinor VARCHAR(255) NOT NULL,
    CookieEnable SMALLINT NOT NULL,
    JavascriptEnable SMALLINT NOT NULL,
    IsMobile SMALLINT NOT NULL,
    MobilePhone SMALLINT NOT NULL,
    MobilePhoneModel TEXT,
    Params TEXT,
    IPNetworkID INTEGER NOT NULL,
    TraficSourceID SMALLINT NOT NULL,
    SearchEngineID SMALLINT NOT NULL,
    SearchPhrase TEXT,
    AdvEngineID SMALLINT NOT NULL,
    IsArtifical SMALLINT NOT NULL,
    WindowClientWidth SMALLINT NOT NULL,
    WindowClientHeight SMALLINT NOT NULL,
    ClientTimeZone SMALLINT NOT NULL,
    ClientEventTime TIMESTAMP NOT NULL,
    SilverlightVersion1 SMALLINT NOT NULL,
    SilverlightVersion2 SMALLINT NOT NULL,
    SilverlightVersion3 INTEGER NOT NULL,
    SilverlightVersion4 SMALLINT NOT NULL,
    PageCharset TEXT,
    CodeVersion INTEGER NOT NULL,
    IsLink SMALLINT NOT NULL,
    IsDownload SMALLINT NOT NULL,
    IsNotBounce SMALLINT NOT NULL,
    FUniqID BIGINT NOT NULL,
    OriginalURL TEXT,
    HID INTEGER NOT NULL,
    IsOldCounter SMALLINT NOT NULL,
    IsEvent SMALLINT NOT NULL,
    IsParameter SMALLINT NOT NULL,
    DontCountHits SMALLINT NOT NULL,
    WithHash SMALLINT NOT NULL,
    HitColor CHAR NOT NULL,
    LocalEventTime TIMESTAMP NOT NULL,
    Age SMALLINT NOT NULL,
    Sex SMALLINT NOT NULL,
    Income SMALLINT NOT NULL,
    Interests SMALLINT NOT NULL,
    Robotness SMALLINT NOT NULL,
    RemoteIP INTEGER NOT NULL,
    WindowName INTEGER NOT NULL,
    OpenerName INTEGER NOT NULL,
    HistoryLength SMALLINT NOT NULL,
    BrowserLanguage TEXT,
    BrowserCountry TEXT,
    SocialNetwork TEXT,
    SocialAction TEXT,
    HTTPError SMALLINT NOT NULL,
    SendTiming INTEGER NOT NULL,
    DNSTiming INTEGER NOT NULL,
    ConnectTiming INTEGER NOT NULL,
    ResponseStartTiming INTEGER NOT NULL,
    ResponseEndTiming INTEGER NOT NULL,
    FetchTiming INTEGER NOT NULL,
    SocialSourceNetworkID SMALLINT NOT NULL,
    SocialSourcePage TEXT,
    ParamPrice BIGINT NOT NULL,
    ParamOrderID TEXT,
    ParamCurrency TEXT,
    ParamCurrencyID SMALLINT NOT NULL,
    OpenstatServiceName TEXT,
    OpenstatCampaignID TEXT,
    OpenstatAdID TEXT,
    OpenstatSourceID TEXT,
    UTMSource TEXT,
    UTMMedium TEXT,
    UTMCampaign TEXT,
    UTMContent TEXT,
    UTMTerm TEXT,
    FromTag TEXT,
    HasGCLID SMALLINT NOT NULL,
    RefererHash BIGINT NOT NULL,
    URLHash BIGINT NOT NULL,
    CLID INTEGER NOT NULL
);
"""

# The raw parquet stores EventDate/EventTime/ClientEventTime/LocalEventTime as bare integers
# (ClickHouse's native Date/DateTime encoding); this REPLACEs them with the TIMESTAMP/DATE values
# CREATE_HITS_SQL declares. Verbatim from ClickBench's ``duckdb/load``.
_INSERT_HITS_SQL = """
INSERT INTO hits
SELECT * REPLACE (
    make_date(EventDate) AS EventDate,
    epoch_ms(EventTime * 1000) AS EventTime,
    epoch_ms(ClientEventTime * 1000) AS ClientEventTime,
    epoch_ms(LocalEventTime * 1000) AS LocalEventTime)
FROM read_parquet(?, binary_as_string=True)
"""


def _memory_limit_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                return max(1, int(total_kb / 1024 / 1024 * 0.65))
    raise RuntimeError("Could not read MemTotal from /proc/meminfo")


def _thread_count() -> int:
    return os.cpu_count() or 8


def ensure_clickbench_parquet(
    parquet_path: str | Path,
    *,
    url: str = DEFAULT_HITS_URL,
    chunk_size: int = 8 * 1024 * 1024,
) -> Path:
    """Download the official ClickBench ``hits.parquet`` to ``parquet_path`` if not already
    present, verifying the final size against the server's ``Content-Length``.

    Idempotent: a file already matching the expected size is reused. Downloads into a
    ``.partial`` sibling first and only renames into place once complete + size-verified, so an
    interrupted download never leaves a truncated file mistaken for a finished one.

    Plain sequential GET, not a range-seeking reader: the host answers ranged requests with a
    full ``200 OK`` body (not ``206 Partial Content``), so anything relying on real partial
    content (including DuckDB's own ``read_parquet('https://...')``) cannot reliably stream it.
    """
    parquet_path = Path(parquet_path)
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        expected_size = int(resp.headers.get("Content-Length", 0)) or None

        if parquet_path.exists() and (
            expected_size is None or parquet_path.stat().st_size == expected_size
        ):
            print(f"{parquet_path}: present ({parquet_path.stat().st_size:,} bytes), skipping")
            return parquet_path

        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = parquet_path.with_name(parquet_path.name + ".partial")
        print(
            f"{parquet_path}: downloading from {url} "
            f"({expected_size / 1e9:.1f} GB) ..."
            if expected_size
            else f"{parquet_path}: downloading from {url} ..."
        )
        written = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
                written += len(chunk)
                if expected_size:
                    pct = 100 * written / expected_size
                    print(f"\r  {written / 1e9:.2f} / {expected_size / 1e9:.2f} GB ({pct:.1f}%)", end="", flush=True)
        print()

    if expected_size is not None and written != expected_size:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"download of {url} truncated: got {written} bytes, expected {expected_size}"
        )
    tmp_path.replace(parquet_path)
    return parquet_path


def ensure_clickbench_duckdb(
    duckdb_path: str | Path,
    parquet_path: str | Path,
    *,
    spill_dir: str | Path | None = None,
) -> Path:
    """Materialize a single-table ``hits.duckdb`` from a local ``hits.parquet`` (see
    :func:`ensure_clickbench_parquet`), applying ClickBench's own column typing.

    Idempotent: reused if the ``hits`` table already exists. Builds into a ``.partial`` sibling
    and only renames into place after a successful ``CHECKPOINT``, so an interrupted build never
    leaves a half-populated file that a later call would try (and fail) to ``CREATE TABLE`` into
    again.
    """
    from synnodb.duckdb_compat.db_io import list_tables

    duckdb_path = Path(duckdb_path)
    if duckdb_path.exists():
        con = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            present = set(list_tables(con))
        finally:
            con.close()
        if "hits" in present:
            print(f"{duckdb_path}: present (hits table), reusing")
            return duckdb_path

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    spill = (Path(spill_dir) if spill_dir else duckdb_path.parent) / "_duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    tmp_path = duckdb_path.with_name(duckdb_path.name + ".partial")
    tmp_wal = tmp_path.with_name(tmp_path.name + ".wal")
    tmp_path.unlink(missing_ok=True)
    tmp_wal.unlink(missing_ok=True)
    print(f"{duckdb_path}: loading {parquet_path} into a typed hits table ...")
    con = duckdb.connect(str(tmp_path))
    try:
        con.execute(f"SET memory_limit='{_memory_limit_gb()}GB';")
        con.execute(f"SET threads={_thread_count()};")
        con.execute(f"SET temp_directory='{spill.as_posix()}';")
        con.execute("PRAGMA disable_progress_bar")
        con.execute(CREATE_HITS_SQL)
        con.execute(_INSERT_HITS_SQL, [str(Path(parquet_path).resolve())])
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        tmp_wal.unlink(missing_ok=True)
        shutil.rmtree(spill, ignore_errors=True)
        raise
    con.close()
    shutil.rmtree(spill, ignore_errors=True)
    tmp_wal.unlink(missing_ok=True)
    tmp_path.replace(duckdb_path)
    return duckdb_path
