from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from synnodb.misc.router.pgrouter.analyze import NORMALIZATION_RULES, analyze_main, find_repetitions, normalize_query_structure


class AnalyzeToolTest(unittest.TestCase):
    def test_normalization_rules_are_named_and_ordered(self) -> None:
        self.assertEqual(
            [rule.name for rule in NORMALIZATION_RULES],
            [
                "literal-and-parameter-placeholders",
                "remove-output-aliases",
                "canonicalize-comparison-direction",
                "canonicalize-and-predicates",
                "canonicalize-in-lists",
                "canonicalize-inner-joins",
                "canonicalize-commutative-expressions",
            ],
        )

    def test_normalize_query_structure_collapses_literals_commutative_addition_and_aliases(self) -> None:
        self.assertEqual(
            normalize_query_structure("select id, name from demo where name = 'alice' order by id"),
            normalize_query_structure("select id, name from demo where name = 'bob' order by id"),
        )
        self.assertEqual(
            normalize_query_structure("select id + 10 as shifted_id from demo where id = 1"),
            normalize_query_structure("select 10 + id as shifted_id from demo where id = 1"),
        )
        self.assertEqual(
            normalize_query_structure("select id as demo_id from demo where id = 1"),
            normalize_query_structure("select id as local_id from demo where id = 1"),
        )

    def test_normalize_query_structure_canonicalizes_filters_and_joins(self) -> None:
        self.assertEqual(
            normalize_query_structure("select * from demo where a = 1 and b = 2 and c = 3"),
            normalize_query_structure("select * from demo where c = 3 and a = 1 and b = 2"),
        )
        self.assertEqual(
            normalize_query_structure("select * from demo where 1 = a"),
            normalize_query_structure("select * from demo where a = 1"),
        )
        self.assertEqual(
            normalize_query_structure("select * from demo where id in (1, 2, 3)"),
            normalize_query_structure("select * from demo where id in (3, 2, 1)"),
        )
        self.assertEqual(
            normalize_query_structure(
                "select * from demo d join accounts a on d.account_id = a.id join tenants t on a.tenant_id = t.id"
            ),
            normalize_query_structure(
                "select * from accounts a join tenants t on a.tenant_id = t.id join demo d on d.account_id = a.id"
            ),
        )

    def test_find_repetitions_groups_parameterized_and_commutative_queries(self) -> None:
        records = [
            {"query_id": "sess:q0001", "query": "select id, name from demo where name = 'alice' order by id", "query_source": "simple", "duration_ms": 1.0, "row_count": 1, "parameters": []},
            {"query_id": "sess:q0002", "query": "select id, name from demo where name = 'bob' order by id", "query_source": "simple", "duration_ms": 1.1, "row_count": 1, "parameters": []},
            {"query_id": "sess:q0003", "query": "select id + 10 as shifted_id from demo where id = 1", "query_source": "simple", "duration_ms": 0.7, "row_count": 1, "parameters": []},
            {"query_id": "sess:q0004", "query": "select 10 + id as shifted_id from demo where id = 1", "query_source": "simple", "duration_ms": 0.8, "row_count": 1, "parameters": []},
            {"query_id": "sess:q0004b", "query": "select id as demo_id from demo where id = 1", "query_source": "simple", "duration_ms": 0.85, "row_count": 1, "parameters": []},
            {"query_id": "sess:q0004c", "query": "select id as local_id from demo where id = 1", "query_source": "simple", "duration_ms": 0.86, "row_count": 1, "parameters": []},
            {
                "query_id": "sess:q0005",
                "query": "select $1::int4 as binary_int, $2::text as binary_text",
                "query_source": "extended",
                "duration_ms": 0.9,
                "row_count": 1,
                "parameters": [{"index": 1, "value": 42}, {"index": 2, "value": "delta"}],
            },
            {
                "query_id": "sess:q0006",
                "query": "select $1::int4 as binary_int, $2::text as binary_text",
                "query_source": "extended",
                "duration_ms": 1.0,
                "row_count": 1,
                "parameters": [{"index": 1, "value": 7}, {"index": 2, "value": "echo"}],
            },
        ]

        repetitions = find_repetitions(records)
        self.assertEqual(len(repetitions), 4)
        structures = {item.normalized_query: item for item in repetitions}

        names_group = structures[normalize_query_structure("select id, name from demo where name = 'alice' order by id")]
        self.assertEqual(names_group.count, 2)
        self.assertEqual(names_group.distinct_value_count, 2)

        arithmetic_group = structures[normalize_query_structure("select id + 10 as shifted_id from demo where id = 1")]
        self.assertEqual(arithmetic_group.count, 2)
        self.assertEqual(arithmetic_group.distinct_query_count, 2)

        alias_group = structures[normalize_query_structure("select id as demo_id from demo where id = 1")]
        self.assertEqual(alias_group.count, 2)
        self.assertEqual(alias_group.distinct_query_count, 2)

        binary_group = structures[normalize_query_structure("select $1::int4 as binary_int, $2::text as binary_text")]
        self.assertEqual(binary_group.count, 2)
        self.assertEqual(binary_group.distinct_value_count, 2)

    def test_find_repetitions_groups_transactions_by_normalized_statement_sequence(self) -> None:
        records = [
            {
                "query_id": "sess:tx0001",
                "query": "begin; select id from demo where name = 'alice'; commit",
                "query_source": "transaction",
                "duration_ms": 2.0,
                "row_count": 1,
                "statements": [
                    {"query": "begin", "parameters": []},
                    {"query": "select id from demo where name = 'alice'", "parameters": []},
                    {"query": "commit", "parameters": []},
                ],
            },
            {
                "query_id": "sess:tx0002",
                "query": "begin; select id from demo where name = 'bob'; commit",
                "query_source": "transaction",
                "duration_ms": 2.1,
                "row_count": 1,
                "statements": [
                    {"query": "begin", "parameters": []},
                    {"query": "select id from demo where name = 'bob'", "parameters": []},
                    {"query": "commit", "parameters": []},
                ],
            },
        ]

        repetitions = find_repetitions(records)
        self.assertEqual(len(repetitions), 1)
        repetition = repetitions[0]
        self.assertEqual(
            repetition.normalized_query,
            "BEGIN ; SELECT id FROM demo WHERE name = %s ; COMMIT",
        )
        self.assertEqual(repetition.count, 2)
        self.assertEqual(repetition.distinct_value_count, 2)
        self.assertEqual(repetition.queries[0]["values"], ["alice"])

    def test_analyze_tool_prints_groups(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-analyze-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            records = [
                {"query_id": "sess:q0001", "query": "select id, name from demo where name = 'alice' order by id", "row_count": 1, "duration_ms": 1.0, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0002", "query": "select id, name from demo where name = 'bob' order by id", "row_count": 1, "duration_ms": 1.2, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0003", "query": "select id + 10 as shifted_id from demo where id = 1", "row_count": 1, "duration_ms": 0.7, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0004", "query": "select 10 + id as shifted_id from demo where id = 1", "row_count": 1, "duration_ms": 0.8, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0005", "query": "select id as demo_id from demo where id = 1", "row_count": 1, "duration_ms": 0.9, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0006", "query": "select id as local_id from demo where id = 1", "row_count": 1, "duration_ms": 1.0, "query_source": "simple", "parameters": []},
            ]
            jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = analyze_main(["--jsonl-path", str(jsonl_path), "--limit", "10"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("group 1 | count=2", rendered)
            self.assertIn("structure: SELECT", rendered)
            self.assertIn("sess:q0001", rendered)
            self.assertIn('values=["alice"]', rendered)
            self.assertIn("sess:q0004", rendered)
            self.assertIn("sess:q0006", rendered)

    def test_analyze_tool_aggregate_collapses_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-analyze-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            records = [
                {"query_id": "sess1:q0001", "query": "select id, name from demo where name = 'alice' order by id", "row_count": 1, "duration_ms": 1.0, "query_source": "simple", "parameters": []},
                {"query_id": "sess1:q0002", "query": "select id, name from demo where name = 'bob' order by id", "row_count": 1, "duration_ms": 1.1, "query_source": "simple", "parameters": []},
                {"query_id": "sess2:q0001", "query": "select id, name from demo where name = 'alice' order by id", "row_count": 1, "duration_ms": 1.2, "query_source": "simple", "parameters": []},
                {"query_id": "sess2:q0002", "query": "select id, name from demo where name = 'bob' order by id", "row_count": 1, "duration_ms": 1.3, "query_source": "simple", "parameters": []},
            ]
            jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = analyze_main(["--jsonl-path", str(jsonl_path), "--aggregate"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("group 1 | reuse_score=8 | runs=4", rendered)
            self.assertIn(
                'values=["alice"] | runs=2 | example=select id, name from demo where name = \'alice\' order by id',
                rendered,
            )
            self.assertIn(
                'values=["bob"] | runs=2 | example=select id, name from demo where name = \'bob\' order by id',
                rendered,
            )
            self.assertNotIn("sess2:q0001", rendered)

    def test_analyze_tool_aggregate_ranks_by_reuse_score(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-analyze-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            records = [
                {"query_id": "sess:q0001", "query": "select * from demo where name = 'alice'", "row_count": 1, "duration_ms": 1.0, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0002", "query": "select * from demo where name = 'bob'", "row_count": 1, "duration_ms": 1.1, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0003", "query": "select * from demo where name = 'carol'", "row_count": 1, "duration_ms": 1.2, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0004", "query": "select * from demo where id = 1", "row_count": 1, "duration_ms": 1.3, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0005", "query": "select * from demo where 1 = id", "row_count": 1, "duration_ms": 1.4, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0006", "query": "select * from demo where id = 1", "row_count": 1, "duration_ms": 1.5, "query_source": "simple", "parameters": []},
                {"query_id": "sess:q0007", "query": "select * from demo where 1 = id", "row_count": 1, "duration_ms": 1.6, "query_source": "simple", "parameters": []},
            ]
            jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = analyze_main(["--jsonl-path", str(jsonl_path), "--aggregate", "--limit", "2"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            lines = [line for line in rendered.splitlines() if line.startswith("group ")]
            self.assertEqual(
                lines[:2],
                [
                    "group 1 | reuse_score=9 | runs=3 | query_variants=3 | value_variants=3",
                    "group 2 | reuse_score=8 | runs=4 | query_variants=2 | value_variants=1",
                ],
            )

    def test_analyze_tool_aggregate_prefers_original_structure_for_single_query_variant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-analyze-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            query = (
                "select $1::int4 as binary_int, $2::text as binary_text, $3::bool as binary_bool, "
                "$4::float8 as binary_float8"
            )
            records = [
                {
                    "query_id": "sess:q0001",
                    "query": query,
                    "row_count": 1,
                    "duration_ms": 1.0,
                    "query_source": "extended",
                    "parameters": [{"index": 1, "value": 42}, {"index": 2, "value": "delta"}, {"index": 3, "value": True}, {"index": 4, "value": 3.5}],
                },
                {
                    "query_id": "sess:q0002",
                    "query": query,
                    "row_count": 1,
                    "duration_ms": 1.1,
                    "query_source": "extended",
                    "parameters": [{"index": 1, "value": 7}, {"index": 2, "value": "echo"}, {"index": 3, "value": False}, {"index": 4, "value": 9.25}],
                },
            ]
            jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = analyze_main(["--jsonl-path", str(jsonl_path), "--aggregate"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn(f"structure: {query}", rendered)
            self.assertNotIn("structure: SELECT CAST(%s AS INT)", rendered)
            self.assertNotIn("example=", rendered)

    def test_analyze_tool_aggregate_unwraps_jsonb_values_for_display(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pgrouter-analyze-") as tmpdir:
            tmp = Path(tmpdir)
            jsonl_path = tmp / "queries.jsonl"
            query = "select $1::jsonb as payload"
            records = [
                {
                    "query_id": "sess:q0001",
                    "query": query,
                    "row_count": 1,
                    "duration_ms": 1.0,
                    "query_source": "extended",
                    "parameters": [{"index": 1, "value": {"jsonb_version": 1, "value": '{"k":"v"}'}}],
                },
                {
                    "query_id": "sess:q0002",
                    "query": query,
                    "row_count": 1,
                    "duration_ms": 1.1,
                    "query_source": "extended",
                    "parameters": [{"index": 1, "value": {"jsonb_version": 1, "value": '{"k":"w"}'}}],
                },
            ]
            jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = analyze_main(["--jsonl-path", str(jsonl_path), "--aggregate"])

            rendered = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn('values=[{"k": "v"}] | runs=1', rendered)
            self.assertIn('values=[{"k": "w"}] | runs=1', rendered)
            self.assertNotIn('"jsonb_version": 1', rendered)


if __name__ == "__main__":
    unittest.main()
