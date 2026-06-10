from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pgrouter.output import append_query_record, append_result_row, register_result_writer
from pgrouter.query_capture import start_query_tracking
from pgrouter.state import ColumnInfo, SessionState


class DummyWriter:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.rows: list[dict[str, object]] = []
        self.closed = False

    def append_row(self, row: dict[str, object]) -> None:
        self.rows.append(row)

    def finalize(self) -> str | None:
        self.closed = True
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json.dumps({"rows": self.rows}), encoding="utf-8")
        return str(self.output_path)

    def abort(self) -> None:
        self.closed = True
        self.rows.clear()


def make_state(tmpdir: str, *, result_file_format: str = "json") -> SessionState:
    return SessionState(
        client_addr="127.0.0.1:55555",
        jsonl_path=str(Path(tmpdir) / "queries.jsonl"),
        upstream_host="127.0.0.1",
        upstream_port=15433,
        catalog_lookup_dsn=None,
        catalog_lookup_password=None,
        results_dir=str(Path(tmpdir) / "results"),
        result_file_format=result_file_format,
        session_id="testsess01",
    )


class OutputWriterTest(unittest.TestCase):
    def test_json_result_writer_streams_rows_to_disk(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-output-") as tmpdir:
            state = make_state(tmpdir)
            start_query_tracking(state, "select 1", "simple")
            state.row_description = [ColumnInfo(name="value", type_oid=23, format_code=0)]
            append_result_row(state, {"value": 1})
            temp_files = list(Path(state.results_dir).glob(".*.tmp"))
            self.assertEqual(len(temp_files), 1)

            append_result_row(state, {"value": 2})
            state.current_row_count = 2
            state.current_command_tag = "SELECT 2"
            asyncio.run(append_query_record(state))

            result_path = Path(state.results_dir) / "testsess01:q0001.json"
            self.assertTrue(result_path.exists())
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), [{"value": 1}, {"value": 2}])

    def test_custom_result_writer_can_be_registered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-output-") as tmpdir:
            register_result_writer("dummy", ".dummy", DummyWriter)
            try:
                state = make_state(tmpdir, result_file_format="dummy")
                start_query_tracking(state, "select 1", "simple")
                append_result_row(state, {"value": 1})
                state.current_row_count = 1
                state.current_command_tag = "SELECT 1"
                asyncio.run(append_query_record(state))

                result_path = Path(state.results_dir) / "testsess01:q0001.dummy"
                self.assertTrue(result_path.exists())
                self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), {"rows": [{"value": 1}]})
            finally:
                from pgrouter.output import RESULT_WRITER_FACTORIES

                RESULT_WRITER_FACTORIES.pop("dummy", None)


if __name__ == "__main__":
    unittest.main()
