"""CLI entrypoint for the router."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Tuple

from .config import RouterConfig
from .output import result_file_formats
from .service import run_router

log = logging.getLogger("pgrouter")


def parse_host_port(value: str, default_port: int) -> Tuple[str, int]:
    if not value:
        raise argparse.ArgumentTypeError("address must not be empty")
    if ":" not in value:
        return value, default_port
    host, port_text = value.rsplit(":", 1)
    if not host:
        raise argparse.ArgumentTypeError("address must include a host")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port in address: {value!r}") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(f"port out of range in address: {value!r}")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run the plaintext PostgreSQL router.")
    ap.add_argument("--listen", default="127.0.0.1", help="router listen address as host or host:port")
    ap.add_argument("--upstream", default="127.0.0.1:15433", help="upstream Postgres address as host or host:port")
    ap.add_argument("--jsonl-path", default="queries.jsonl", help="path to the query metadata JSONL file")
    ap.add_argument(
        "--catalog-lookup-dsn",
        default=os.environ.get("PGROUTER_LOOKUP_DSN"),
        help="DSN for the side connection used to resolve type names",
    )
    ap.add_argument(
        "--catalog-lookup-password",
        default=os.environ.get("PGROUTER_LOOKUP_PASSWORD"),
        help="password for side lookup when reusing startup user/database",
    )
    ap.add_argument("--results-dir", default="results", help="directory for per-query result files")
    ap.add_argument("--result-file-format", choices=result_file_formats(), default="json", help="result file serialization format")
    ap.add_argument("--append", action="store_true", help="append to existing captures instead of clearing them")
    ap.add_argument("--debug", action="store_true", help="enable debug logging")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    listen_host, listen_port = parse_host_port(args.listen, 5432)
    upstream_host, upstream_port = parse_host_port(args.upstream, 15433)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = RouterConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        jsonl_path=args.jsonl_path,
        catalog_lookup_dsn=args.catalog_lookup_dsn,
        catalog_lookup_password=args.catalog_lookup_password,
        results_dir=args.results_dir,
        result_file_format=args.result_file_format,
        capture_enabled=True,
        append=args.append,
    )
    run_router(config)


def run() -> None:
    main()
