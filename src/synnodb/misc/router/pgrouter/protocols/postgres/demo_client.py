"""Low-level PostgreSQL protocol helpers for the demo client."""

from __future__ import annotations

import socket
import struct
from typing import Any, Callable, Optional, Sequence

from ...state import ColumnInfo
from .constants import PROTO_3_0
from .pgtypes import (
    BOOL_OID,
    BPCHAR_OID,
    BYTEA_OID,
    FLOAT8_OID,
    INT2_OID,
    INT4_OID,
    INT8_OID,
    JSON_OID,
    JSONB_OID,
    TEXT_OID,
    UUID_OID,
    VARCHAR_OID,
)
from .value_decoding import decode_binary_value
from .wire import decode_error_or_notice, decode_row_description, i16, i32, read_cstring


def recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> tuple[bytes, bytes]:
    tag = recv_exact(sock, 1)
    length = i32(recv_exact(sock, 4))
    payload = recv_exact(sock, length - 4)
    return tag, payload


def send_message(sock: socket.socket, tag: bytes, payload: bytes) -> None:
    sock.sendall(tag + struct.pack("!i", len(payload) + 4) + payload)


def send_startup(sock: socket.socket, user: str, database: str) -> None:
    params = [
        ("user", user),
        ("database", database),
        ("client_encoding", "UTF8"),
        ("application_name", "pg-router-test-client"),
    ]
    body = struct.pack("!i", PROTO_3_0)
    for key, value in params:
        body += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
    body += b"\x00"
    sock.sendall(struct.pack("!i", len(body) + 4) + body)


def send_query(sock: socket.socket, sql: str) -> None:
    payload = sql.encode("utf-8") + b"\x00"
    send_message(sock, b"Q", payload)


def send_parse(sock: socket.socket, stmt_name: str, sql: str, param_type_oids: Sequence[int]) -> None:
    payload = stmt_name.encode("utf-8") + b"\x00"
    payload += sql.encode("utf-8") + b"\x00"
    payload += struct.pack("!h", len(param_type_oids))
    for oid in param_type_oids:
        payload += struct.pack("!i", oid)
    send_message(sock, b"P", payload)


def send_bind(
    sock: socket.socket,
    portal_name: str,
    stmt_name: str,
    param_formats: Sequence[int],
    params: Sequence[Optional[bytes]],
    result_formats: Sequence[int],
) -> None:
    payload = portal_name.encode("utf-8") + b"\x00"
    payload += stmt_name.encode("utf-8") + b"\x00"
    payload += struct.pack("!h", len(param_formats))
    for fmt in param_formats:
        payload += struct.pack("!h", fmt)
    payload += struct.pack("!h", len(params))
    for param in params:
        if param is None:
            payload += struct.pack("!i", -1)
            continue
        payload += struct.pack("!i", len(param))
        payload += param
    payload += struct.pack("!h", len(result_formats))
    for fmt in result_formats:
        payload += struct.pack("!h", fmt)
    send_message(sock, b"B", payload)


def send_execute(sock: socket.socket, portal_name: str, max_rows: int = 0) -> None:
    send_message(sock, b"E", portal_name.encode("utf-8") + b"\x00" + struct.pack("!i", max_rows))


def send_describe(sock: socket.socket, kind: str, name: str) -> None:
    send_message(sock, b"D", kind.encode("ascii") + name.encode("utf-8") + b"\x00")


def send_sync(sock: socket.socket) -> None:
    send_message(sock, b"S", b"")


def send_terminate(sock: socket.socket) -> None:
    sock.sendall(b"X" + struct.pack("!i", 4))


def decode_data_row(payload: bytes, columns: Sequence[ColumnInfo]) -> list[Optional[Any]]:
    off = 0
    count = i16(payload, off)
    off += 2
    row: list[Optional[Any]] = []
    for idx in range(count):
        length = i32(payload, off)
        off += 4
        if length == -1:
            row.append(None)
            continue
        raw = payload[off:off + length]
        off += length
        column = columns[idx] if idx < len(columns) else ColumnInfo(f"col{idx + 1}", TEXT_OID, 0)
        if column.format_code == 0:
            row.append(raw.decode("utf-8", errors="replace"))
        else:
            row.append(decode_binary_value(column.type_oid, raw))
    return row


def read_until_ready(
    sock: socket.socket,
    *,
    label: str,
    out: Callable[..., None],
) -> tuple[list[ColumnInfo], list[list[Optional[Any]]], Optional[str]]:
    columns: list[ColumnInfo] = []
    rows: list[list[Optional[Any]]] = []
    command_tag: Optional[str] = None
    while True:
        tag, payload = recv_message(sock)
        if tag == b"R":
            auth_code = i32(payload, 0)
            if auth_code != 0:
                raise RuntimeError(f"{label}: unsupported auth method {auth_code}")
        elif tag in (b"S", b"K"):
            continue
        elif tag == b"N":
            fields = decode_error_or_notice(payload)
            out(f"[notice] {fields.get('M', 'unknown notice')}")
        elif tag == b"T":
            columns = decode_row_description(payload)
        elif tag == b"D":
            rows.append(decode_data_row(payload, columns))
        elif tag == b"C":
            command_tag, _ = read_cstring(payload, 0)
        elif tag in (b"1", b"2", b"3", b"n", b"s", b"t"):
            continue
        elif tag == b"E":
            fields = decode_error_or_notice(payload)
            raise RuntimeError(f"{label}: {fields.get('C')} {fields.get('M')}")
        elif tag == b"Z":
            return columns, rows, command_tag
