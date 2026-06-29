"""Length-prefixed JSON framing for the worker control plane.

Only small control messages cross this channel (commands + acks + segment handles);
bulk data goes through shared memory. Frame = 4-byte little-endian length + JSON.
"""
from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO, Optional

_HEADER = struct.Struct("<I")


def write_message(stream: BinaryIO, obj: Any) -> None:
    data = json.dumps(obj).encode("utf-8")
    stream.write(_HEADER.pack(len(data)))
    stream.write(data)
    stream.flush()


def _read_exactly(stream: BinaryIO, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_message(stream: BinaryIO) -> Optional[dict]:
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    body = _read_exactly(stream, length)
    if body is None:
        return None
    return json.loads(body)
