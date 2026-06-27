from __future__ import annotations

import unittest

from pgrouter.testclient import EMBEDDED_DEMO_QUERIES, query_set_for_mode


class DemoClientModeTest(unittest.TestCase):
    def test_embedded_mode_query_set_includes_transaction_demo_and_disables_binary(self) -> None:
        queries, include_binary_extended = query_set_for_mode(for_embedded=True)
        self.assertEqual(list(queries), EMBEDDED_DEMO_QUERIES)
        self.assertFalse(include_binary_extended)
        self.assertIn("begin", queries)
        self.assertIn("commit", queries)
        self.assertEqual(
            [query for query in queries if "select id, name from demo where name =" in query],
            [
                "select id, name from demo where name = 'alice' order by id",
                "select id, name from demo where name = 'alice' order by id",
                "select id, name from demo where name = 'bob' order by id",
            ],
        )


if __name__ == "__main__":
    unittest.main()
