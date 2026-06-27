#!/usr/bin/env python3
"""Example application-side embedding of the router with handler-based interception."""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from pgrouter import (
    EmbeddedRouter,
    ResultColumn,
    RouterConfig,
    RouteResult,
    Statement,
    StatementContext,
    StatementRoute,
    TransactionControlContext,
    TransactionRoute,
    TransactionStartContext,
    TransactionStatementContext,
)
from pgrouter.cli import parse_host_port
from pgrouter.protocols.postgres.pgtypes import INT4_OID, TEXT_OID

DEFAULT_LISTEN = "127.0.0.1:5432"
DEFAULT_UPSTREAM = "127.0.0.1:15433"
log = logging.getLogger("pgrouter.demo_embedded")


def resolve_demo_row(name: str) -> dict[str, object]:
    known_rows = {
        "alice": {"id": 9001, "name": "hook:alice"},
        "bob": {"id": 9002, "name": "hook:bob"},
        "carol": {"id": 9003, "name": "hook:carol"},
    }
    normalized_name = name.strip().lower()
    known_row = known_rows.get(normalized_name)
    if known_row is not None:
        return dict(known_row)
    return {
        "id": 9900 + len(normalized_name),
        "name": f"hook:guest:{normalized_name or 'unknown'}",
    }


def demo_handler(context: StatementContext) -> RouteResult:
    name = str(context.parameter_values[0]) if context.parameter_values else "unknown"
    row = resolve_demo_row(name)
    log.info(
        "Demo handler served route=%r name=%r row=%s",
        context.route.structure,
        name,
        row,
    )
    return RouteResult(rows=[row])


def streaming_demo_handler(_context: StatementContext) -> RouteResult:
    def rows() -> object:
        for name in ("alice", "bob", "carol"):
            yield resolve_demo_row(name)

    log.info("Demo streaming handler served ordered demo rows")
    return RouteResult(rows=rows())


class DemoTransactionSession:
    def __init__(self, start_context: TransactionStartContext) -> None:
        self.transaction_id = start_context.transaction_id
        self.seen_names: list[str] = []

    def on_statement(self, context: TransactionStatementContext) -> RouteResult:
        name = str(context.parameter_values[0]) if context.parameter_values else "unknown"
        self.seen_names.append(name)
        sequence = len(self.seen_names)
        row = {
            "id": 9500 + sequence,
            "name": f"tx:{sequence}:{name.strip().lower()}",
        }
        log.info("Demo transaction handler tx=%s step=%d name=%r row=%s", self.transaction_id, sequence, name, row)
        return RouteResult(rows=[row])

    def on_commit(self, context: TransactionControlContext) -> None:
        log.info("Demo transaction handler commit tx=%s names=%s statements=%d", context.transaction_id, self.seen_names, context.statement_count)
        return None

    def on_rollback(self, context: TransactionControlContext) -> None:
        log.info("Demo transaction handler rollback tx=%s names=%s statements=%d", context.transaction_id, self.seen_names, context.statement_count)
        return None


def transaction_demo_handler(start_context: TransactionStartContext) -> DemoTransactionSession:
    return DemoTransactionSession(start_context)


NAME_STATEMENT_ROUTE = StatementRoute(
    statement=Statement(
        "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
        result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
        name="demo_name_lookup",
    ),
    handler=demo_handler,
)

STREAMING_STATEMENT_ROUTE = StatementRoute(
    statement=Statement(
        "SELECT id, name FROM demo ORDER BY id",
        result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
        name="demo_stream_all_rows",
    ),
    handler=streaming_demo_handler,
)

TRANSACTION_ROUTE = TransactionRoute(
    statements=[
        Statement(
            "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
            result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
            name="first_name_lookup",
        ),
        Statement(
            "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
            result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
            name="second_name_lookup",
        ),
    ],
    handler=transaction_demo_handler,
    name="demo_name_pair_transaction",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the embedded pgrouter demo host.")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help="embedded router listen address as host or host:port")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="upstream Postgres address as host or host:port")
    parser.add_argument("--capture", action="store_true", help="enable query/result capture output")
    return parser


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def wait_for_shutdown_signal() -> threading.Event:
    stop_event = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_shutdown)
    return stop_event


def build_config(args: argparse.Namespace) -> RouterConfig:
    listen_host, listen_port = parse_host_port(args.listen, 5432)
    upstream_host, upstream_port = parse_host_port(args.upstream, 15433)
    return RouterConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        statement_routes=[NAME_STATEMENT_ROUTE, STREAMING_STATEMENT_ROUTE],
        transaction_routes=[TRANSACTION_ROUTE],
        capture_enabled=args.capture,
    )


def main() -> None:
    args = build_parser().parse_args()
    configure_logging()
    stop_event = wait_for_shutdown_signal()
    config = build_config(args)
    router = EmbeddedRouter(config)

    router.start()
    log.info(
        "Embedded router is running on %s:%d. Use demo_client.py against that port to exercise the handlers.",
        config.listen_host,
        config.listen_port,
    )
    try:
        stop_event.wait()
    finally:
        router.stop()


if __name__ == "__main__":
    main()
