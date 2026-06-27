from __future__ import annotations

import argparse
import unittest

from synnodb.misc.router.pgrouter.cli import parse_host_port


class CliParseTest(unittest.TestCase):
    def test_parse_host_port_defaults_port_when_missing(self) -> None:
        self.assertEqual(parse_host_port("127.0.0.1", 5432), ("127.0.0.1", 5432))

    def test_parse_host_port_reads_explicit_port(self) -> None:
        self.assertEqual(parse_host_port("127.0.0.1:15433", 5432), ("127.0.0.1", 15433))

    def test_parse_host_port_rejects_invalid_port(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_host_port("127.0.0.1:notaport", 5432)


if __name__ == "__main__":
    unittest.main()
