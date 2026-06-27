"""Decode PostgreSQL text and binary values."""

from __future__ import annotations

import datetime as dt
import struct
import uuid
from decimal import Decimal
from typing import Any, Optional

from .pgtypes import (
    ARRAY_ELEMENT_OIDS,
    BOOL_OID,
    BPCHAR_OID,
    BYTEA_OID,
    DATE_OID,
    FLOAT4_OID,
    FLOAT8_OID,
    INT2_OID,
    INT4_OID,
    INT8_OID,
    JSON_OID,
    JSONB_OID,
    NUMERIC_OID,
    TEXT_OID,
    TIME_OID,
    TIMESTAMP_OID,
    TIMESTAMPTZ_OID,
    TIMETZ_OID,
    UUID_OID,
    VARCHAR_OID,
)

POSTGRES_EPOCH_DATE = dt.date(2000, 1, 1)
POSTGRES_EPOCH_DATETIME = dt.datetime(2000, 1, 1)
POSTGRES_EPOCH_DATETIME_UTC = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)


def _i32(buffer: bytes, offset: int = 0) -> int:
    return struct.unpack_from("!i", buffer, offset)[0]


def _fmt_hex(buffer: bytes, limit: int = 64) -> str:
    rendered = buffer[:limit].hex()
    return rendered + ("..." if len(buffer) > limit else "")


def decode_text_value(type_oid: Optional[int], raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if type_oid == BOOL_OID:
        return text == "t"
    if type_oid in (INT2_OID, INT4_OID, INT8_OID):
        try:
            return int(text)
        except ValueError:
            return text
    if type_oid in (FLOAT4_OID, FLOAT8_OID):
        try:
            return float(text)
        except ValueError:
            return text
    if type_oid == NUMERIC_OID:
        try:
            return str(Decimal(text))
        except Exception:
            return text
    return text


def decode_numeric_binary(raw: bytes) -> str:
    ndigits, weight, sign, dscale = struct.unpack("!hhhh", raw[:8])
    digits = [struct.unpack("!h", raw[8 + i * 2:10 + i * 2])[0] for i in range(ndigits)]
    groups: list[str] = []
    for idx, digit in enumerate(digits):
        groups.append(str(digit) if idx == 0 else f"{digit:04d}")
    whole_group_count = weight + 1
    if ndigits == 0:
        whole = "0"
        frac = ""
    elif whole_group_count <= 0:
        whole = "0"
        frac_groups = ["0000"] * (-whole_group_count) + groups
        frac = "".join(frac_groups)
    else:
        whole_groups = groups[:whole_group_count]
        if len(whole_groups) < whole_group_count:
            whole_groups.extend(["0000"] * (whole_group_count - len(whole_groups)))
        frac_groups = groups[whole_group_count:]
        whole = whole_groups[0] + "".join(group.zfill(4) for group in whole_groups[1:])
        frac = "".join(frac_groups)
    if dscale > 0:
        frac = (frac + "0" * dscale)[:dscale]
        value = f"{whole}.{frac}"
    else:
        value = whole
    value = value.lstrip("0") or "0"
    if value.startswith("."):
        value = f"0{value}"
    if sign == 0x4000:
        value = f"-{value}"
    elif sign == 0xC000:
        value = "NaN"
    return value


def decode_time_binary(raw: bytes) -> str:
    micros = struct.unpack("!q", raw)[0]
    return (dt.datetime.min + dt.timedelta(microseconds=micros)).time().isoformat()


def decode_timetz_binary(raw: bytes) -> str:
    micros, tz_seconds = struct.unpack("!qi", raw)
    time_value = (dt.datetime.min + dt.timedelta(microseconds=micros)).time()
    offset = dt.timedelta(seconds=-tz_seconds)
    hours, remainder = divmod(int(offset.total_seconds()), 3600)
    minutes = abs(remainder) // 60
    return f"{time_value.isoformat()}{hours:+03d}:{minutes:02d}"


def decode_timestamp_binary(raw: bytes, *, with_tz: bool) -> str:
    micros = struct.unpack("!q", raw)[0]
    if with_tz:
        value = POSTGRES_EPOCH_DATETIME_UTC + dt.timedelta(microseconds=micros)
    else:
        value = POSTGRES_EPOCH_DATETIME + dt.timedelta(microseconds=micros)
    return value.isoformat()


def decode_array_binary(raw: bytes) -> Any:
    ndim, _has_null, elem_oid = struct.unpack("!iii", raw[:12])
    offset = 12
    dimensions: list[int] = []
    for _ in range(ndim):
        length, _lower_bound = struct.unpack("!ii", raw[offset:offset + 8])
        offset += 8
        dimensions.append(length)

    def parse_values(dim_index: int) -> Any:
        nonlocal offset
        result: list[Any] = []
        length = dimensions[dim_index]
        for _ in range(length):
            if dim_index == len(dimensions) - 1:
                item_length = _i32(raw, offset)
                offset += 4
                if item_length == -1:
                    result.append(None)
                    continue
                item_raw = raw[offset:offset + item_length]
                offset += item_length
                result.append(decode_binary_value(elem_oid, item_raw))
            else:
                result.append(parse_values(dim_index + 1))
        return result

    if ndim == 0:
        return []
    return parse_values(0)


def decode_binary_value(type_oid: int, raw: bytes) -> Any:
    if type_oid == BOOL_OID and len(raw) == 1:
        return raw != b"\x00"
    if type_oid == INT2_OID and len(raw) == 2:
        return struct.unpack("!h", raw)[0]
    if type_oid == INT4_OID and len(raw) == 4:
        return struct.unpack("!i", raw)[0]
    if type_oid == INT8_OID and len(raw) == 8:
        return struct.unpack("!q", raw)[0]
    if type_oid == FLOAT4_OID and len(raw) == 4:
        return struct.unpack("!f", raw)[0]
    if type_oid == FLOAT8_OID and len(raw) == 8:
        return struct.unpack("!d", raw)[0]
    if type_oid == NUMERIC_OID and len(raw) >= 8:
        return decode_numeric_binary(raw)
    if type_oid in ARRAY_ELEMENT_OIDS:
        return decode_array_binary(raw)
    if type_oid in (TEXT_OID, VARCHAR_OID, BPCHAR_OID, JSON_OID):
        return raw.decode("utf-8", errors="replace")
    if type_oid == JSONB_OID and raw:
        version = raw[0]
        body = raw[1:]
        return {"jsonb_version": version, "value": body.decode("utf-8", errors="replace")}
    if type_oid == UUID_OID and len(raw) == 16:
        return str(uuid.UUID(bytes=raw))
    if type_oid == BYTEA_OID:
        return f"\\x{raw.hex()}"
    if type_oid == DATE_OID and len(raw) == 4:
        days = struct.unpack("!i", raw)[0]
        return (POSTGRES_EPOCH_DATE + dt.timedelta(days=days)).isoformat()
    if type_oid == TIME_OID and len(raw) == 8:
        return decode_time_binary(raw)
    if type_oid == TIMETZ_OID and len(raw) == 12:
        return decode_timetz_binary(raw)
    if type_oid == TIMESTAMP_OID and len(raw) == 8:
        return decode_timestamp_binary(raw, with_tz=False)
    if type_oid == TIMESTAMPTZ_OID and len(raw) == 8:
        return decode_timestamp_binary(raw, with_tz=True)
    return f"<binary {len(raw)} bytes 0x{_fmt_hex(raw, 16)}>"


def decode_value(format_code: int, type_oid: Optional[int], raw: bytes) -> Any:
    if format_code == 0:
        return decode_text_value(type_oid, raw)
    return decode_binary_value(type_oid or 0, raw)
