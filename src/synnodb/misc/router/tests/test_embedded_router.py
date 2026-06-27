from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from pgrouter import (
    EmbeddedRouter,
    ResultColumn,
    RouterConfig,
    RouteResult,
    Statement,
    StatementContext,
    StatementRoute,
    TransactionControlContext,
    TransactionRoute,
    TransactionStartContext,
    TransactionStatementContext,
)
from pgrouter.protocols.postgres.demo_client import (
    read_until_ready,
    send_bind,
    send_describe,
    send_execute,
    send_parse,
    send_startup,
    send_sync,
    send_terminate,
)
from pgrouter.protocols.postgres.pgtypes import INT4_OID, TEXT_OID

from tests.integration_support import (
    DockerPostgresInstance,
    cleanup_test_postgres_containers,
    load_jsonl_records,
    load_result_records,
    register_test_postgres_cleanup,
    reserve_tcp_port,
    run_psql,
    run_psql_commands,
    wait_for_port,
)


class EmbeddedRouterIntegrationTest(unittest.TestCase):
    trust_pg: DockerPostgresInstance

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not installed")
        if shutil.which("psql") is None:
            raise unittest.SkipTest("psql is not installed")
        cleanup_test_postgres_containers()
        register_test_postgres_cleanup()
        cls.trust_pg = DockerPostgresInstance(auth_mode="trust")
        try:
            cls.trust_pg.start()
        except Exception:
            cls.trust_pg.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "trust_pg"):
            cls.trust_pg.stop()
        cleanup_test_postgres_containers()

    def setUp(self) -> None:
        self.seen_contexts: list[StatementContext] = []
        self.transaction_events: list[tuple[str, object]] = []

    def name_statement_handler(self, context: StatementContext) -> RouteResult:
        self.seen_contexts.append(context)
        name = str(context.parameter_values[0]) if context.parameter_values else "unknown"
        def rows() -> object:
            yield {"id": {"alice": 9001, "bob": 9002}.get(name, 9999), "name": f"hook:{name}"}

        return RouteResult(rows=rows())

    def streaming_statement_handler(self, context: StatementContext) -> RouteResult:
        self.seen_contexts.append(context)

        def rows() -> object:
            yield {"id": 9101, "name": "stream:alice"}
            yield {"id": 9102, "name": "stream:bob"}
            yield {"id": 9103, "name": "stream:carol"}

        return RouteResult(rows=rows())

    def extended_statement_handler(self, context: StatementContext) -> RouteResult:
        self.seen_contexts.append(context)
        answer = int(context.parameter_values[0]) + 1
        label = f"hook:{context.parameter_values[1]}"
        def rows() -> object:
            yield {"answer": answer, "label": label}

        return RouteResult(rows=rows())

    class NamePairTransactionHook:
        def __init__(self, outer: "EmbeddedRouterIntegrationTest", start_context: TransactionStartContext) -> None:
            self.outer = outer
            self.transaction_id = start_context.transaction_id
            self.names: list[str] = []
            self.outer.transaction_events.append(("start", start_context.transaction_id))

        def on_statement(self, context: TransactionStatementContext) -> RouteResult:
            name = str(context.parameter_values[0]) if context.parameter_values else "unknown"
            self.names.append(name)
            self.outer.transaction_events.append(("statement", context.statement_index, name))
            sequence = len(self.names)

            def rows() -> object:
                yield {"id": 9500 + sequence, "name": f"tx:{sequence}:{name}"}

            return RouteResult(rows=rows())

        def on_commit(self, context: TransactionControlContext) -> None:
            self.outer.transaction_events.append(("commit", context.statement_count, tuple(self.names)))
            return None

        def on_rollback(self, context: TransactionControlContext) -> None:
            self.outer.transaction_events.append(("rollback", context.statement_count, tuple(self.names)))
            return None

    def transaction_handler(self, start_context: TransactionStartContext) -> "EmbeddedRouterIntegrationTest.NamePairTransactionHook":
        return EmbeddedRouterIntegrationTest.NamePairTransactionHook(self, start_context)

    class StreamingTransactionHook:
        def __init__(self, outer: "EmbeddedRouterIntegrationTest", start_context: TransactionStartContext) -> None:
            self.outer = outer
            self.transaction_id = start_context.transaction_id
            self.outer.transaction_events.append(("stream-start", start_context.transaction_id))

        def on_statement(self, context: TransactionStatementContext) -> RouteResult:
            self.outer.transaction_events.append(("stream-statement", context.statement_index, context.statement.name))

            async def rows() -> object:
                yield {"id": 9601, "name": "stream-tx:alice"}
                yield {"id": 9602, "name": "stream-tx:bob"}
                yield {"id": 9603, "name": "stream-tx:carol"}

            return RouteResult(rows=rows())

        def on_commit(self, context: TransactionControlContext) -> None:
            self.outer.transaction_events.append(("stream-commit", context.statement_count))
            return None

        def on_rollback(self, context: TransactionControlContext) -> None:
            self.outer.transaction_events.append(("stream-rollback", context.statement_count))
            return None

    def streaming_transaction_handler(
        self,
        start_context: TransactionStartContext,
    ) -> "EmbeddedRouterIntegrationTest.StreamingTransactionHook":
        return EmbeddedRouterIntegrationTest.StreamingTransactionHook(self, start_context)

    def statement_routes(self) -> list[StatementRoute]:
        return [
            StatementRoute(
                statement=Statement(
                    "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
                    result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
                    name="test_name_lookup",
                ),
                handler=self.name_statement_handler,
            ),
            StatementRoute(
                statement=Statement(
                    "SELECT id, name FROM demo ORDER BY id",
                    result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
                    name="test_stream_all_rows",
                ),
                handler=self.streaming_statement_handler,
            ),
            StatementRoute(
                statement=Statement(
                    "SELECT $1::int4 AS answer, $2::text AS label",
                    result_columns=[ResultColumn("answer", INT4_OID), ResultColumn("label", TEXT_OID)],
                    name="test_extended_answer_label",
                ),
                handler=self.extended_statement_handler,
            ),
        ]

    def transaction_routes(self) -> list[TransactionRoute]:
        return [
            TransactionRoute(
                statements=[
                    Statement(
                        "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
                        result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
                        name="tx_first_lookup",
                    ),
                    Statement(
                        "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
                        result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
                        name="tx_second_lookup",
                    ),
                ],
                handler=self.transaction_handler,
                name="name_pair_transaction",
            ),
            TransactionRoute(
                statements=[
                    Statement(
                        "SELECT id, name FROM demo ORDER BY id",
                        result_columns=[ResultColumn("id", INT4_OID), ResultColumn("name", TEXT_OID)],
                        name="tx_stream_all_rows",
                    ),
                ],
                handler=self.streaming_transaction_handler,
                name="stream_all_rows_transaction",
            ),
        ]

    def start_router(self, *, capture_enabled: bool, jsonl_path: str = "queries.jsonl", results_dir: str = "results") -> tuple[EmbeddedRouter, int]:
        port = reserve_tcp_port()
        router = EmbeddedRouter(
            RouterConfig(
                listen_host="127.0.0.1",
                listen_port=port,
                upstream_host="127.0.0.1",
                upstream_port=self.trust_pg.port,
                statement_routes=self.statement_routes(),
                transaction_routes=self.transaction_routes(),
                capture_enabled=capture_enabled,
                jsonl_path=jsonl_path,
                results_dir=results_dir,
            )
        )
        router.start()
        wait_for_port("127.0.0.1", port, timeout_s=10.0)
        return router, port

    def test_embedded_router_intercepts_simple_query_without_capture_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-embedded-") as tmpdir:
            tmp = Path(tmpdir)
            router, port = self.start_router(capture_enabled=False, jsonl_path=str(tmp / "queries.jsonl"), results_dir=str(tmp / "results"))
            try:
                self.assertTrue(router.is_running)
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                result = run_psql(conninfo, "select id, name from demo where name = 'alice' order by id")
                self.assertEqual(result.stdout.strip(), "9001|hook:alice")
                self.assertEqual(len(self.seen_contexts), 1)
                self.assertEqual(self.seen_contexts[0].parameter_values, ("alice",))
                self.assertFalse((tmp / "queries.jsonl").exists())
                self.assertFalse((tmp / "results").exists())
            finally:
                router.stop()
                self.assertFalse(router.is_running)

    def test_embedded_router_context_manager_tracks_running_state(self) -> None:
        port = reserve_tcp_port()
        router = EmbeddedRouter(
            RouterConfig(
                listen_host="127.0.0.1",
                listen_port=port,
                upstream_host="127.0.0.1",
                upstream_port=self.trust_pg.port,
                statement_routes=self.statement_routes(),
                transaction_routes=self.transaction_routes(),
                capture_enabled=False,
            )
        )
        self.assertFalse(router.is_running)
        with router:
            wait_for_port("127.0.0.1", port, timeout_s=10.0)
            self.assertTrue(router.is_running)
        self.assertFalse(router.is_running)

    def test_embedded_router_can_restart_after_stop(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        router.stop()
        self.assertFalse(router.is_running)

        router = EmbeddedRouter(
            RouterConfig(
                listen_host="127.0.0.1",
                listen_port=port,
                upstream_host="127.0.0.1",
                upstream_port=self.trust_pg.port,
                statement_routes=self.statement_routes(),
                transaction_routes=self.transaction_routes(),
                capture_enabled=False,
            )
        )
        try:
            router.start()
            wait_for_port("127.0.0.1", port, timeout_s=10.0)
            self.assertTrue(router.is_running)
        finally:
            router.stop()

    def test_embedded_router_can_capture_intercepted_simple_query(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-embedded-capture-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            router, port = self.start_router(capture_enabled=True, jsonl_path=str(jsonl_path), results_dir=str(results_dir))
            try:
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                result = run_psql(conninfo, "select id, name from demo where name = 'bob' order by id")
                self.assertEqual(result.stdout.strip(), "9002|hook:bob")
                self.assertEqual(self.seen_contexts[-1].parameter_values, ("bob",))
                records = load_jsonl_records(jsonl_path)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["query"], "select id, name from demo where name = 'bob' order by id")
                self.assertEqual(records[0]["row_count"], 1)
                self.assertEqual(load_result_records(records[0]["result_file"], "json"), [{"id": 9002, "name": "hook:bob"}])
            finally:
                router.stop()

    def test_embedded_router_intercepts_stateful_transaction(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        try:
            conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
            result = run_psql_commands(
                conninfo,
                [
                    "begin",
                    "select id, name from demo where name = 'alice' order by id",
                    "select id, name from demo where name = 'bob' order by id",
                    "commit",
                ],
            )
            self.assertEqual(result.stdout.strip().splitlines(), ["9501|tx:1:alice", "9502|tx:2:bob"])
            self.assertEqual(
                self.transaction_events,
                [("start", self.transaction_events[0][1]), ("statement", 1, "alice"), ("statement", 2, "bob"), ("commit", 2, ("alice", "bob"))],
            )
        finally:
            router.stop()

    def test_embedded_router_can_capture_intercepted_transaction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-embedded-tx-capture-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            router, port = self.start_router(capture_enabled=True, jsonl_path=str(jsonl_path), results_dir=str(results_dir))
            try:
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                run_psql_commands(
                    conninfo,
                    [
                        "begin",
                        "select id, name from demo where name = 'alice' order by id",
                        "select id, name from demo where name = 'bob' order by id",
                        "commit",
                    ],
                )
                records = load_jsonl_records(jsonl_path)
                transaction_records = [record for record in records if record["query_source"] == "transaction"]
                self.assertEqual(len(transaction_records), 1)
                statements = transaction_records[0]["statements"]
                self.assertEqual([statement["query"] for statement in statements], [
                    "begin",
                    "select id, name from demo where name = 'alice' order by id",
                    "select id, name from demo where name = 'bob' order by id",
                    "commit",
                ])
                self.assertEqual(load_result_records(statements[1]["result_file"], "json"), [{"id": 9501, "name": "tx:1:alice"}])
                self.assertEqual(load_result_records(statements[2]["result_file"], "json"), [{"id": 9502, "name": "tx:2:bob"}])
            finally:
                router.stop()

    def test_embedded_router_streams_transaction_statement_rows(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        try:
            conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
            result = run_psql_commands(
                conninfo,
                [
                    "begin",
                    "select id, name from demo order by id",
                    "commit",
                ],
            )
            self.assertEqual(
                result.stdout.strip().splitlines(),
                ["9601|stream-tx:alice", "9602|stream-tx:bob", "9603|stream-tx:carol"],
            )
            self.assertEqual(
                self.transaction_events,
                [("stream-start", self.transaction_events[0][1]), ("stream-statement", 1, "tx_stream_all_rows"), ("stream-commit", 1)],
            )
        finally:
            router.stop()

    def test_embedded_router_can_capture_streamed_transaction_statement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-embedded-tx-stream-capture-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            router, port = self.start_router(capture_enabled=True, jsonl_path=str(jsonl_path), results_dir=str(results_dir))
            try:
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                run_psql_commands(
                    conninfo,
                    [
                        "begin",
                        "select id, name from demo order by id",
                        "commit",
                    ],
                )
                records = load_jsonl_records(jsonl_path)
                transaction_records = [record for record in records if record["query_source"] == "transaction"]
                self.assertEqual(len(transaction_records), 1)
                statements = transaction_records[0]["statements"]
                self.assertEqual([statement["query"] for statement in statements], [
                    "begin",
                    "select id, name from demo order by id",
                    "commit",
                ])
                self.assertEqual(
                    load_result_records(statements[1]["result_file"], "json"),
                    [
                        {"id": 9601, "name": "stream-tx:alice"},
                        {"id": 9602, "name": "stream-tx:bob"},
                        {"id": 9603, "name": "stream-tx:carol"},
                    ],
                )
            finally:
                router.stop()

    def test_embedded_router_rejects_incomplete_transaction_commit(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        try:
            conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
            result = subprocess.run(
                [
                    "psql",
                    conninfo,
                    "-qAt",
                    "-c",
                    "begin",
                    "-c",
                    "select id, name from demo where name = 'alice' order by id",
                    "-c",
                    "commit",
                    "-c",
                    "rollback",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertIn("9501|tx:1:alice", result.stdout)
            self.assertIn("transaction handler route is incomplete; rollback is required", result.stderr)
        finally:
            router.stop()

    def test_embedded_router_intercepts_extended_query_with_parameters(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        try:
            with socket.create_connection(("127.0.0.1", port)) as sock:
                send_startup(sock, "app", "app")
                read_until_ready(sock, label="startup", out=lambda *args, **kwargs: None)
                send_parse(sock, "stmt1", "select $1::int4 as answer, $2::text as label", [INT4_OID, TEXT_OID])
                send_bind(sock, "portal1", "stmt1", [0, 0], [b"42", b"delta"], [0, 0])
                send_describe(sock, "P", "portal1")
                send_execute(sock, "portal1")
                send_sync(sock)
                columns, rows, command_tag = read_until_ready(sock, label="extended intercept", out=lambda *args, **kwargs: None)
                self.assertEqual([column.name for column in columns], ["answer", "label"])
                self.assertEqual(rows, [["43", "hook:delta"]])
                self.assertEqual(command_tag, "SELECT 1")
                self.assertEqual(self.seen_contexts[-1].parameter_values, (42, "delta"))
                send_terminate(sock)
        finally:
            router.stop()

    def test_embedded_router_streams_simple_query_rows(self) -> None:
        router, port = self.start_router(capture_enabled=False)
        try:
            conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
            result = run_psql(conninfo, "select id, name from demo order by id")
            self.assertEqual(
                result.stdout.strip().splitlines(),
                ["9101|stream:alice", "9102|stream:bob", "9103|stream:carol"],
            )
        finally:
            router.stop()

    def test_query_route_validates_structure_strings_strictly(self) -> None:
        route = StatementRoute(Statement("SELECT id, name FROM demo WHERE name = %s ORDER BY id"), handler=self.name_statement_handler)
        self.assertEqual(route.normalized_structure, "SELECT id, name FROM demo WHERE name = %s ORDER BY id")

        extended_route = StatementRoute(Statement("SELECT $1::int4 AS answer, $2::text AS label"), handler=self.extended_statement_handler)
        self.assertEqual(
            extended_route.normalized_structure,
            "SELECT CAST(%s AS INT), CAST(%s AS TEXT)",
        )

        transaction_route = TransactionRoute(
            statements=[
                Statement("SELECT id, name FROM demo WHERE name = %s ORDER BY id"),
                Statement("SELECT id, name FROM demo WHERE name = %s ORDER BY id"),
            ],
            handler=self.transaction_handler,
        )
        self.assertEqual(
            transaction_route.normalized_structures,
            (
                "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
                "SELECT id, name FROM demo WHERE name = %s ORDER BY id",
            ),
        )

        with self.assertRaisesRegex(ValueError, "invalid query structure"):
            StatementRoute(Statement("select from"), handler=self.name_statement_handler)

        with self.assertRaisesRegex(ValueError, "invalid query structure"):
            TransactionRoute(statements=[Statement("select from")], handler=self.transaction_handler)

        with self.assertRaisesRegex(ValueError, "unique first statements"):
            RouterConfig(
                listen_host="127.0.0.1",
                listen_port=5432,
                upstream_host="127.0.0.1",
                upstream_port=self.trust_pg.port,
                transaction_routes=[
                    TransactionRoute(
                        statements=[Statement("SELECT id, name FROM demo WHERE name = %s ORDER BY id")],
                        handler=self.transaction_handler,
                        name="route_a",
                    ),
                    TransactionRoute(
                        statements=[
                            Statement("SELECT id, name FROM demo WHERE name = %s ORDER BY id"),
                            Statement("SELECT 1"),
                        ],
                        handler=self.transaction_handler,
                        name="route_b",
                    ),
                ],
            )


if __name__ == "__main__":
    unittest.main()
