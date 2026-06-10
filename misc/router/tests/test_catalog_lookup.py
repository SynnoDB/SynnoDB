from __future__ import annotations

import unittest
from unittest import mock

from pgrouter.protocols.postgres.catalog import build_catalog_lookup_connect_kwargs, fetch_type_names_from_catalog_sync
from pgrouter.state import SessionState


def make_state() -> SessionState:
    state = SessionState(
        client_addr="127.0.0.1:55555",
        jsonl_path="queries.jsonl",
        upstream_host="127.0.0.1",
        upstream_port=15433,
        catalog_lookup_dsn=None,
        catalog_lookup_password=None,
        results_dir="results",
        result_file_format="json",
        session_id="testsess01",
    )
    state.startup_params = {"user": "app", "database": "app"}
    return state


class CatalogLookupTest(unittest.TestCase):
    def test_build_catalog_lookup_kwargs_from_startup_and_password(self) -> None:
        state = make_state()
        state.catalog_lookup_password = "secret"

        self.assertEqual(
            build_catalog_lookup_connect_kwargs(state),
            {
                "host": "127.0.0.1",
                "port": 15433,
                "user": "app",
                "dbname": "app",
                "application_name": "pg-router-type-lookup",
                "password": "secret",
            },
        )

    def test_build_catalog_lookup_kwargs_prefers_explicit_dsn(self) -> None:
        state = make_state()
        state.catalog_lookup_dsn = "postgresql://other:pw@db.example/testdb?sslmode=disable"

        self.assertEqual(
            build_catalog_lookup_connect_kwargs(state),
            {
                "conninfo": "postgresql://other:pw@db.example/testdb?sslmode=disable",
                "application_name": "pg-router-type-lookup",
                "sslmode": "disable",
            },
        )

    def test_fetch_type_names_uses_psycopg_connect(self) -> None:
        state = make_state()
        rows = [(23, "int4"), (25, "text")]

        fake_cursor = mock.MagicMock()
        fake_cursor.__enter__.return_value = fake_cursor
        fake_cursor.fetchall.return_value = rows

        fake_connection = mock.MagicMock()
        fake_connection.__enter__.return_value = fake_connection
        fake_connection.cursor.return_value = fake_cursor

        fake_psycopg = mock.MagicMock()
        fake_psycopg.connect.return_value = fake_connection

        with mock.patch("pgrouter.protocols.postgres.catalog.psycopg", fake_psycopg):
            result = fetch_type_names_from_catalog_sync(state, [25, 23, 25])

        self.assertEqual(result, {23: "int4", 25: "text"})
        fake_psycopg.connect.assert_called_once_with(
            host="127.0.0.1",
            port=15433,
            user="app",
            dbname="app",
            application_name="pg-router-type-lookup",
        )
        fake_cursor.execute.assert_called_once()
        executed_sql, executed_params = fake_cursor.execute.call_args.args
        self.assertIn("where t.oid = any(%s)", executed_sql.lower())
        self.assertEqual(executed_params, ([23, 25],))


if __name__ == "__main__":
    unittest.main()
