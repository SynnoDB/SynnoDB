#!/usr/bin/env python3
"""HTTP service wrapper for run_generated_code.

Starts a long-running REST API server that exposes one endpoint per query:

  GET /                          -> JSON: list all queries with placeholder names + example URLs
  GET /run/<query_id>?p1=v1...            -> run the query, return result CSV as text/csv
  GET /sql/<query_id>                     -> JSON: {template} – SQL with [PLACEHOLDER] markers
  GET /sql/<query_id>?p1=v1...           -> JSON: {template, assembled} – also fills in placeholders
  GET /code/<query_id>                    -> JSON: {files:[{name, content}]} – generated C++ source (queryX.hpp/.cpp)
  GET /run_engine/<engine>/<query_id>?.. -> JSON: {csv, time_ms} – run assembled SQL against DuckDB, Umbra, or Bespoke

The Bespoke binary is compiled and served by a separate bespoke_service.py process.

Example:
  python bespoke_service.py tpch --wandb_snapshot <hash> --port 7657
  python run_generated_code_service.py tpch --bespoke http://127.0.0.1:7657 --port 8080
  curl "http://localhost:8080/"
  curl "http://localhost:8080/run/1?date=1998-09-01"
"""

import argparse
import base64
import datetime
import hmac
import http.server
import json
import logging
import os
import random
import re
import socket
import socketserver
import sqlite3
import ssl

# add parent to path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv

from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from synnodb.workloads.dataset.dataset_tables_dict import get_dataset_name
from synnodb.workloads.dataset.query_gen_factory import get_query_gen

try:
    import geoip2.database
    import geoip2.errors
except ImportError:  # Optional; deploy.sh installs it for the demo stack.
    geoip2 = None  # type: ignore

sys.path.append(Path(__file__).parent.parent.parent.as_posix())

from synnodb.observability.benchmark.run import get_all_query_ids
from synnodb.observability.logging.logger import setup_logging
from synnodb.observability.ui_template_runner.service_notify import (
    notify_5xx_response,
    notify_service_crash,
)
from synnodb.observability.ui_template_runner.ui_handler import handle_static

MAX_PLACEHOLDER_VALUE_LEN = 128
# Evict stale per-IP rate-limit windows once the table exceeds this many entries.
_IP_WINDOW_MAX_ENTRIES = 10000

_SAFE_STRING_RE = re.compile(r"^[A-Za-z0-9_ .:/#-]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


setup_logging(logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global service state – populated once by init_service()
# ---------------------------------------------------------------------------
class _State:
    benchmark: str = None  # type: ignore
    sf: float = None  # type: ignore
    query_ids: list[str] = None  # type: ignore
    # Maps query_id -> ordered dict {placeholder_name: sample_value}
    placeholder_names: dict[str, dict] = {}
    # Maps query_id -> SQL template string (with [PLACEHOLDER] markers)
    sql_templates: dict[str, str] = {}
    code_metadata: dict = {}
    # Engines
    bespoke_url: str | None = (
        None  # base URL of the running bespoke_service.py, e.g. "http://127.0.0.1:7657"
    )
    bespoke_profiled_url: str | None = (
        None  # base URL of the profiled bespoke_service.py (--trace), e.g. "http://127.0.0.1:7658"
    )
    duckdb_con: DuckDBConnectionManager | None = None  # noqa: F821
    umbra_url: str | None = (
        None  # base URL of the running umbra_service.py, e.g. "http://127.0.0.1:7655"
    )
    # Only DuckDB runs in-process and needs a lock; the bespoke/umbra/clickhouse
    # services each serialize requests internally.
    duckdb_lock: threading.Lock = threading.Lock()
    clickhouse_url: str | None = (
        None  # base URL of the running clickhouse_service.py, e.g. "http://127.0.0.1:7656"
    )
    rate_limit_rpm: int = 60
    ip_window: dict[str, tuple[int, float]] = {}
    ip_window_lock: threading.Lock = threading.Lock()
    # Telemetry
    telemetry_db: str | None = None
    geo_cache: dict = {}
    geo_cache_lock: threading.Lock = threading.Lock()
    geoip_reader = None
    geo_lookup_enabled: bool = False
    # Dashboard auth
    dashboard_password: str | None = None


STATE = _State()


# ---------------------------------------------------------------------------
# Telemetry DB helpers
# ---------------------------------------------------------------------------


def _init_telemetry_db(path: str) -> None:
    con = sqlite3.connect(path, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS query_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       REAL    NOT NULL,
        run_id   TEXT    NOT NULL DEFAULT '',
        client_ip TEXT   NOT NULL,
        user_agent TEXT  NOT NULL DEFAULT '',
        query_id TEXT    NOT NULL,
        placeholders TEXT NOT NULL,
        engine   TEXT    NOT NULL,
        time_ms  REAL    NOT NULL,
        sf       REAL    NOT NULL,
        error    TEXT
    )""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(query_events)").fetchall()}
    if "run_id" not in cols:
        con.execute(
            "ALTER TABLE query_events ADD COLUMN run_id TEXT NOT NULL DEFAULT ''"
        )
    if "error" not in cols:
        con.execute("ALTER TABLE query_events ADD COLUMN error TEXT")
    if "user_agent" not in cols:
        con.execute(
            "ALTER TABLE query_events ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''"
        )
    con.execute("CREATE INDEX IF NOT EXISTS idx_qe_ts ON query_events(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_qe_run_id ON query_events(run_id)")
    con.execute("""CREATE TABLE IF NOT EXISTS geo_cache (
        ip           TEXT PRIMARY KEY,
        country      TEXT,
        country_code TEXT,
        region       TEXT,
        city         TEXT,
        lat          REAL,
        lon          REAL,
        cached_at    REAL NOT NULL
    )""")
    con.commit()
    con.close()


_MAX_UA_LEN = 512


def _log_query_event(
    run_id: str,
    ip: str,
    qid: str,
    ph: dict,
    engine: str,
    ms: float,
    sf: float,
    error: str | None = None,
    user_agent: str = "",
) -> None:
    if STATE.telemetry_db is None:
        return
    try:
        con = sqlite3.connect(STATE.telemetry_db, timeout=5)
        con.execute(
            "INSERT INTO query_events (ts, run_id, client_ip, user_agent, query_id, placeholders, engine, time_ms, sf, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                run_id,
                ip,
                user_agent[:_MAX_UA_LEN],
                qid,
                json.dumps(ph),
                engine,
                ms,
                float(sf),
                error,
            ),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning("Telemetry write failed: %s", exc)
    # Kick off background local GeoIP lookup for new IPs (non-blocking).
    if STATE.geo_lookup_enabled and not _is_local_ip(ip):
        with STATE.geo_cache_lock:
            already_cached = ip in STATE.geo_cache
        if not already_cached:
            threading.Thread(
                target=_fetch_geo_background, args=(ip,), daemon=True
            ).start()


_GEO_CACHE_TTL_S = 86400  # 24 h
_GEO_CACHE_MAX_ENTRIES = 10000


def _store_geo_cache(ip: str, geo: dict) -> None:
    """Cache *geo* for *ip* in memory, bounding the cache to avoid unbounded growth."""
    with STATE.geo_cache_lock:
        if ip not in STATE.geo_cache and len(STATE.geo_cache) >= _GEO_CACHE_MAX_ENTRIES:
            # Evict the oldest insertion (dict preserves insertion order).
            STATE.geo_cache.pop(next(iter(STATE.geo_cache)), None)
        STATE.geo_cache[ip] = geo


def _is_local_ip(ip: str) -> bool:
    return ip.startswith(
        ("127.", "10.", "192.168.", "172.16.", "172.17.", "::1", "localhost")
    )


def _load_geoip_reader() -> None:
    db_path = Path(__file__).parent / "output" / "GeoLite2-City.mmdb"
    if geoip2 is None:
        logger.warning("geoip2 package not installed; local IP geolocation disabled.")
        return
    if not db_path.exists():
        logger.info(
            "GeoIP database not found at %s; local IP geolocation disabled.", db_path
        )
        return
    try:
        STATE.geoip_reader = geoip2.database.Reader(db_path.as_posix())
        STATE.geo_lookup_enabled = True
        logger.info("Local IP geolocation enabled: %s", db_path)
    except Exception as exc:
        logger.warning("Could not open GeoIP database %s: %s", db_path, exc)


def _lookup_geo_local(ip: str) -> dict | None:
    if STATE.geoip_reader is None:
        return None
    try:
        record = STATE.geoip_reader.city(ip)
    except geoip2.errors.AddressNotFoundError:
        return {
            "country": "Unknown",
            "country_code": "",
            "region": "",
            "city": "",
            "lat": None,
            "lon": None,
        }
    except Exception as exc:
        logger.debug("Local GeoIP lookup failed for %s: %s", ip, exc)
        return None

    country = record.country.name or record.registered_country.name or ""
    country_code = record.country.iso_code or record.registered_country.iso_code or ""
    return {
        "country": country,
        "country_code": country_code,
        "region": record.subdivisions.most_specific.name or "",
        "city": record.city.name or "",
        "lat": record.location.latitude,
        "lon": record.location.longitude,
    }


def _geolocate_ip(ip: str) -> dict:
    """Return cached geo dict for *ip* (non-blocking; returns placeholder while resolving)."""
    if _is_local_ip(ip):
        return {
            "country": "Local",
            "country_code": "",
            "region": "",
            "city": "local network",
            "lat": None,
            "lon": None,
        }
    if not STATE.geo_lookup_enabled:
        return {
            "country": "Not collected",
            "country_code": "",
            "region": "",
            "city": "",
            "lat": None,
            "lon": None,
        }
    with STATE.geo_cache_lock:
        if ip in STATE.geo_cache:
            return STATE.geo_cache[ip]
    if STATE.telemetry_db:
        try:
            con = sqlite3.connect(STATE.telemetry_db, timeout=2)
            row = con.execute(
                "SELECT country, country_code, region, city, lat, lon FROM geo_cache "
                "WHERE ip=? AND cached_at > ?",
                (ip, time.time() - _GEO_CACHE_TTL_S),
            ).fetchone()
            con.close()
            if row:
                geo = {
                    "country": row[0],
                    "country_code": row[1],
                    "region": row[2],
                    "city": row[3],
                    "lat": row[4],
                    "lon": row[5],
                }
                _store_geo_cache(ip, geo)
                return geo
        except Exception:
            pass
    if STATE.geoip_reader is not None:
        threading.Thread(target=_fetch_geo_background, args=(ip,), daemon=True).start()
    return {
        "country": "resolving…",
        "country_code": "",
        "region": "",
        "city": "",
        "lat": None,
        "lon": None,
    }


def _fetch_geo_background(ip: str) -> None:
    """Resolve geolocation locally from the MaxMind database and cache result."""
    try:
        geo = _lookup_geo_local(ip)
        if geo is None:
            return
        if STATE.telemetry_db:
            con = sqlite3.connect(STATE.telemetry_db, timeout=5)
            con.execute(
                "INSERT OR REPLACE INTO geo_cache "
                "(ip, country, country_code, region, city, lat, lon, cached_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ip,
                    geo["country"],
                    geo["country_code"],
                    geo["region"],
                    geo["city"],
                    geo["lat"],
                    geo["lon"],
                    time.time(),
                ),
            )
            con.commit()
            con.close()
        _store_geo_cache(ip, geo)
    except Exception as exc:
        logger.debug("Geo lookup failed for %s: %s", ip, exc)


def _infer_sample_type(sample_value: object) -> str:
    if isinstance(sample_value, bool):
        return "bool"
    if isinstance(sample_value, int):
        return "int"
    if isinstance(sample_value, float):
        return "float"
    if isinstance(sample_value, str):
        if _DATE_RE.fullmatch(sample_value):
            return "date"
        if _INT_RE.fullmatch(sample_value):
            return "int"
        if _FLOAT_RE.fullmatch(sample_value):
            return "float"
        return "string"
    return "string"


def _validate_placeholder_value(name: str, raw_value: str, sample_value: object) -> str:
    if len(raw_value) > MAX_PLACEHOLDER_VALUE_LEN:
        raise ValueError(
            f"Placeholder '{name}' exceeds max length ({MAX_PLACEHOLDER_VALUE_LEN})."
        )
    if "\n" in raw_value or "\r" in raw_value or "\x00" in raw_value:
        raise ValueError(f"Placeholder '{name}' contains forbidden control characters.")

    inferred = _infer_sample_type(sample_value)
    if inferred == "int":
        if not _INT_RE.fullmatch(raw_value):
            raise ValueError(f"Placeholder '{name}' must be an integer.")
        return raw_value
    if inferred == "float":
        if not _FLOAT_RE.fullmatch(raw_value):
            raise ValueError(f"Placeholder '{name}' must be a number.")
        return raw_value
    if inferred == "date":
        if not _DATE_RE.fullmatch(raw_value):
            raise ValueError(f"Placeholder '{name}' must match YYYY-MM-DD.")
        # Format alone is not enough: reject well-formed but non-existent dates
        # (e.g. 1993-01-35, 1993-02-29) up front, so every engine sees valid
        # input. Without this, DuckDB/Umbra raise their own errors while the
        # bespoke engine silently rolls the date over and returns a bogus result.
        try:
            datetime.date.fromisoformat(raw_value)
        except ValueError:
            raise ValueError(
                f"Placeholder '{name}' is not a valid calendar date: '{raw_value}'."
            ) from None
        return raw_value

    # Strict allowlist for free-form string placeholders to reduce SQL injection risk.
    if not _SAFE_STRING_RE.fullmatch(raw_value):
        raise ValueError(f"Placeholder '{name}' contains unsafe characters.")
    lowered = raw_value.lower()
    for token in ("--", "/*", "*/", ";"):
        if token in lowered:
            raise ValueError(
                f"Placeholder '{name}' contains forbidden SQL token '{token}'."
            )
    return raw_value


def _normalise_run_id(raw_value: object | None) -> str:
    if raw_value is None:
        return uuid.uuid4().hex
    run_id = str(raw_value).strip()
    if not _RUN_ID_RE.fullmatch(run_id):
        return uuid.uuid4().hex
    return run_id


def _discover_query_metadata(
    benchmark: str, query_ids: list[str]
) -> tuple[dict[str, dict], dict[str, str]]:
    """Call each query generator once (seed=42) to learn placeholder names and SQL templates."""
    gen_query_fn = get_query_gen(benchmark)
    rnd = random.Random(42)
    placeholder_names: dict[str, dict] = {}
    sql_templates: dict[str, str] = {}
    for query_id in query_ids:
        template, _, placeholders = gen_query_fn(query_name=f"Q{query_id}", rnd=rnd)
        placeholder_names[query_id] = (
            placeholders  # preserves insertion order (Python 3.7+)
        )
        sql_templates[query_id] = template
    return placeholder_names, sql_templates


def init_service(args) -> None:
    """Discover query metadata and connect to external engine services."""
    sf = args.sf
    sf = float(sf) if "." in str(sf) else int(sf)

    query_ids = get_all_query_ids(args.benchmark)
    placeholder_names, sql_templates = _discover_query_metadata(
        args.benchmark, query_ids
    )

    STATE.benchmark = args.benchmark
    STATE.sf = sf
    STATE.query_ids = query_ids
    STATE.placeholder_names = placeholder_names
    STATE.sql_templates = sql_templates
    STATE.rate_limit_rpm = args.rate_limit_rpm
    load_dotenv()
    _load_geoip_reader()
    STATE.dashboard_password = os.environ.get("DASHBOARD_PASSWORD") or None
    if STATE.dashboard_password:
        logger.info("Dashboard password protection enabled.")
    else:
        logger.warning("DASHBOARD_PASSWORD not set – /dashboard is unprotected.")

    meta_path = Path(__file__).parent / "output" / "code_metadata.json"
    if meta_path.exists():
        try:
            STATE.code_metadata = json.loads(meta_path.read_text())
            logger.info("Loaded code metadata: %s", STATE.code_metadata)
        except Exception as exc:
            logger.warning("Could not load code_metadata.json: %s", exc)

    db_path = Path(__file__).parent / "output" / "telemetry.db"
    db_path.parent.mkdir(exist_ok=True)
    STATE.telemetry_db = str(db_path)
    _init_telemetry_db(STATE.telemetry_db)
    logger.info("Telemetry DB: %s", STATE.telemetry_db)

    if args.bespoke:
        STATE.bespoke_url = args.bespoke.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{STATE.bespoke_url}/health", timeout=5
            ) as resp:
                health = json.loads(resp.read())
            assert health.get("status") == "ok"
            logger.info("Connected to Bespoke service at %s", STATE.bespoke_url)
        except Exception as exc:
            logger.warning(
                "Bespoke service not reachable at %s (%s) – will retry on each request. "
                "Start it with: python bespoke_service.py %s --sf %s",
                STATE.bespoke_url,
                exc,
                args.benchmark,
                sf,
            )

    if args.bespoke_profiled:
        STATE.bespoke_profiled_url = args.bespoke_profiled.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{STATE.bespoke_profiled_url}/health", timeout=5
            ) as resp:
                health = json.loads(resp.read())
            assert health.get("status") == "ok"
            logger.info(
                "Connected to profiled Bespoke service at %s",
                STATE.bespoke_profiled_url,
            )
        except Exception as exc:
            logger.warning(
                "Profiled Bespoke service not reachable at %s (%s) – will retry on each request. "
                "Start it with: python bespoke_service.py %s --sf %s --trace --workspace-dir <copy>",
                STATE.bespoke_profiled_url,
                exc,
                args.benchmark,
                sf,
            )

    parquet_path = (
        Path(args.base_parquet_dir) / f"{get_dataset_name(args.benchmark)}_parquet"
    )
    assert parquet_path.exists(), f"Parquet directory not found: {parquet_path}"

    if args.duckdb:
        logger.info("Initializing DuckDB runner (loading tables into memory)…")
        STATE.duckdb_con = DuckDBConnectionManager(
            pre_load_duckdb_tables=True,
            parquet_path=parquet_path.as_posix(),
            sf=sf,
            pin_worker=True,
            pin_core=5,
            benchmark=args.benchmark,
        )
        logger.info("DuckDB ready.")

    if args.umbra:
        STATE.umbra_url = args.umbra.rstrip("/")
        try:
            with urllib.request.urlopen(f"{STATE.umbra_url}/health", timeout=5) as resp:
                health = json.loads(resp.read())
            assert health.get("status") == "ok"
            logger.info("Connected to Umbra service at %s", STATE.umbra_url)
        except Exception as exc:
            logger.warning(
                "Umbra service not reachable at %s (%s) – will retry on each request. "
                "Start it with: python umbra_service.py %s --sf %s",
                STATE.umbra_url,
                exc,
                args.benchmark,
                sf,
            )

    if args.clickhouse:
        STATE.clickhouse_url = args.clickhouse.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{STATE.clickhouse_url}/health", timeout=5
            ) as resp:
                health = json.loads(resp.read())
            assert health.get("status") == "ok"
            logger.info("Connected to ClickHouse service at %s", STATE.clickhouse_url)
        except Exception as exc:
            logger.warning(
                "ClickHouse service not reachable at %s (%s) – will retry on each request. "
                "Start it with: python clickhouse_service.py %s --sf %s",
                STATE.clickhouse_url,
                exc,
                args.benchmark,
                sf,
            )

    # Log the auto-generated API surface
    for qid in query_ids:
        ph = placeholder_names[qid]
        param_str = "&".join(f"{k}={v}" for k, v in ph.items())
        logger.info("  GET /run/%s?%s", qid, param_str)


_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0


class EngineQueryError(Exception):
    """Raised when a baseline engine rejects a query (e.g. invalid date / SQL).

    Carries the engine's own error message. This reflects bad *input*, not a
    server fault, so it is surfaced to the client as a 400 rather than a generic
    500 "Internal server error".
    """


def _post_with_retry(
    url: str, payload: bytes, engine: str, query_id: str
) -> dict | None:
    """POST JSON payload to url and return the parsed JSON response.

    Two failure modes are distinguished:

    * The service *responded* with an HTTP error status (e.g. a 500 from a bad
      query). The response body is parsed and returned as-is so the caller can
      surface the engine's own error message. This is NOT retried — the service
      is up. (``HTTPError`` must be caught before ``URLError``, as it subclasses
      it; otherwise a clean 500 looks identical to the service being down.)
    * The service is genuinely unreachable (connection refused, timeout, DNS).
      These are retried up to ``_RETRY_ATTEMPTS`` times; ``None`` is returned if
      every attempt fails.
    """
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # The service answered with an error status — it is reachable, so do
            # not retry. Return its body verbatim when it carries an ``error``
            # field, otherwise synthesise one from the status line.
            try:
                data = json.loads(exc.read())
                if isinstance(data, dict) and "error" in data:
                    return data
                return {"error": f"{engine} service returned HTTP {exc.code}: {data}"}
            except Exception:
                return {
                    "error": f"{engine} service returned HTTP {exc.code} ({exc.reason})"
                }
        except (urllib.error.URLError, OSError) as exc:
            if attempt < _RETRY_ATTEMPTS:
                logger.warning(
                    "%s service unreachable for Q%s (attempt %d/%d), retrying in %.0fs: %s",
                    engine,
                    query_id,
                    attempt,
                    _RETRY_ATTEMPTS,
                    _RETRY_DELAY_S,
                    exc,
                )
                time.sleep(_RETRY_DELAY_S)
            else:
                logger.error(
                    "%s service unreachable for Q%s after %d attempts: %s",
                    engine,
                    query_id,
                    _RETRY_ATTEMPTS,
                    exc,
                )
    return None


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
class _QueryHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # redirect to our logger
        logger.debug("HTTP %s - " + fmt, self.client_address[0], *args)

    # ------------------------------------------------------------------ helpers
    def _send(
        self, code: int, body: bytes, mime: str, extra_headers: dict | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict) -> None:
        self._send(
            code, json.dumps(data, default=str, indent=2).encode(), "application/json"
        )
        if code >= 500:
            notify_5xx_response("frontend", self.path, code, data)

    def _send_text(self, code: int, text: str, mime: str = "text/plain") -> None:
        self._send(code, text.encode(), mime)

    def _send_internal_error(self, request_id: str) -> None:
        self._send_json(
            500, {"error": "Internal server error", "request_id": request_id}
        )

    def _rate_limit_ok(self) -> bool:
        now = time.time()
        # Key on the raw socket peer rather than X-Forwarded-For (used for
        # telemetry): XFF is client-spoofable and must not be trusted for rate
        # limiting. Behind a trusted reverse proxy this would need revisiting.
        ip = self.client_address[0] if self.client_address else "unknown"
        with STATE.ip_window_lock:
            # Opportunistically drop stale windows so this table can't grow
            # without bound on a public endpoint.
            if len(STATE.ip_window) > _IP_WINDOW_MAX_ENTRIES:
                cutoff = now - 60
                for stale_ip in [
                    k for k, (_, ws) in STATE.ip_window.items() if ws < cutoff
                ]:
                    del STATE.ip_window[stale_ip]
            count, window_start = STATE.ip_window.get(ip, (0, now))
            if now - window_start >= 60:
                count = 0
                window_start = now
            count += 1
            STATE.ip_window[ip] = (count, window_start)
            return count <= STATE.rate_limit_rpm

    def _extract_placeholders(
        self, query_id: str, params: dict
    ) -> tuple[dict, list[str], str | None]:
        placeholder_template = STATE.placeholder_names.get(query_id, {})
        placeholders: dict = {}
        missing: list[str] = []

        for key, sample_value in placeholder_template.items():
            val = params.get(key)
            if val is None:
                missing.append(key)
                continue
            try:
                placeholders[key] = _validate_placeholder_value(key, val, sample_value)
            except ValueError as exc:
                return {}, [], str(exc)
        return placeholders, missing, None

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        if not self._rate_limit_ok():
            self._send_json(429, {"error": "Rate limit exceeded"})
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/":
            self._handle_index()
        elif path == "/dashboard":
            self._handle_dashboard()
        elif path == "/api/telemetry":
            self._handle_api_telemetry(params)
        elif path == "/api/logs":
            self._handle_api_logs(params)
        elif handle_static(path, self):
            pass
        elif path.startswith("/run_engine/"):
            parts = path[len("/run_engine/") :].split("/", 1)
            if len(parts) == 2:
                self._handle_run_engine(parts[0], parts[1], params)
            else:
                self._send_json(
                    404, {"error": "Expected /run_engine/<engine>/<query_id>"}
                )
        elif path.startswith("/run_profiled/"):
            query_id = path[len("/run_profiled/") :]
            self._handle_run(query_id, params, profiled=True)
        elif path.startswith("/run/"):
            query_id = path[len("/run/") :]
            self._handle_run(query_id, params)
        elif path.startswith("/sql/"):
            query_id = path[len("/sql/") :]
            self._handle_sql(query_id, params)
        elif path.startswith("/code/"):
            query_id = path[len("/code/") :]
            self._handle_code(query_id)
        else:
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    # ------------------------------------------------------------------ helpers (cont.)
    def _get_client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return xff if xff else self.client_address[0]

    def _get_user_agent(self) -> str:
        return self.headers.get("User-Agent", "") or ""

    def _require_auth(self) -> bool:
        """Return True if the request passes dashboard Basic Auth (or no password is set)."""
        if STATE.dashboard_password is None:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, provided = (
                    base64.b64decode(auth[6:])
                    .decode("utf-8", errors="replace")
                    .partition(":")
                )
                if hmac.compare_digest(provided, STATE.dashboard_password):
                    return True
            except Exception:
                pass
        body = b"Unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="BespokeOLAP Telemetry"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _handle_dashboard(self) -> None:
        if not self._require_auth():
            return
        p = Path(__file__).parent / "telemetry" / "dashboard.html"
        if p.exists():
            self._send(
                200,
                p.read_bytes(),
                "text/html; charset=utf-8",
                extra_headers={"Cache-Control": "no-store, must-revalidate"},
            )
        else:
            self._send_json(404, {"error": "dashboard.html not found"})

    def _handle_api_telemetry(self, params: dict) -> None:
        if not self._require_auth():
            return
        if STATE.telemetry_db is None:
            self._send_json(503, {"error": "Telemetry DB not initialised"})
            return
        limit = min(int(params.get("limit", 2000)), 10000)
        try:
            con = sqlite3.connect(STATE.telemetry_db, timeout=5)
            rows = con.execute(
                "SELECT id, ts, run_id, client_ip, user_agent, query_id, placeholders, engine, time_ms, sf, error "
                "FROM query_events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            ips = list({r[3] for r in rows})
            geo_map: dict = {}
            if ips:
                ph_sql = ",".join("?" * len(ips))
                for row in con.execute(
                    f"SELECT ip, country, country_code, region, city, lat, lon "
                    f"FROM geo_cache WHERE ip IN ({ph_sql})",
                    ips,
                ).fetchall():
                    geo_map[row[0]] = {
                        "country": row[1],
                        "country_code": row[2],
                        "region": row[3],
                        "city": row[4],
                        "lat": row[5],
                        "lon": row[6],
                    }
            con.close()
        except Exception as exc:
            logger.exception("Telemetry API DB error")
            self._send_json(500, {"error": str(exc)})
            return

        events = [
            {
                "id": event_id,
                "ts": ts,
                "run_id": run_id or f"legacy-{event_id}",
                "client_ip": ip,
                "user_agent": ua,
                "geo": geo_map.get(ip) or _geolocate_ip(ip),
                "query_id": qid,
                "placeholders": json.loads(ph),
                "engine": engine,
                "time_ms": ms,
                "sf": sf,
                "error": error,
            }
            for event_id, ts, run_id, ip, ua, qid, ph, engine, ms, sf, error in rows
        ]
        self._send_json(
            200,
            {
                "events": events,
                "deployment": {
                    **STATE.code_metadata,
                    "benchmark": STATE.benchmark,
                    "sf": STATE.sf,
                    "engines_enabled": {
                        "bespoke": STATE.bespoke_url is not None,
                        "bespoke_profiled": STATE.bespoke_profiled_url is not None,
                        "duckdb": STATE.duckdb_con is not None,
                        "umbra": STATE.umbra_url is not None,
                        "clickhouse": STATE.clickhouse_url is not None,
                    },
                },
            },
        )

    _LOG_SERVICES = ("ui", "bespoke", "bespoke_profiled", "umbra", "telemetry", "clickhouse")
    _LOG_TAIL_BYTES = 256 * 1024  # cap response size at 256 KB

    def _handle_api_logs(self, params: dict) -> None:
        if not self._require_auth():
            return
        service = params.get("service", "")
        if service not in self._LOG_SERVICES:
            self._send_json(
                400,
                {
                    "error": f"Unknown service '{service}'. Allowed: {list(self._LOG_SERVICES)}"
                },
            )
            return
        log_dir = Path(os.path.expanduser("~/bespoke_olap/webrunner_logs"))
        if not log_dir.is_dir():
            self._send_json(404, {"error": f"Log directory not found: {log_dir}"})
            return
        # filenames are `<DATETIME>_<service>.log`, where DATETIME sorts lexicographically.
        matches = sorted(log_dir.glob(f"*_{service}.log"))
        if not matches:
            self._send_json(404, {"error": f"No log files for service '{service}'"})
            return
        latest = matches[-1]
        try:
            size = latest.stat().st_size
            with latest.open("rb") as f:
                if size > self._LOG_TAIL_BYTES:
                    f.seek(size - self._LOG_TAIL_BYTES)
                    # discard partial first line
                    f.readline()
                content = f.read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.exception("Failed to read log file")
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(
            200,
            {
                "service": service,
                "file": latest.name,
                "size_bytes": size,
                "truncated": size > self._LOG_TAIL_BYTES,
                "content": content,
            },
        )

    # ------------------------------------------------------------------ handlers
    def _handle_index(self):
        queries = {}
        for qid in STATE.query_ids:
            ph = STATE.placeholder_names.get(qid, {})
            param_str = "&".join(f"{k}={v}" for k, v in ph.items())
            queries[f"q{qid}"] = {
                "url": f"/run/{qid}" + (f"?{param_str}" if param_str else ""),
                "placeholders": {k: str(v) for k, v in ph.items()},
            }
        self._send_json(
            200,
            {
                "benchmark": STATE.benchmark,
                "sf": STATE.sf,
                "queries": queries,
                "engines": {
                    "bespoke": STATE.bespoke_url is not None,
                    "bespoke_profiled": STATE.bespoke_profiled_url is not None,
                    "duckdb": STATE.duckdb_con is not None,
                    "umbra": STATE.umbra_url is not None,
                    "clickhouse": STATE.clickhouse_url is not None,
                },
                "code_metadata": STATE.code_metadata,
            },
        )

    def _handle_sql(self, query_id: str, params: dict):
        if query_id not in STATE.query_ids:
            self._send_json(404, {"error": f"Unknown query_id: '{query_id}'"})
            return
        template = STATE.sql_templates.get(query_id, "")
        response = {"template": template}
        if params:
            assembled = template
            placeholders, missing, validation_error = self._extract_placeholders(
                query_id, params
            )
            if validation_error is not None:
                self._send_json(400, {"error": validation_error})
                return
            if missing:
                self._send_json(400, {"error": f"Missing placeholder(s): {missing}"})
                return
            for key, value in placeholders.items():
                assembled = assembled.replace(f"[{key.upper()}]", value)
            response["assembled"] = assembled
        self._send_json(200, response)

    def _handle_code(self, query_id: str):
        """Return the generated C++ source (queryX.cpp + queryX.hpp) for a query.

        query_id is validated against the allowlist before building any path,
        so it cannot be used for directory traversal.
        """
        if query_id not in STATE.query_ids:
            self._send_json(404, {"error": f"Unknown query_id: '{query_id}'"})
            return
        out_dir = Path(__file__).parent / "output"
        files = []
        # Implementation (.cpp) first – it is the interesting one; the .hpp is
        # just the function signature.
        for suffix in (".cpp", ".hpp"):
            fpath = out_dir / f"query{query_id}{suffix}"
            if not fpath.exists():
                continue
            try:
                files.append({"name": fpath.name, "content": fpath.read_text()})
            except Exception as exc:
                logger.warning("Could not read source file %s: %s", fpath, exc)
        if not files:
            self._send_json(
                404, {"error": f"No source files found for query '{query_id}'"}
            )
            return
        self._send_json(200, {"query_id": query_id, "files": files})

    def _handle_run(self, query_id: str, params: dict, profiled: bool = False):
        # The profiled variant targets the separate --trace bespoke instance and
        # additionally returns a per-section profile breakdown.
        engine_url = STATE.bespoke_profiled_url if profiled else STATE.bespoke_url
        engine_name = "bespoke_profiled" if profiled else "bespoke"
        run_id = _normalise_run_id(params.get("run_id"))
        log_params = dict(params)
        log_params.pop("run_id", None)
        ip = self._get_client_ip()
        ua = self._get_user_agent()
        if query_id not in STATE.query_ids:
            self._send_json(
                404,
                {
                    "error": f"Unknown query_id: '{query_id}'",
                    "available": STATE.query_ids,
                },
            )
            return

        placeholder_template = STATE.placeholder_names.get(query_id, {})
        placeholders, missing, validation_error = self._extract_placeholders(
            query_id, params
        )
        if validation_error is not None:
            _log_query_event(
                run_id,
                ip,
                query_id,
                log_params,
                engine_name,
                0.0,
                STATE.sf,
                error=validation_error,
                user_agent=ua,
            )
            self._send_json(400, {"error": validation_error})
            return

        if missing:
            ph = placeholder_template
            example_url = "/run/{}?{}".format(
                query_id, "&".join(f"{k}={v}" for k, v in ph.items())
            )
            _log_query_event(
                run_id,
                ip,
                query_id,
                log_params,
                engine_name,
                0.0,
                STATE.sf,
                error=f"Missing placeholder(s): {missing}",
                user_agent=ua,
            )
            self._send_json(
                400,
                {
                    "error": f"Missing placeholder(s): {missing}",
                    "expected_params": list(ph.keys()),
                    "example_values": {k: str(v) for k, v in ph.items()},
                    "example_url": example_url,
                },
            )
            return

        if engine_url is None:
            flag = "--bespoke_profiled" if profiled else "--bespoke"
            self._send_json(
                503,
                {
                    "error": f"Bespoke service not configured. Start service with {flag} <url>."
                },
            )
            return

        payload = json.dumps(
            {
                "run_id": run_id,
                "query_id": query_id,
                "placeholders": placeholders,
                "sf": STATE.sf,
            }
        ).encode()
        result = _post_with_retry(
            f"{engine_url}/query", payload, engine_name, query_id
        )
        if result is None:
            _log_query_event(
                run_id,
                ip,
                query_id,
                placeholders,
                engine_name,
                0.0,
                STATE.sf,
                error="Bespoke service not responding after retries",
                user_agent=ua,
            )
            self._send_json(
                503, {"error": "Bespoke service not responding after retries"}
            )
            return
        if "error" in result:
            request_id = f"run-{int(time.time() * 1000)}"
            logger.error("Bespoke service error for Q%s: %s", query_id, result["error"])
            _log_query_event(
                run_id,
                ip,
                query_id,
                placeholders,
                engine_name,
                0.0,
                STATE.sf,
                error=str(result["error"]),
                user_agent=ua,
            )
            self._send_internal_error(request_id)
            return

        logger.info(
            "TELEMETRY run_id=%s engine=%s query=%s time_ms=%.1f sf=%s",
            run_id,
            engine_name,
            query_id,
            result["time_ms"],
            STATE.sf,
        )
        _log_query_event(
            run_id,
            ip,
            query_id,
            placeholders,
            engine_name,
            result["time_ms"],
            STATE.sf,
            user_agent=ua,
        )
        self._send_json(
            200,
            {
                "run_id": run_id,
                "csv": result["csv"],
                "time_ms": result["time_ms"],
                "profile": result.get("profile"),
            },
        )

    def _handle_run_engine(self, engine: str, query_id: str, params: dict):
        run_id = _normalise_run_id(params.get("run_id"))
        log_params = dict(params)
        log_params.pop("run_id", None)
        ip = self._get_client_ip()
        ua = self._get_user_agent()
        if engine not in ("duckdb", "umbra", "clickhouse"):
            self._send_json(
                400,
                {
                    "error": f"Unknown engine '{engine}'. Use 'duckdb', 'umbra', or 'clickhouse'."
                },
            )
            return
        if engine == "duckdb" and STATE.duckdb_con is None:
            self._send_json(
                503,
                {
                    "error": "DuckDB runner not initialized. Start service with --duckdb."
                },
            )
            return
        if engine == "umbra" and STATE.umbra_url is None:
            self._send_json(
                503,
                {
                    "error": "Umbra service not configured. Start service with --umbra <url>."
                },
            )
            return
        if engine == "clickhouse" and STATE.clickhouse_url is None:
            self._send_json(
                503,
                {
                    "error": "ClickHouse service not configured. Start service with --clickhouse <url>."
                },
            )
            return
        if query_id not in STATE.query_ids:
            self._send_json(404, {"error": f"Unknown query_id: '{query_id}'"})
            return

        placeholders, missing, validation_error = self._extract_placeholders(
            query_id, params
        )
        if validation_error is not None:
            _log_query_event(
                run_id,
                ip,
                query_id,
                log_params,
                engine,
                0.0,
                STATE.sf,
                error=validation_error,
                user_agent=ua,
            )
            self._send_json(400, {"error": validation_error})
            return
        if missing:
            _log_query_event(
                run_id,
                ip,
                query_id,
                log_params,
                engine,
                0.0,
                STATE.sf,
                error=f"Missing placeholder(s): {missing}",
                user_agent=ua,
            )
            self._send_json(400, {"error": f"Missing placeholder(s): {missing}"})
            return

        # Assemble SQL from template + placeholders
        sql = STATE.sql_templates[query_id]
        for key, value in placeholders.items():
            sql = sql.replace(f"[{key.upper()}]", value)

        try:
            if engine == "duckdb":
                with STATE.duckdb_lock:
                    assert STATE.duckdb_con is not None
                    try:
                        time_ms, df, _ = STATE.duckdb_con.duckdb_sql(sql)
                    except Exception as exc:
                        # DuckDB rejected the query (e.g. invalid date / SQL) —
                        # surface its message rather than a generic 500.
                        raise EngineQueryError(str(exc)) from exc
                    csv_text = df.to_csv(index=False)
            elif engine == "umbra":
                assert STATE.umbra_url is not None
                payload = json.dumps(
                    {"run_id": run_id, "sql": sql, "sf": STATE.sf}
                ).encode()
                result = _post_with_retry(
                    f"{STATE.umbra_url}/query", payload, engine, query_id
                )
                if result is None:
                    _log_query_event(
                        run_id,
                        ip,
                        query_id,
                        placeholders,
                        engine,
                        0.0,
                        STATE.sf,
                        error="Umbra service not responding after retries",
                        user_agent=ua,
                    )
                    self._send_json(
                        503, {"error": "Umbra service not responding after retries"}
                    )
                    return
                if "error" in result:
                    raise EngineQueryError(result["error"])
                csv_text = result["csv"]
                time_ms = result["time_ms"]
            else:  # clickhouse
                assert STATE.clickhouse_url is not None
                payload = json.dumps(
                    {"run_id": run_id, "sql": sql, "sf": STATE.sf}
                ).encode()
                result = _post_with_retry(
                    f"{STATE.clickhouse_url}/query", payload, engine, query_id
                )
                if result is None:
                    _log_query_event(
                        run_id,
                        ip,
                        query_id,
                        placeholders,
                        engine,
                        0.0,
                        STATE.sf,
                        error="ClickHouse service not responding after retries",
                        user_agent=ua,
                    )
                    self._send_json(
                        503,
                        {"error": "ClickHouse service not responding after retries"},
                    )
                    return
                if "error" in result:
                    raise EngineQueryError(result["error"])
                csv_text = result["csv"]
                time_ms = result["time_ms"]
        except EngineQueryError as exc:
            # The engine rejected the query (bad input, not a server fault):
            # report its own message as a 400 so the UI can show what was wrong.
            logger.info(
                "run_engine(%s) query error for Q%s: %s", engine, query_id, exc
            )
            _log_query_event(
                run_id, ip, query_id, placeholders, engine, 0.0, STATE.sf,
                error=str(exc), user_agent=ua,
            )
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            request_id = f"eng-{int(time.time() * 1000)}"
            logger.exception("run_engine(%s) failed for Q%s", engine, query_id)
            logger.error("request_id=%s detail=%s", request_id, str(exc))
            _log_query_event(
                run_id,
                ip,
                query_id,
                placeholders,
                engine,
                0.0,
                STATE.sf,
                error=str(exc),
                user_agent=ua,
            )
            self._send_internal_error(request_id)
            return

        logger.info(
            "TELEMETRY run_id=%s engine=%s query=%s time_ms=%.1f sf=%s",
            run_id,
            engine,
            query_id,
            time_ms,
            STATE.sf,
        )
        _log_query_event(
            run_id,
            ip,
            query_id,
            placeholders,
            engine,
            time_ms,
            STATE.sf,
            user_agent=ua,
        )
        self._send_json(200, {"run_id": run_id, "csv": csv_text, "time_ms": time_ms})


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Spawn a new thread per connection; queries are serialised inside via STATE.lock."""

    daemon_threads = True
    request_queue_size = 128

    ssl_context: ssl.SSLContext | None = None
    tls_handshake_timeout: float = 10.0

    def get_request(self):
        # Return the raw accepted socket. TLS handshake is deferred to the
        # worker thread in finish_request so a slow / non-TLS client (e.g. an
        # internet scanner speaking plain HTTP) cannot stall the accept loop.
        return self.socket.accept()

    def finish_request(self, request, client_address):
        if self.ssl_context is not None:
            try:
                request.settimeout(self.tls_handshake_timeout)
                request = self.ssl_context.wrap_socket(request, server_side=True)
                request.settimeout(None)
            except (ssl.SSLError, OSError, socket.timeout) as exc:
                logger.debug("TLS handshake failed from %s: %s", client_address, exc)
                try:
                    request.close()
                except OSError:
                    pass
                return
        super().finish_request(request, client_address)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="BespokeOLAP HTTP query service – constantly running REST API."
    )
    parser.add_argument(
        "benchmark", choices=["tpch", "ceb"], help="Which benchmark to serve"
    )
    parser.add_argument("--sf", default=1, help="Scale factor (default: 1)")
    parser.add_argument(
        "--base-parquet-dir",
        required=True,
        help="Root directory containing benchmark parquet folders",
    )
    parser.add_argument(
        "--bespoke",
        type=str,
        default=None,
        metavar="URL",
        help="URL of running bespoke_service.py (e.g. http://127.0.0.1:7657)",
    )
    parser.add_argument(
        "--bespoke_profiled",
        type=str,
        default=None,
        metavar="URL",
        help="URL of a second bespoke_service.py started with --trace, used to "
        "show the per-section profiling breakdown (e.g. http://127.0.0.1:7658)",
    )
    parser.add_argument(
        "--duckdb",
        action="store_true",
        default=False,
        help="Enable DuckDB comparison engine",
    )
    parser.add_argument(
        "--umbra",
        type=str,
        default=None,
        metavar="URL",
        help="URL of running umbra_service.py (e.g. http://127.0.0.1:7655)",
    )
    parser.add_argument(
        "--clickhouse",
        type=str,
        default=None,
        metavar="URL",
        help="URL of running clickhouse_service.py (e.g. http://127.0.0.1:7656)",
    )
    parser.add_argument(
        "--rate-limit-rpm",
        type=int,
        default=60,
        help="Per-IP request limit per minute (default: 60)",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7654)
    parser.add_argument(
        "--cert",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to TLS certificate file (PEM). Enables HTTPS when set together with --key.",
    )
    parser.add_argument(
        "--key",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to TLS private key file (PEM). Enables HTTPS when set together with --cert.",
    )
    args = parser.parse_args()

    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be provided together")

    if args.rate_limit_rpm <= 0:
        raise ValueError("--rate-limit-rpm must be > 0")

    try:
        init_service(args)

        server = _ThreadedHTTPServer((args.host, args.port), _QueryHandler)
        if args.cert and args.key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
            server.ssl_context = ctx
            scheme = "https"
        else:
            scheme = "http"
        logger.info(
            "Serving on %s://%s:%d  (Ctrl-C to stop)", scheme, args.host, args.port
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            server.shutdown()
    except Exception as exc:
        logger.exception("Frontend service crashed")
        notify_service_crash("frontend", exc)
        raise


if __name__ == "__main__":
    main()
