from __future__ import annotations

import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from synnodb.misc.router.pgrouter.testclient import (
    BINARY_EXTENDED_QUERY,
    BINARY_EXTENDED_RESULT_TYPES,
    BINARY_EXTENDED_STATEMENT_NAME,
    DEFAULT_QUERIES,
    REPETITION_DEMO_QUERIES,
    TRANSACTION_DEMO_QUERIES,
    binary_extended_executions,
    run_queries,
)

from synnodb.misc.router.tests.integration_support import (
    DockerPostgresInstance,
    cleanup_test_postgres_containers,
    load_jsonl_records,
    load_result_records,
    register_test_postgres_cleanup,
    reserve_tcp_port,
    run_psql,
    run_psql_commands,
    start_router_process,
    stop_process,
    wait_for_port,
    wait_for_process_exit,
)


class RouterIntegrationTest(unittest.TestCase):
    trust_pg: DockerPostgresInstance
    auth_pg: DockerPostgresInstance

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not installed")
        if shutil.which("psql") is None:
            raise unittest.SkipTest("psql is not installed")

        cleanup_test_postgres_containers()
        register_test_postgres_cleanup()

        cls.trust_pg = DockerPostgresInstance(auth_mode="trust")
        cls.auth_pg = DockerPostgresInstance(auth_mode="password")
        try:
            cls.trust_pg.start()
            cls.auth_pg.start()
        except Exception:
            cls.trust_pg.stop()
            cls.auth_pg.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "trust_pg"):
            cls.trust_pg.stop()
        if hasattr(cls, "auth_pg"):
            cls.auth_pg.stop()
        cleanup_test_postgres_containers()

    def run_router_capture_test(self, result_file_format: str) -> None:
        port = reserve_tcp_port()
        with tempfile.TemporaryDirectory(prefix="pgrouter-test-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"

            proc = start_router_process(
                port=port,
                jsonl_path=jsonl_path,
                results_dir=results_dir,
                result_file_format=result_file_format,
                upstream=f"127.0.0.1:{self.trust_pg.port}",
            )
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                run_queries("127.0.0.1", port, "app", "app", DEFAULT_QUERIES, emit_output=False)
                self.assert_router_demo_capture(jsonl_path, results_dir, result_file_format)
            finally:
                stop_process(proc)

    def assert_router_demo_capture(self, jsonl_path: Path, results_dir: Path, result_file_format: str) -> None:
        records = load_jsonl_records(jsonl_path)
        self.assertEqual(len(records), 15)
        self.assert_record_metadata(records)
        self.assert_non_result_queries(records[:3])
        self.assert_repetition_demo_records(records[5:11])
        self.assert_transaction_records(records[11:13], result_file_format)
        self.assert_binary_query_records(records[13:15])
        self.assert_result_file_layout(records, results_dir, result_file_format)
        self.assert_binary_result_rows(records[13:15], result_file_format)

    def assert_record_metadata(self, records: list[dict[str, object]]) -> None:
        session_ids = {entry["session_id"] for entry in records}
        self.assertEqual(len(session_ids), 1)
        query_ids = [entry["query_id"] for entry in records]
        self.assertEqual(len(set(query_ids)), len(query_ids))
        self.assertEqual([entry["row_count"] for entry in records], [0, 0, 0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
        for entry in records:
            self.assertGreaterEqual(entry["duration_ms"], 0)
            self.assertIsNotNone(entry["started_at"])
            self.assertNotIn("finished_at", entry)
        for entry in records[:13]:
            self.assertIsNone(entry["statement_name"])
            self.assertIsNone(entry["portal_name"])
            self.assertIsNone(entry["statement_execution_index"])
            self.assertFalse(entry["statement_reused"])

        select_rows = records[3]
        self.assertTrue(select_rows["result_file"])
        self.assertEqual([t["name"] for t in select_rows["result_types"]], ["id", "name"])
        self.assertEqual([t["format"] for t in select_rows["result_types"]], ["text", "text"])
        self.assertIsNone(select_rows["transaction_id"])
        self.assertEqual(select_rows["transaction_status"], "idle")

        count_rows = records[4]
        self.assertTrue(count_rows["result_file"])
        self.assertEqual([t["name"] for t in count_rows["result_types"]], ["total_rows"])
        self.assertEqual(count_rows["transaction_status"], "idle")

    def assert_repetition_demo_records(self, records: list[dict[str, object]]) -> None:
        self.assertEqual([entry["query"] for entry in records], REPETITION_DEMO_QUERIES)
        for entry in records:
            self.assertEqual(entry["row_count"], 1)
            self.assertTrue(entry["result_file"])
            self.assertEqual(entry["parameters"], [])

    def assert_non_result_queries(self, records: list[dict[str, object]]) -> None:
        for entry in records:
            self.assertIsNone(entry["result_file"])
            self.assertEqual(entry["result_types"], [])
            self.assertEqual(entry["parameters"], [])

    def assert_transaction_records(self, records: list[dict[str, object]], result_file_format: str) -> None:
        self.assertEqual(len(records), 2)
        expected_names = ["alice", "bob"]
        expected_queries = [
            TRANSACTION_DEMO_QUERIES[:3],
            TRANSACTION_DEMO_QUERIES[3:],
        ]
        for record, expected_name, expected_statements in zip(records, expected_names, expected_queries):
            self.assertEqual(record["query_source"], "transaction")
            self.assertEqual(record["transaction_status"], "committed")
            self.assertEqual(record["command"], "COMMIT")
            self.assertEqual(record["row_count"], 1)
            self.assertIsNone(record["result_file"])
            self.assertEqual(record["statement_count"], 3)
            statements = record["statements"]
            self.assertEqual([statement["query"] for statement in statements], expected_statements)
            self.assertEqual(statements[0]["command"], "BEGIN")
            self.assertIsNone(statements[0]["result_file"])
            self.assertEqual(statements[1]["row_count"], 1)
            self.assertTrue(statements[1]["result_file"])
            self.assertEqual(
                load_result_records(statements[1]["result_file"], result_file_format),
                [{"id": 1 if expected_name == "alice" else 2, "name": expected_name}],
            )
            self.assertEqual(statements[2]["command"], "COMMIT")
            self.assertIsNone(statements[2]["result_file"])

    def assert_binary_query_records(self, records: list[dict[str, object]]) -> None:
        expected_executions = binary_extended_executions()
        self.assertEqual(len(records), len(expected_executions))
        for index, (record, execution) in enumerate(zip(records, expected_executions), start=1):
            self.assertEqual(record["query"], BINARY_EXTENDED_QUERY)
            self.assertTrue(record["result_file"])
            self.assertEqual(record["query_source"], "extended")
            self.assertEqual(record["statement_name"], BINARY_EXTENDED_STATEMENT_NAME)
            self.assertEqual(record["portal_name"], execution.portal_name)
            self.assertEqual(record["statement_execution_index"], index)
            self.assertEqual(record["statement_reused"], index > 1)
            self.assertEqual(record["result_types"], BINARY_EXTENDED_RESULT_TYPES)
            self.assertEqual(record["parameters"], list(execution.expected_parameters))

    def assert_result_file_layout(
        self,
        records: list[dict[str, object]],
        results_dir: Path,
        result_file_format: str,
    ) -> None:
        expected_suffix = ".json" if result_file_format == "json" else ".pkl"
        result_files = sorted(results_dir.glob(f"*{expected_suffix}"))
        self.assertEqual(len(result_files), 12)
        session_id = records[0]["session_id"]
        expected_names = [
            f"{session_id}:q0004",
            f"{session_id}:q0005",
            f"{session_id}:q0006",
            f"{session_id}:q0007",
            f"{session_id}:q0008",
            f"{session_id}:q0009",
            f"{session_id}:q0010",
            f"{session_id}:q0011",
            f"{session_id}:q0013",
            f"{session_id}:q0016",
            f"{session_id}:q0018",
            f"{session_id}:q0019",
        ]
        for expected_name, path in zip(expected_names, result_files):
            self.assertEqual(path.suffix, expected_suffix)
            self.assertEqual(path.stem, expected_name)

    def assert_binary_result_rows(self, records: list[dict[str, object]], result_file_format: str) -> None:
        for record, execution in zip(records, binary_extended_executions()):
            self.assertEqual(load_result_records(record["result_file"], result_file_format), [execution.expected_row])

    def test_router_writes_jsonl_and_json_result_files(self) -> None:
        self.run_router_capture_test("json")

    def test_router_writes_jsonl_and_pickle_result_files(self) -> None:
        self.run_router_capture_test("pickle")

    def test_router_streams_large_result_set_to_json_file(self) -> None:
        port = reserve_tcp_port()
        with tempfile.TemporaryDirectory(prefix="pgrouter-large-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            proc = start_router_process(
                port=port,
                jsonl_path=jsonl_path,
                results_dir=results_dir,
                upstream=f"127.0.0.1:{self.trust_pg.port}",
            )
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                sql = "select g as n from generate_series(1, 5000) g"
                run_psql(conninfo, sql, timeout_s=30.0, capture_output=False)

                records = load_jsonl_records(jsonl_path)
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record["row_count"], 5000)
                self.assertEqual(record["query"], sql)
                rows_on_disk = load_result_records(record["result_file"], "json")
                self.assertEqual(len(rows_on_disk), 5000)
                self.assertEqual(rows_on_disk[0], {"n": 1})
                self.assertEqual(rows_on_disk[-1], {"n": 5000})
            finally:
                stop_process(proc)

    def test_router_graceful_shutdown_waits_for_active_session(self) -> None:
        port = reserve_tcp_port()
        with tempfile.TemporaryDirectory(prefix="pgrouter-shutdown-") as tmpdir:
            tmp = Path(tmpdir)
            proc = start_router_process(
                port=port,
                jsonl_path=tmp / "queries.jsonl",
                results_dir=tmp / "results",
                upstream=f"127.0.0.1:{self.trust_pg.port}",
            )
            client_proc: subprocess.Popen[str] | None = None
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                client_proc = subprocess.Popen(
                    ["psql", conninfo, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", "select pg_sleep(2), 1"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                time.sleep(0.3)
                proc.send_signal(signal.SIGINT)
                time.sleep(0.3)
                self.assertIsNone(proc.poll(), "router exited before active session completed")
                self.assertEqual(client_proc.wait(timeout=10), 0)
                self.assertEqual(wait_for_process_exit(proc, 10), 0)
            finally:
                if client_proc is not None:
                    stop_process(client_proc)
                stop_process(proc)

    def test_router_second_sigint_forces_shutdown(self) -> None:
        port = reserve_tcp_port()
        with tempfile.TemporaryDirectory(prefix="pgrouter-force-stop-") as tmpdir:
            tmp = Path(tmpdir)
            proc = start_router_process(
                port=port,
                jsonl_path=tmp / "queries.jsonl",
                results_dir=tmp / "results",
                upstream=f"127.0.0.1:{self.trust_pg.port}",
            )
            client_proc: subprocess.Popen[str] | None = None
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                conninfo = f"postgresql://app@127.0.0.1:{port}/app?sslmode=disable"
                client_proc = subprocess.Popen(
                    ["psql", conninfo, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", "select pg_sleep(10), 1"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                time.sleep(0.3)
                proc.send_signal(signal.SIGINT)
                time.sleep(0.3)
                proc.send_signal(signal.SIGINT)
                self.assertEqual(wait_for_process_exit(proc, 5), 0)
                self.assertNotEqual(client_proc.wait(timeout=5), 0)
            finally:
                if client_proc is not None:
                    stop_process(client_proc)
                stop_process(proc)

    def test_router_supports_password_auth_upstream_and_catalog_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-auth-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            port = reserve_tcp_port()
            proc = start_router_process(
                port=port,
                jsonl_path=jsonl_path,
                results_dir=results_dir,
                upstream=f"127.0.0.1:{self.auth_pg.port}",
                extra_env={"PGROUTER_LOOKUP_PASSWORD": self.auth_pg.password},
                use_uv=True,
            )
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                conninfo = f"postgresql://app:{self.auth_pg.password}@127.0.0.1:{port}/app?sslmode=disable"
                run_psql_commands(
                    conninfo,
                    ["create type mood as enum ('ok', 'bad')", "select 'ok'::mood as state"],
                    timeout_s=30.0,
                )

                records = load_jsonl_records(jsonl_path)
                self.assertEqual(len(records), 2)
                select_record = records[-1]
                self.assertEqual(select_record["query"], "select 'ok'::mood as state")
                self.assertEqual(select_record["row_count"], 1)
                self.assertEqual(select_record["result_types"][0]["name"], "state")
                self.assertEqual(select_record["result_types"][0]["type_name"], "public.mood")
            finally:
                stop_process(proc)


if __name__ == "__main__":
    unittest.main()
