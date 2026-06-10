from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pgrouter.inspect import inspect_main


class InspectToolTest(unittest.TestCase):
    def test_inspect_tool_prints_summary_and_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-inspect-") as tmpdir:
            tmp = Path(tmpdir)
            results_dir = tmp / "results"
            results_dir.mkdir()
            result_path = results_dir / "sess:q0001.json"
            result_path.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")

            jsonl_path = tmp / "queries.jsonl"
            jsonl_path.write_text(
                json.dumps(
                    {
                        "query_id": "sess:q0001",
                        "query": "select id from demo",
                        "row_count": 2,
                        "duration_ms": 1.5,
                        "command": "SELECT 2",
                        "result_file": "results/sess:q0001.json",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = inspect_main(["--jsonl-path", str(jsonl_path), "--show-rows", "1"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("sess:q0001 | rows=2 | dur=1.5ms | cmd=SELECT 2 | select id from demo", rendered)
            self.assertIn('{"id": 1}', rendered)
            self.assertIn("... 1 more row(s)", rendered)

    def test_inspect_tool_prints_transaction_statement_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-inspect-") as tmpdir:
            tmp = Path(tmpdir)
            results_dir = tmp / "results"
            results_dir.mkdir()
            statement_result_path = results_dir / "sess:q0002.json"
            statement_result_path.write_text(json.dumps([{"id": 1, "name": "alice"}]), encoding="utf-8")

            jsonl_path = tmp / "queries.jsonl"
            jsonl_path.write_text(
                json.dumps(
                    {
                        "query_id": "sess:tx0001",
                        "query": "begin; select id, name from demo where name = 'alice'; commit",
                        "row_count": 1,
                        "duration_ms": 2.0,
                        "command": "COMMIT",
                        "query_source": "transaction",
                        "result_file": None,
                        "statements": [
                            {"query": "begin", "command": "BEGIN", "result_file": None},
                            {
                                "query": "select id, name from demo where name = 'alice'",
                                "command": "SELECT 1",
                                "result_file": "results/sess:q0002.json",
                            },
                            {"query": "commit", "command": "COMMIT", "result_file": None},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = inspect_main(["--jsonl-path", str(jsonl_path), "--show-rows", "1"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("sess:tx0001 | rows=1 | dur=2.0ms | cmd=COMMIT | begin; select id, name from demo where name = 'alice'; commit", rendered)
            self.assertIn("statement 2 | cmd=SELECT 1 | select id, name from demo where name = 'alice'", rendered)
            self.assertIn('{"id": 1, "name": "alice"}', rendered)


if __name__ == "__main__":
    unittest.main()
