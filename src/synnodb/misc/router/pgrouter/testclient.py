"""Tiny PostgreSQL protocol client for exercising the local router."""

from __future__ import annotations

import argparse
import socket
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .cli import parse_host_port
from .protocols.postgres.demo_client import (
    read_until_ready,
    send_bind,
    send_describe,
    send_execute,
    send_parse,
    send_query,
    send_startup,
    send_sync,
    send_terminate,
)
from .protocols.postgres.pgtypes import BOOL_OID, BYTEA_OID, FLOAT8_OID, INT4_OID, JSONB_OID, TEXT_OID, UUID_OID

SETUP_QUERIES = [
    "create table if not exists demo(id serial primary key, name text not null)",
    "truncate table demo restart identity",
    "insert into demo(name) values ('alice'), ('bob'), ('carol')",
    "select id, name from demo order by id",
    "select count(*) as total_rows from demo",
]

REPETITION_DEMO_QUERIES = [
    "select id, name from demo where name = 'alice' order by id",
    "select id, name from demo where name = 'bob' order by id",
    "select id + 10 as shifted_id from demo where id = 1",
    "select 10 + id as shifted_id from demo where id = 1",
    "select id as demo_id from demo where id = 1",
    "select id as local_id from demo where id = 1",
]

TRANSACTION_DEMO_QUERIES = [
    "begin",
    "select id, name from demo where name = 'alice' order by id",
    "commit",
    "begin",
    "select id, name from demo where name = 'bob' order by id",
    "commit",
]


DEFAULT_QUERIES = [*SETUP_QUERIES, *REPETITION_DEMO_QUERIES, *TRANSACTION_DEMO_QUERIES]
EMBEDDED_DEMO_QUERIES = [
    "select id, name from demo where name = 'alice' order by id",
    "select id, name from demo order by id",
    "select 1 as passthrough_value",
    "begin",
    "select id, name from demo where name = 'alice' order by id",
    "select id, name from demo where name = 'bob' order by id",
    "commit",
]
DEFAULT_TARGET = "127.0.0.1:5432"


@dataclass(frozen=True)
class BinaryExtendedExecution:
    portal_name: str
    bind_params: tuple[bytes, ...]
    expected_parameters: tuple[dict[str, Any], ...]
    expected_row: dict[str, Any]


BINARY_EXTENDED_STATEMENT_NAME = "binary_demo_stmt"
BINARY_EXTENDED_QUERY = (
    "select "
    "$1::int4 as binary_int, "
    "$2::text as binary_text, "
    "$3::bool as binary_bool, "
    "$4::float8 as binary_float8, "
    "$5::uuid as binary_uuid, "
    "$6::bytea as binary_bytea, "
    "$7::jsonb as binary_jsonb"
)
BINARY_EXTENDED_RESULT_TYPES = [
    {"name": "binary_int", "type_oid": 23, "type_name": "int4", "format": "binary"},
    {"name": "binary_text", "type_oid": 25, "type_name": "text", "format": "binary"},
    {"name": "binary_bool", "type_oid": 16, "type_name": "bool", "format": "binary"},
    {"name": "binary_float8", "type_oid": 701, "type_name": "float8", "format": "binary"},
    {"name": "binary_uuid", "type_oid": 2950, "type_name": "uuid", "format": "binary"},
    {"name": "binary_bytea", "type_oid": 17, "type_name": "bytea", "format": "binary"},
    {"name": "binary_jsonb", "type_oid": 3802, "type_name": "jsonb", "format": "binary"},
]


def binary_extended_executions() -> list[BinaryExtendedExecution]:
    return [
        BinaryExtendedExecution(
            portal_name="binary_demo_portal_1",
            bind_params=(
                struct.pack("!i", 42),
                b"delta",
                b"\x01",
                struct.pack("!d", 3.5),
                uuid.UUID("12345678-1234-5678-1234-567812345678").bytes,
                b"\x00\x01\xfe\xff",
                b"\x01" + b'{"k":"v"}',
            ),
            expected_parameters=(
                {"index": 1, "format": "binary", "type_oid": 23, "type_name": "int4", "length": 4, "is_null": False, "value": 42},
                {"index": 2, "format": "binary", "type_oid": 25, "type_name": "text", "length": 5, "is_null": False, "value": "delta"},
                {"index": 3, "format": "binary", "type_oid": 16, "type_name": "bool", "length": 1, "is_null": False, "value": True},
                {"index": 4, "format": "binary", "type_oid": 701, "type_name": "float8", "length": 8, "is_null": False, "value": 3.5},
                {"index": 5, "format": "binary", "type_oid": 2950, "type_name": "uuid", "length": 16, "is_null": False, "value": "12345678-1234-5678-1234-567812345678"},
                {"index": 6, "format": "binary", "type_oid": 17, "type_name": "bytea", "length": 4, "is_null": False, "value": "\\x0001feff"},
                {"index": 7, "format": "binary", "type_oid": 3802, "type_name": "jsonb", "length": 10, "is_null": False, "value": {"jsonb_version": 1, "value": '{"k":"v"}'}},
            ),
            expected_row={
                "binary_int": 42,
                "binary_text": "delta",
                "binary_bool": True,
                "binary_float8": 3.5,
                "binary_uuid": "12345678-1234-5678-1234-567812345678",
                "binary_bytea": "\\x0001feff",
                "binary_jsonb": {"jsonb_version": 1, "value": '{"k": "v"}'},
            },
        ),
        BinaryExtendedExecution(
            portal_name="binary_demo_portal_2",
            bind_params=(
                struct.pack("!i", 7),
                b"echo",
                b"\x00",
                struct.pack("!d", 9.25),
                uuid.UUID("87654321-4321-8765-4321-876543218765").bytes,
                b"\xaa\xbb\xcc",
                b"\x01" + b'{"k":"w"}',
            ),
            expected_parameters=(
                {"index": 1, "format": "binary", "type_oid": 23, "type_name": "int4", "length": 4, "is_null": False, "value": 7},
                {"index": 2, "format": "binary", "type_oid": 25, "type_name": "text", "length": 4, "is_null": False, "value": "echo"},
                {"index": 3, "format": "binary", "type_oid": 16, "type_name": "bool", "length": 1, "is_null": False, "value": False},
                {"index": 4, "format": "binary", "type_oid": 701, "type_name": "float8", "length": 8, "is_null": False, "value": 9.25},
                {"index": 5, "format": "binary", "type_oid": 2950, "type_name": "uuid", "length": 16, "is_null": False, "value": "87654321-4321-8765-4321-876543218765"},
                {"index": 6, "format": "binary", "type_oid": 17, "type_name": "bytea", "length": 3, "is_null": False, "value": "\\xaabbcc"},
                {"index": 7, "format": "binary", "type_oid": 3802, "type_name": "jsonb", "length": 10, "is_null": False, "value": {"jsonb_version": 1, "value": '{"k":"w"}'}},
            ),
            expected_row={
                "binary_int": 7,
                "binary_text": "echo",
                "binary_bool": False,
                "binary_float8": 9.25,
                "binary_uuid": "87654321-4321-8765-4321-876543218765",
                "binary_bytea": "\\xaabbcc",
                "binary_jsonb": {"jsonb_version": 1, "value": '{"k": "w"}'},
            },
        ),
    ]


def render_row(row: Sequence[Optional[Any]]) -> str:
    return ", ".join("NULL" if value is None else str(value) for value in row)


def run_simple_queries(sock: socket.socket, queries: Sequence[str], out: Callable[..., None]) -> None:
    for sql in queries:
        out()
        out(f"SQL> {sql}")
        send_query(sock, sql)
        columns, rows, command_tag = read_until_ready(sock, label=sql, out=out)
        if columns:
            out("columns:", ", ".join(col.name for col in columns))
        for row in rows:
            out("row:", render_row(row))
        out("command:", command_tag)


def run_binary_extended_query(sock: socket.socket, out: Callable[..., None]) -> None:
    out()
    out(f"EXTENDED> {BINARY_EXTENDED_QUERY}")
    send_parse(
        sock,
        BINARY_EXTENDED_STATEMENT_NAME,
        BINARY_EXTENDED_QUERY,
        [INT4_OID, TEXT_OID, BOOL_OID, FLOAT8_OID, UUID_OID, BYTEA_OID, JSONB_OID],
    )
    for execution in binary_extended_executions():
        send_bind(
            sock,
            execution.portal_name,
            BINARY_EXTENDED_STATEMENT_NAME,
            param_formats=[1, 1, 1, 1, 1, 1, 1],
            params=list(execution.bind_params),
            result_formats=[1, 1, 1, 1, 1, 1, 1],
        )
        send_describe(sock, "P", execution.portal_name)
        send_execute(sock, execution.portal_name)
        send_sync(sock)

        columns, rows, command_tag = read_until_ready(sock, label=f"binary extended query {execution.portal_name}", out=out)
        if columns:
            column_summary = ", ".join(
                f"{col.name}(oid={col.type_oid}, format={'binary' if col.format_code == 1 else 'text'})"
                for col in columns
            )
            out("columns:", column_summary)
        for row in rows:
            out("row:", render_row(row))
        out("command:", command_tag)


def run_queries(
    host: str,
    port: int,
    user: str,
    database: str,
    queries: Sequence[str],
    *,
    emit_output: bool = True,
    include_binary_extended: bool = True,
) -> None:
    out: Callable[..., None]
    if emit_output:
        out = print
    else:
        out = lambda *args, **kwargs: None

    with socket.create_connection((host, port)) as sock:
        send_startup(sock, user, database)
        read_until_ready(sock, label="startup", out=out)
        out(f"connected to {host}:{port} as {user} db={database}")
        run_simple_queries(sock, queries, out)
        if include_binary_extended:
            run_binary_extended_query(sock, out)
        send_terminate(sock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the pgrouter demo client.")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="router address as host or host:port")
    parser.add_argument("--user", default="app")
    parser.add_argument("--database", default="app")
    parser.add_argument("--for-embedded", action="store_true", help="run only the small embedded-demo query set")
    return parser


def query_set_for_mode(*, for_embedded: bool) -> tuple[Sequence[str], bool]:
    if for_embedded:
        return EMBEDDED_DEMO_QUERIES, False
    return DEFAULT_QUERIES, True


def main() -> None:
    args = build_parser().parse_args()
    host, port = parse_host_port(args.target, 5432)
    queries, include_binary_extended = query_set_for_mode(for_embedded=args.for_embedded)
    run_queries(
        host,
        port,
        args.user,
        args.database,
        queries,
        include_binary_extended=include_binary_extended,
    )


if __name__ == "__main__":
    main()
