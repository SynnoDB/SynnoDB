"""Wire-level parsing helpers."""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

from ...state import ColumnInfo
from .pgtypes import INT4_ARRAY_OID, FLOAT8_ARRAY_OID
from .value_decoding import decode_binary_value, decode_text_value, decode_value


def i16(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("!h", b, off)[0]


def i32(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("!i", b, off)[0]


def read_cstring(buf: bytes, off: int) -> Tuple[str, int]:
    end = buf.index(0, off)
    return buf[off:end].decode("utf-8", errors="replace"), end + 1


def parse_kv_cstrings(buf: bytes, off: int = 0) -> Dict[str, str]:
    out: Dict[str, str] = {}
    while off < len(buf):
        if buf[off] == 0:
            return out
        k, off = read_cstring(buf, off)
        v, off = read_cstring(buf, off)
        out[k] = v
    return out


def fmt_hex(b: bytes, limit: int = 64) -> str:
    s = b[:limit].hex()
    return s + ("..." if len(b) > limit else "")


def format_code_name(format_code: int) -> str:
    return "text" if format_code == 0 else "binary"


def decode_error_or_notice(payload: bytes) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    off = 0
    while off < len(payload):
        code = payload[off]
        off += 1
        if code == 0:
            break
        value, off = read_cstring(payload, off)
        fields[chr(code)] = value
    return fields


def decode_row_description(payload: bytes) -> List[ColumnInfo]:
    off = 0
    n = i16(payload, off)
    off += 2
    cols: List[ColumnInfo] = []
    for _ in range(n):
        name, off = read_cstring(payload, off)
        off += 4
        off += 2
        type_oid = i32(payload, off)
        off += 4
        off += 2
        off += 4
        fmt = i16(payload, off)
        off += 2
        cols.append(ColumnInfo(name=name, type_oid=type_oid, format_code=fmt))
    return cols


def decode_data_row(payload: bytes, row_desc: Optional[List[ColumnInfo]]) -> List[Any]:
    off = 0
    n = i16(payload, off)
    off += 2
    vals: List[Any] = []
    for idx in range(n):
        ln = i32(payload, off)
        off += 4
        fmt = row_desc[idx].format_code if row_desc and idx < len(row_desc) else 0
        if ln == -1:
            vals.append(None)
            continue
        raw = payload[off:off + ln]
        off += ln
        type_oid = row_desc[idx].type_oid if row_desc and idx < len(row_desc) else None
        vals.append(decode_value(fmt, type_oid, raw))
    return vals


def summarize_data_row(payload: bytes, row_desc: Optional[List[ColumnInfo]]) -> str:
    values = decode_data_row(payload, row_desc)
    rendered: List[str] = []
    for idx, value in enumerate(values):
        col_name = row_desc[idx].name if row_desc and idx < len(row_desc) else f"col{idx+1}"
        if value is None:
            rendered.append(f"{col_name}=NULL")
        else:
            rendered.append(f"{col_name}={value!r}")
    return ", ".join(rendered)


def data_row_to_object(payload: bytes, row_desc: Optional[List[ColumnInfo]]) -> Dict[str, Any]:
    values = decode_data_row(payload, row_desc)
    row: Dict[str, Any] = {}
    for idx, value in enumerate(values):
        key = row_desc[idx].name if row_desc and idx < len(row_desc) else f"col{idx+1}"
        row[key] = value
    return row
