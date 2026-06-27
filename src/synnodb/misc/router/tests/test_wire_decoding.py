from __future__ import annotations

import datetime as dt
import struct
import unittest

from pgrouter.protocols.postgres.pgtypes import (
    BOOL_OID,
    BYTEA_OID,
    DATE_OID,
    FLOAT8_OID,
    INT4_ARRAY_OID,
    INT4_OID,
    JSONB_OID,
    NUMERIC_OID,
    TIMESTAMP_OID,
    TIMESTAMPTZ_OID,
    UUID_OID,
)
from pgrouter.protocols.postgres.value_decoding import (
    decode_binary_value,
)


def encode_numeric_binary(*, ndigits: int, weight: int, sign: int, dscale: int, digits: list[int]) -> bytes:
    payload = struct.pack("!hhhh", ndigits, weight, sign, dscale)
    for digit in digits:
        payload += struct.pack("!h", digit)
    return payload


def encode_int4_array(values: list[int]) -> bytes:
    payload = struct.pack("!iii", 1, 0, INT4_OID)
    payload += struct.pack("!ii", len(values), 1)
    for value in values:
        payload += struct.pack("!i", 4)
        payload += struct.pack("!i", value)
    return payload


def timestamp_micros(value: dt.datetime) -> int:
    epoch = dt.datetime(2000, 1, 1, tzinfo=value.tzinfo)
    delta = value - epoch
    return ((delta.days * 24 * 60 * 60) + delta.seconds) * 1_000_000 + delta.microseconds


class WireDecodingTest(unittest.TestCase):
    def test_decode_common_binary_types(self) -> None:
        self.assertTrue(decode_binary_value(BOOL_OID, b"\x01"))
        self.assertEqual(decode_binary_value(INT4_OID, struct.pack("!i", 42)), 42)
        self.assertEqual(decode_binary_value(FLOAT8_OID, struct.pack("!d", 3.5)), 3.5)
        self.assertEqual(
            decode_binary_value(UUID_OID, bytes.fromhex("12345678123456781234567812345678")),
            "12345678-1234-5678-1234-567812345678",
        )
        self.assertEqual(decode_binary_value(BYTEA_OID, b"\x00\x01\xfe\xff"), "\\x0001feff")
        self.assertEqual(
            decode_binary_value(JSONB_OID, b"\x01" + b'{"k":"v"}'),
            {"jsonb_version": 1, "value": '{"k":"v"}'},
        )

    def test_decode_numeric_binary(self) -> None:
        self.assertEqual(
            decode_binary_value(
                NUMERIC_OID,
                encode_numeric_binary(ndigits=1, weight=0, sign=0x0000, dscale=0, digits=[42]),
            ),
            "42",
        )
        self.assertEqual(
            decode_binary_value(
                NUMERIC_OID,
                encode_numeric_binary(ndigits=2, weight=0, sign=0x4000, dscale=1, digits=[42, 5000]),
            ),
            "-42.5",
        )

    def test_decode_binary_array_and_temporal_types(self) -> None:
        self.assertEqual(decode_binary_value(INT4_ARRAY_OID, encode_int4_array([1, 2, 3])), [1, 2, 3])
        self.assertEqual(decode_binary_value(DATE_OID, struct.pack("!i", 1)), "2000-01-02")
        self.assertEqual(
            decode_binary_value(TIMESTAMP_OID, struct.pack("!q", timestamp_micros(dt.datetime(2024, 1, 2, 3, 4, 5)))),
            "2024-01-02T03:04:05",
        )
        self.assertEqual(
            decode_binary_value(
                TIMESTAMPTZ_OID,
                struct.pack("!q", timestamp_micros(dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC))),
            ),
            "2024-01-02T03:04:05+00:00",
        )


if __name__ == "__main__":
    unittest.main()
