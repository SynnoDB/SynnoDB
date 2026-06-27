#!/usr/bin/env python3
"""Standalone ClickHouse HTTP service.

Starts the ClickHouse Docker container, loads the requested benchmark/SF data,
then serves a simple HTTP API so other processes can run queries without
restarting ClickHouse.

Endpoints:
  GET  /health              -> {"status": "ok"}
  POST /query               -> body: {"sql": "...", "sf": 1}
                               response: {"csv": "...", "time_ms": 123.4}

Usage:
  python clickhouse_service.py tpch --sf 1 --port 7656
"""

import argparse
import csv as csv_module
import http.server
import io
import json
import logging
import socketserver

# add parent to path
import sys
import threading
import time
from pathlib import Path

from synnodb.observability.logging.logger import setup_logging
from synnodb.workloads.dataset.dataset_tables_dict import get_dataset_name

sys.path.append(Path(__file__).parent.parent.parent.as_posix())


from synnodb.observability.benchmark.systems.clickhouse import ClickHouseRunner
from synnodb.observability.ui_template_runner.service_notify import (
    notify_5xx_response,
    notify_service_crash,
)

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)


class _State:
    runner: ClickHouseRunner | None = None
    lock: threading.Lock = threading.Lock()
    sf: float = None  # type: ignore
    benchmark: str = None  # type: ignore


STATE = _State()


class _ClickHouseHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("HTTP %s - " + fmt, self.client_address[0], *args)

    def _send(self, code: int, body: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")
        if code >= 500:
            notify_5xx_response("clickhouse", self.path, code, data)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Use POST /query"})

    def do_POST(self):
        if self.path != "/query":
            self._send_json(404, {"error": "Unknown endpoint"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        sql = req.get("sql")
        run_id = req.get("run_id", "")
        sf = req.get("sf", STATE.sf)
        if not sql:
            self._send_json(400, {"error": "Missing 'sql' field"})
            return

        if isinstance(sf, str):
            sf = float(sf) if "." in sf else int(sf)

        with STATE.lock:
            try:
                runner = STATE.runner
                assert runner is not None, "Runner not initialized"
                runner._switch_sf(sf)
                client = runner._current_client
                t0 = time.perf_counter()
                assert client is not None, "ClickHouse client not initialized"
                qr = client.query(sql)
                time_ms = (time.perf_counter() - t0) * 1000.0
                logger.info(
                    "TELEMETRY run_id=%s engine=clickhouse time_ms=%.1f sf=%s",
                    run_id,
                    time_ms,
                    sf,
                )
                buf = io.StringIO()
                writer = csv_module.writer(buf)
                writer.writerow(qr.column_names)
                writer.writerows(qr.result_rows)
                csv_text = buf.getvalue()
            except Exception as exc:
                logger.exception("ClickHouse query failed")
                self._send_json(500, {"error": str(exc)})
                return

        self._send_json(200, {"run_id": run_id, "csv": csv_text, "time_ms": time_ms})


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone ClickHouse HTTP service")
    parser.add_argument("benchmark", choices=["tpch", "ceb"])
    parser.add_argument("--sf", default=1, help="Scale factor (default: 1)")
    parser.add_argument(
        "--base-parquet-dir",
        required=True,
        help="Root directory containing benchmark parquet folders",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7656)
    parser.add_argument("--container-cores", type=int, default=1)
    parser.add_argument("--container-pin-core", type=int, default=4)
    parser.add_argument("--clickhouse-host", type=str, default="localhost")
    parser.add_argument("--clickhouse-port", type=int, default=8123)
    parser.add_argument("--clickhouse-user", type=str, default="default")
    parser.add_argument("--clickhouse-password", type=str, default="clickhouse")
    args = parser.parse_args()

    sf = args.sf
    sf = float(sf) if "." in str(sf) else int(sf)

    parquet_path = (
        Path(args.base_parquet_dir) / f"{get_dataset_name(args.benchmark)}_parquet"
    )
    assert parquet_path.exists(), f"Parquet directory not found: {parquet_path}"

    STATE.benchmark = args.benchmark
    STATE.sf = sf

    try:
        logger.info("Initializing ClickHouse runner for %s SF%s…", args.benchmark, sf)
        STATE.runner = ClickHouseRunner(
            parquet_path=parquet_path,
            benchmark=args.benchmark,
            scale_factors=[sf],
            host=args.clickhouse_host,
            port=args.clickhouse_port,
            user=args.clickhouse_user,
            password=args.clickhouse_password,
            container_num_cores=args.container_cores,
            container_pin_core_id=args.container_pin_core,
        )
        STATE.runner._switch_sf(sf)
        logger.info("ClickHouse ready.")

        server = _ThreadedHTTPServer((args.host, args.port), _ClickHouseHandler)
        logger.info(
            "ClickHouse service listening on http://%s:%d", args.host, args.port
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            server.shutdown()
    except Exception as exc:
        logger.exception("ClickHouse service crashed")
        notify_service_crash("clickhouse", exc)
        raise


if __name__ == "__main__":
    main()
