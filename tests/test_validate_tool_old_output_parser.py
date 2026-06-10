import os
import sys
import unittest

# add parent to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestValidateToolOldOutputParser(unittest.TestCase):
    def test_parse_single_query(self):
        result = _parse_query_items("Query 1: 12.34")
        self.assertEqual(result, {"1": 12.34})

    def test_parse_multiple_queries(self):
        result = _parse_query_items("Query 1: 12.34, Query 2: 56.78")
        self.assertEqual(result, {"1": 12.34, "2": 56.78})

    def test_parse_empty_string(self):
        result = _parse_query_items("")
        self.assertEqual(result, {})

    def test_parse_with_whitespace(self):
        result = _parse_query_items("Query  1 : 10.5, Query  2 : 20.3")
        self.assertEqual(result, {"1": 10.5, "2": 20.3})

    def test_parse_runtime_report(self):
        report = """All results match!\nDuckDB runtimes (ms): Query 1: 5739.07, Query 2: 529.77, Query 3: 3047.45, Query 4: 2919.54, Query 5: 2459.23, Query 6: 905.11, Query 7: 2631.29, Query 8: 3056.15, Query 9: 10024.13, Query 10: 6066.73, Query 11: 527.18, Query 12: 4287.01, Query 13: 7035.10, Query 14: 2958.49, Query 15: 1851.08, Query 16: 1144.97, Query 17: 3433.20, Query 18: 8439.20, Query 19: 5577.41, Query 20: 1850.46, Query 21: 7780.51, Query 22: 1240.08 - sum: 83503.14\nYour Implementation runtimes (ms): Query 1: 3527.00, Query 2: 1068.00, Query 3: 3319.00, Query 4: 4444.00, Query 5: 2700.00, Query 6: 468.00, Query 7: 8517.00, Query 8: 3944.00, Query 9: 17956.00, Query 10: 3855.00, Query 11: 238.00, Query 12: 7203.00, Query 13: 10512.00, Query 14: 1096.00, Query 15: 1083.00, Query 16: 2271.00, Query 17: 45396.00, Query 18: 4639.00, Query 19: 7287.00, Query 20: 11508.00, Query 21: 7907.00, Query 22: 2359.00 - sum: 151297.00\nIngest time (ms): 119928"""

        result = parse_old_runtime_report(report)

        # Calculate average speedup from individual query runtimes
        duckdb_rts = {
            "1": 5739.07,
            "2": 529.77,
            "3": 3047.45,
            "4": 2919.54,
            "5": 2459.23,
            "6": 905.11,
            "7": 2631.29,
            "8": 3056.15,
            "9": 10024.13,
            "10": 6066.73,
            "11": 527.18,
            "12": 4287.01,
            "13": 7035.10,
            "14": 2958.49,
            "15": 1851.08,
            "16": 1144.97,
            "17": 3433.20,
            "18": 8439.20,
            "19": 5577.41,
            "20": 1850.46,
            "21": 7780.51,
            "22": 1240.08,
        }
        impl_rts = {
            "1": 3527.00,
            "2": 1068.00,
            "3": 3319.00,
            "4": 4444.00,
            "5": 2700.00,
            "6": 468.00,
            "7": 8517.00,
            "8": 3944.00,
            "9": 17956.00,
            "10": 3855.00,
            "11": 238.00,
            "12": 7203.00,
            "13": 10512.00,
            "14": 1096.00,
            "15": 1083.00,
            "16": 2271.00,
            "17": 45396.00,
            "18": 4639.00,
            "19": 7287.00,
            "20": 11508.00,
            "21": 7907.00,
            "22": 2359.00,
        }

        speedups = [duckdb_rts[q] / impl_rts[q] for q in duckdb_rts.keys()]
        avg_speedup = sum(speedups) / len(speedups)
        total_speedup = 83503.14 / 151297.0

        expected = {
            "validation/ingest_time_ms": 119928,
            "validation/correct": True,
            "validation/error": False,
            "validation/total_duckdb_runtime_ms": 83503.14,
            "validation/total_impl_runtime_ms": 151297.0,
            "validation/total_speedup": total_speedup,
            "validation/avg_speedup": avg_speedup,
            "validation/num_queries": 22,
            "validation/num_successful_queries": 22,
            "validation/all_queries": True,
            "validation/all_queries_avg_speedup": avg_speedup,
            "validation/all_queries_total_speedup": total_speedup,
        }

        # Add per-query metrics
        for q in duckdb_rts.keys():
            expected[f"validation/query_{q}/duckdb_runtime_ms"] = duckdb_rts[q]
            expected[f"validation/query_{q}/impl_runtime_ms"] = impl_rts[q]
            expected[f"validation/query_{q}/speedup"] = duckdb_rts[q] / impl_rts[q]

        # make sure same keys
        self.assertEqual(set(result.keys()), set(expected.keys()))

        for key, value in expected.items():
            if isinstance(value, float):
                self.assertAlmostEqual(result[key], value, places=5)
            else:
                self.assertEqual(result[key], value)


if __name__ == "__main__":
    unittest.main()
