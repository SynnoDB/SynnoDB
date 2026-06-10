"""PostgreSQL backend message encoding helpers."""

from __future__ import annotations

import json
import struct
import uuid
from typing import Any, Sequence

from ...state import ColumnInfo
from .pgtypes import (
    BOOL_OID,
    BPCHAR_OID,
    BYTEA_OID,
    FLOAT4_OID,
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


def encode_message(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("!i", len(payload) + 4) + payload


def cstring(value: str) -> bytes:
    return value.encode("utf-8") + b"\x00"


def encode_parse_complete() -> bytes:
    return encode_message(b"1", b"")


def encode_bind_complete() -> bytes:
    return encode_message(b"2", b"")


def encode_close_complete() -> bytes:
    return encode_message(b"3", b"")


def encode_no_data() -> bytes:
    return encode_message(b"n", b"")


def encode_ready_for_query(tx_status: str) -> bytes:
    return encode_message(b"Z", tx_status.encode("ascii"))


def encode_parameter_description(type_oids: Sequence[int]) -> bytes:
    payload = struct.pack("!h", len(type_oids))
    for type_oid in type_oids:
        payload += struct.pack("!i", type_oid)
    return encode_message(b"t", payload)


def _type_size(type_oid: int) -> int:
    if type_oid == BOOL_OID:
        return 1
    if type_oid == INT2_OID:
        return 2
    if type_oid in (INT4_OID, FLOAT4_OID):
        return 4
    if type_oid in (INT8_OID, FLOAT8_OID):
        return 8
    if type_oid == UUID_OID:
        return 16
    return -1


def encode_row_description(columns: Sequence[ColumnInfo]) -> bytes:
    payload = struct.pack("!h", len(columns))
    for column in columns:
        payload += cstring(column.name)
        payload += struct.pack("!i", 0)
        payload += struct.pack("!h", 0)
        payload += struct.pack("!i", column.type_oid)
        payload += struct.pack("!h", _type_size(column.type_oid))
        payload += struct.pack("!i", -1)
        payload += struct.pack("!h", column.format_code)
    return encode_message(b"T", payload)


def _text_value(type_oid: int, value: Any) -> bytes:
    if value is None:
        raise ValueError("NULL values are encoded outside _text_value")
    if type_oid == BOOL_OID:
        return (b"t" if bool(value) else b"f")
    if type_oid == BYTEA_OID:
        if isinstance(value, (bytes, bytearray)):
            return f"\\x{bytes(value).hex()}".encode("utf-8")
        if isinstance(value, str):
            return value.encode("utf-8")
    if type_oid in (JSON_OID, JSONB_OID):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False).encode("utf-8")
        return str(value).encode("utf-8")
    return str(value).encode("utf-8")


def _binary_value(type_oid: int, value: Any) -> bytes:
    if value is None:
        raise ValueError("NULL values are encoded outside _binary_value")
    if type_oid == BOOL_OID:
        return b"\x01" if bool(value) else b"\x00"
    if type_oid == INT2_OID:
        return struct.pack("!h", int(value))
    if type_oid == INT4_OID:
        return struct.pack("!i", int(value))
    if type_oid == INT8_OID:
        return struct.pack("!q", int(value))
    if type_oid == FLOAT4_OID:
        return struct.pack("!f", float(value))
    if type_oid == FLOAT8_OID:
        return struct.pack("!d", float(value))
    if type_oid in (TEXT_OID, VARCHAR_OID, BPCHAR_OID, JSON_OID):
        return str(value).encode("utf-8")
    if type_oid == JSONB_OID:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8") if isinstance(value, (dict, list)) else str(value).encode("utf-8")
        return b"\x01" + body
    if type_oid == UUID_OID:
        if isinstance(value, uuid.UUID):
            return value.bytes
        return uuid.UUID(str(value)).bytes
    if type_oid == BYTEA_OID:
        if isinstance(value, str) and value.startswith("\\x"):
            return bytes.fromhex(value[2:])
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    raise ValueError(f"unsupported binary handler response type oid={type_oid}")


def encode_data_row(row: dict[str, Any], columns: Sequence[ColumnInfo]) -> bytes:
    payload = struct.pack("!h", len(columns))
    for column in columns:
        value = row.get(column.name)
        if value is None:
            payload += struct.pack("!i", -1)
            continue
        raw = _text_value(column.type_oid, value) if column.format_code == 0 else _binary_value(column.type_oid, value)
        payload += struct.pack("!i", len(raw)) + raw
    return encode_message(b"D", payload)


def encode_command_complete(command_tag: str) -> bytes:
    return encode_message(b"C", cstring(command_tag))


def encode_error_response(message: str, *, code: str = "0A000", severity: str = "ERROR") -> bytes:
    payload = b""
    payload += b"S" + severity.encode("utf-8") + b"\x00"
    payload += b"C" + code.encode("utf-8") + b"\x00"
    payload += b"M" + message.encode("utf-8") + b"\x00"
    payload += b"\x00"
    return encode_message(b"E", payload)
