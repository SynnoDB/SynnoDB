from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.integration_support import (
    DockerPostgresInstance,
    cleanup_test_postgres_containers,
    compile_jdbc_test_client,
    find_postgresql_jdbc_jar,
    load_jsonl_records,
    load_result_records,
    register_test_postgres_cleanup,
    reserve_tcp_port,
    run_jdbc_test_client,
    start_router_process,
    stop_process,
    wait_for_port,
)


class JdbcIntegrationTest(unittest.TestCase):
    trust_pg: DockerPostgresInstance

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not installed")
        if shutil.which("java") is None or shutil.which("javac") is None:
            raise unittest.SkipTest("java and javac are required for JDBC integration tests")

        cls.jdbc_jar = find_postgresql_jdbc_jar()
        if cls.jdbc_jar is None:
            raise unittest.SkipTest("PostgreSQL JDBC jar not found; set PGJDBC_JAR or place one in tests/jdbc/lib/")

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

    def test_jdbc_client_traffic_is_captured(self) -> None:
        port = reserve_tcp_port()
        with tempfile.TemporaryDirectory(prefix="pgrouter-jdbc-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            results_dir = tmp / "results"
            class_output_dir = compile_jdbc_test_client(tmp, self.jdbc_jar)
            proc = start_router_process(
                port=port,
                jsonl_path=jsonl_path,
                results_dir=results_dir,
                upstream=f"127.0.0.1:{self.trust_pg.port}",
            )
            try:
                wait_for_port("127.0.0.1", port, timeout_s=10.0)
                jdbc_url = (
                    f"jdbc:postgresql://127.0.0.1:{port}/app"
                    "?sslmode=disable&preferQueryMode=extended&prepareThreshold=1&ApplicationName=pgrouter-jdbc-test-client"
                )
                run_jdbc_test_client(
                    jdbc_url=jdbc_url,
                    jdbc_jar=self.jdbc_jar,
                    class_output_dir=class_output_dir,
                )

                records = load_jsonl_records(jsonl_path)
                self.assertEqual(len(records), 8)

                self.assertEqual(records[0]["query_source"], "simple")
                self.assertEqual(records[0]["query"], "SET application_name = 'pgrouter-jdbc-test-client'")

                setup_queries = [record["query"] for record in records[1:5]]
                self.assertEqual(
                    setup_queries,
                    [
                        "create table if not exists demo(id serial primary key, name text not null)",
                        "truncate table demo restart identity",
                        "insert into demo(name) values ('alice'), ('bob'), ('carol')",
                        "select count(*) as total_rows from demo",
                    ],
                )
                self.assertEqual(records[4]["row_count"], 1)
                self.assertEqual(load_result_records(records[4]["result_file"], "json"), [{"total_rows": 3}])

                prepared_records = records[5:8]
                self.assertEqual([record["query_source"] for record in prepared_records], ["extended", "extended", "transaction"])

                direct_record = prepared_records[0]
                self.assertEqual(direct_record["query"], "select id, name from demo where name = $1 order by id")
                self.assertEqual(direct_record["parameters"][0]["value"], "alice")
                self.assertEqual(direct_record["row_count"], 1)
                self.assertEqual(load_result_records(direct_record["result_file"], "json"), [{"id": 1, "name": "alice"}])
                self.assertIsNotNone(direct_record["statement_name"])
                self.assertFalse(direct_record["statement_reused"])
                self.assertEqual(direct_record["statement_execution_index"], 1)

                second_direct_record = prepared_records[1]
                self.assertEqual(second_direct_record["query"], "select id, name from demo where name = $1 order by id")
                self.assertEqual(second_direct_record["parameters"][0]["value"], "bob")
                self.assertEqual(second_direct_record["row_count"], 1)
                self.assertEqual(load_result_records(second_direct_record["result_file"], "json"), [{"id": 2, "name": "bob"}])
                self.assertEqual(second_direct_record["statement_name"], direct_record["statement_name"])
                self.assertTrue(second_direct_record["statement_reused"])
                self.assertEqual(second_direct_record["statement_execution_index"], 2)

                transaction_record = prepared_records[2]
                self.assertEqual(transaction_record["transaction_status"], "committed")
                self.assertEqual(transaction_record["statement_count"], 3)
                self.assertEqual(
                    [statement["query"] for statement in transaction_record["statements"]],
                    ["begin", "select id, name from demo where name = $1 order by id", "commit"],
                )
                select_statement = transaction_record["statements"][1]
                self.assertEqual(select_statement["parameters"][0]["value"], "carol")
                self.assertEqual(select_statement["row_count"], 1)
                self.assertEqual(load_result_records(select_statement["result_file"], "json"), [{"id": 3, "name": "carol"}])
                self.assertEqual(select_statement["statement_name"], direct_record["statement_name"])
                self.assertTrue(select_statement["statement_reused"])
                self.assertEqual(select_statement["statement_execution_index"], 3)
            finally:
                stop_process(proc)


if __name__ == "__main__":
    unittest.main()
