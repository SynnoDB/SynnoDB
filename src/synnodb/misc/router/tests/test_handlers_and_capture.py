from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path

from synnodb.misc.router.pgrouter.protocols.postgres.frontend import handle_frontend_message
from synnodb.misc.router.pgrouter.output import append_query_record, append_result_row
from synnodb.misc.router.pgrouter.query_capture import start_query_tracking
from synnodb.misc.router.pgrouter.state import ColumnInfo, SessionState


INT4_OID = 23
TEXT_OID = 25


def cstring(value: str) -> bytes:
    return value.encode("utf-8") + b"\x00"


def build_parse_payload(stmt_name: str, sql: str, param_type_oids: list[int]) -> bytes:
    payload = cstring(stmt_name)
    payload += cstring(sql)
    payload += struct.pack("!h", len(param_type_oids))
    for oid in param_type_oids:
        payload += struct.pack("!i", oid)
    return payload


def build_bind_payload(
    portal_name: str,
    stmt_name: str,
    param_formats: list[int],
    params: list[bytes | None],
    result_formats: list[int],
) -> bytes:
    payload = cstring(portal_name)
    payload += cstring(stmt_name)
    payload += struct.pack("!h", len(param_formats))
    for fmt in param_formats:
        payload += struct.pack("!h", fmt)
    payload += struct.pack("!h", len(params))
    for param in params:
        if param is None:
            payload += struct.pack("!i", -1)
            continue
        payload += struct.pack("!i", len(param))
        payload += param
    payload += struct.pack("!h", len(result_formats))
    for fmt in result_formats:
        payload += struct.pack("!h", fmt)
    return payload


def build_execute_payload(portal_name: str, max_rows: int = 0) -> bytes:
    return cstring(portal_name) + struct.pack("!i", max_rows)


def make_state(tmpdir: str) -> SessionState:
    return SessionState(
        client_addr="127.0.0.1:55555",
        jsonl_path=str(Path(tmpdir) / "queries.jsonl"),
        upstream_host="127.0.0.1",
        upstream_port=15433,
        catalog_lookup_dsn=None,
        catalog_lookup_password=None,
        results_dir=str(Path(tmpdir) / "results"),
        result_file_format="json",
        session_id="testsess01",
    )


class HandlerAndCaptureUnitTest(unittest.TestCase):
    def test_parse_bind_execute_populates_current_statement_and_parameters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-handler-") as tmpdir:
            state = make_state(tmpdir)
            handle_frontend_message(
                state,
                b"P",
                build_parse_payload(
                    "stmt1",
                    "select $1::int4 as answer, $2::text as label",
                    [INT4_OID, TEXT_OID],
                ),
            )
            handle_frontend_message(
                state,
                b"B",
                build_bind_payload(
                    "portal1",
                    "stmt1",
                    [1, 0],
                    [struct.pack("!i", 42), b"delta"],
                    [1, 0],
                ),
            )
            handle_frontend_message(state, b"E", build_execute_payload("portal1"))

            self.assertEqual(state.prepared_sql["stmt1"], "select $1::int4 as answer, $2::text as label")
            self.assertEqual(state.prepared_param_types["stmt1"], [INT4_OID, TEXT_OID])
            self.assertEqual(state.current_statement_sql, "select $1::int4 as answer, $2::text as label")
            self.assertEqual(state.current_statement_source, "extended")
            self.assertEqual(state.current_statement_id, "testsess01:q0001")
            self.assertEqual(state.current_statement_name, "stmt1")
            self.assertEqual(state.current_portal_name, "portal1")
            self.assertEqual(state.current_statement_execution_index, 1)
            self.assertEqual(state.current_result_format_codes, [1, 0])
            self.assertEqual(len(state.current_parameters), 2)
            self.assertEqual(
                [parameter.value for parameter in state.current_parameters],
                [42, "delta"],
            )
            self.assertEqual(
                [parameter.type_oid for parameter in state.current_parameters],
                [INT4_OID, TEXT_OID],
            )
            self.assertEqual(
                [parameter.format_code for parameter in state.current_parameters],
                [1, 0],
            )

    def test_execute_reuse_increments_statement_execution_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-handler-") as tmpdir:
            state = make_state(tmpdir)
            handle_frontend_message(
                state,
                b"P",
                build_parse_payload(
                    "stmt1",
                    "select $1::int4 as answer",
                    [INT4_OID],
                ),
            )
            handle_frontend_message(
                state,
                b"B",
                build_bind_payload("portal1", "stmt1", [1], [struct.pack("!i", 42)], [1]),
            )
            handle_frontend_message(state, b"E", build_execute_payload("portal1"))
            self.assertEqual(state.current_statement_execution_index, 1)
            self.assertEqual(state.prepared_execute_counts["stmt1"], 1)

            handle_frontend_message(
                state,
                b"B",
                build_bind_payload("portal2", "stmt1", [1], [struct.pack("!i", 7)], [1]),
            )
            handle_frontend_message(state, b"E", build_execute_payload("portal2"))
            self.assertEqual(state.current_statement_name, "stmt1")
            self.assertEqual(state.current_portal_name, "portal2")
            self.assertEqual(state.current_statement_execution_index, 2)
            self.assertEqual(state.prepared_execute_counts["stmt1"], 2)

    def test_append_query_record_aggregates_explicit_transaction_as_one_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-capture-") as tmpdir:
            state = make_state(tmpdir)
            state.backend_pid = 777

            start_query_tracking(state, "begin", "simple")
            state.current_command_tag = "BEGIN"
            state.current_transaction_status_for_query = "I"
            asyncio.run(append_query_record(state))

            state.current_transaction_status = "T"
            state.current_transaction_id = "testsess01:tx0001"
            start_query_tracking(state, "select 1 as value", "simple")
            state.current_transaction_status_for_query = "T"
            state.row_description = [ColumnInfo(name="value", type_oid=INT4_OID, format_code=0)]
            state.current_row_count = 1
            append_result_row(state, {"value": 1})
            state.current_command_tag = "SELECT 1"
            asyncio.run(append_query_record(state))

            start_query_tracking(state, "commit", "simple")
            state.current_transaction_status_for_query = "T"
            state.current_command_tag = "COMMIT"
            asyncio.run(append_query_record(state))

            lines = Path(state.jsonl_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])

            self.assertEqual(record["session_id"], "testsess01")
            self.assertEqual(record["query_id"], "testsess01:tx0001")
            self.assertEqual(record["query"], "begin; select 1 as value; commit")
            self.assertEqual(record["query_source"], "transaction")
            self.assertEqual(record["transaction_id"], "testsess01:tx0001")
            self.assertEqual(record["transaction_status"], "committed")
            self.assertEqual(record["row_count"], 1)
            self.assertIsNone(record["result_file"])
            self.assertEqual(record["parameters"], [])
            self.assertEqual(record["command"], "COMMIT")
            self.assertEqual(record["backend_pid"], 777)
            self.assertGreaterEqual(record["duration_ms"], 0)
            self.assertEqual(record["statement_count"], 3)
            self.assertEqual([statement["query"] for statement in record["statements"]], ["begin", "select 1 as value", "commit"])
            self.assertIsNone(record["statements"][0]["result_file"])
            self.assertTrue(record["statements"][1]["result_file"])
            result_path = Path(record["statements"][1]["result_file"])
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), [{"value": 1}])
            self.assertEqual(record["statements"][1]["result_types"][0]["type_name"], "int4")
            self.assertIsNone(record["statements"][2]["result_file"])
            self.assertEqual(sorted(path.name for path in Path(state.results_dir).iterdir()), ["testsess01:q0002.json"])


if __name__ == "__main__":
    unittest.main()
