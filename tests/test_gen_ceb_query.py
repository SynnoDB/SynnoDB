# add parent to path
import sys
import unittest
from pathlib import Path
from typing import List

sys.path.append(str(Path(__file__).resolve().parent.parent))

from workloads.dataset.gen_ceb.gen_ceb_query import _move_is_null_to_in_clause


class TestGenCEBQuery(unittest.TestCase):
    """Test suite for CEB query generation"""

    CEB_DIR = Path("/mnt/labstore/bespoke_olap/datasets/ceb/imdb")

    #     def _test_query_generation(self, query_name: str, num_queries: int = 1):
    #         """Helper method to test query generation for a specific query name"""
    #         template, sql_queries, bindings_list = gen_query(
    #             self.CEB_DIR, query_name=query_name, num_queries=num_queries
    #         )

    #         # print(f"\n{'=' * 60}")
    #         # print(f"Query: {query_name} (num_queries={num_queries})")
    #         # print(f"{'=' * 60}")
    #         # print("Template:")
    #         # print(template)

    #         self.assertTrue(
    #             template.strip().upper().startswith("SELECT"),
    #             "Template should start with SELECT",
    #         )

    #         # Assertions to ensure queries are always generated
    #         self.assertIsNotNone(
    #             sql_queries, f"SQL queries should not be None for {query_name}"
    #         )
    #         self.assertIsInstance(
    #             sql_queries, list, f"SQL queries should be a list for {query_name}"
    #         )
    #         self.assertIsInstance(
    #             sql_queries[0], str, f"Each SQL query should be a string for {query_name}"
    #         )
    #         # sql queries should differ
    #         if num_queries > 1:
    #             self.assertGreater(
    #                 len(set(sql_queries)),
    #                 1,
    #                 f"Generated SQL queries should differ for {query_name}",
    #             )

    #         self.assertIsNotNone(template, f"Template should not be None for {query_name}")
    #         self.assertIsNotNone(
    #             bindings_list, f"Bindings list should not be None for {query_name}"
    #         )

    #         self.assertIsInstance(
    #             sql_queries, list, f"SQL queries should be a list for {query_name}"
    #         )
    #         self.assertIsInstance(
    #             template, str, f"Template should be a string for {query_name}"
    #         )
    #         self.assertIsInstance(
    #             bindings_list, list, f"Bindings list should be a list for {query_name}"
    #         )

    #         self.assertEqual(
    #             len(sql_queries),
    #             num_queries,
    #             f"Should generate {num_queries} SQL queries for {query_name}",
    #         )
    #         self.assertEqual(
    #             len(bindings_list),
    #             num_queries,
    #             f"Should generate {num_queries} bindings for {query_name}",
    #         )

    #         self.assertGreater(
    #             len(template), 0, f"Template should not be empty for query {query_name}"
    #         )

    #         # Verify each generated query
    #         for i, (sql, bindings) in enumerate(zip(sql_queries, bindings_list)):
    #             # print(f"\n--- Query Variant {i + 1} ---")
    #             # print("SQL:")
    #             # print(sql)
    #             # print("\nBindings:")
    #             # for key, val in bindings.items():
    #             #     print(f"  {key}: {val}")

    #             self.assertIsInstance(sql, str, "Each SQL query should be a string")
    #             self.assertIsInstance(
    #                 bindings, dict, "Each bindings entry should be a dict"
    #             )
    #             self.assertGreater(len(sql), 0, f"SQL query {i + 1} should not be empty")

    #         return template, sql_queries, bindings_list

    #     def test_query_3a_single(self):
    #         """Test single query generation for 3a"""
    #         template, sql_queries, bindings_list = self._test_query_generation(
    #             "3a", num_queries=1
    #         )
    #         expected_template = """SELECT COUNT(*) FROM title as t,
    # movie_keyword as mk, keyword as k,
    # movie_companies as mc, company_name as cn,
    # company_type as ct, kind_type as kt,
    # cast_info as ci, name as n, role_type as rt
    # WHERE t.id = mk.movie_id
    # AND t.id = mc.movie_id
    # AND t.id = ci.movie_id
    # AND ci.movie_id = mc.movie_id
    # AND ci.movie_id = mk.movie_id
    # AND mk.movie_id = mc.movie_id
    # AND k.id = mk.keyword_id
    # AND cn.id = mc.company_id
    # AND ct.id = mc.company_type_id
    # AND kt.id = t.kind_id
    # AND ci.person_id = n.id
    # AND ci.role_id = rt.id
    # AND (t.production_year <= YEAR1)
    # AND (t.production_year >= YEAR2)
    # AND (k.keyword IN KEYWORD)
    # AND (cn.country_code IN COUNTRY)
    # AND (ct.kind IN KIND1)
    # AND (kt.kind IN KIND2)
    # AND (rt.role IN ROLE)
    # AND (n.gender IN GENDER)"""

    #         self.maxDiff = None
    #         self.assertEqual(template, expected_template)

    #         expected_bindings = {
    #             "YEAR1": "2015",
    #             "YEAR2": "1900",
    #             "KEYWORD": "('jealousy', 'lesbian', 'new-york-city')",
    #             "COUNTRY": "('[nz]', '[ve]')",
    #             "KIND1": "('distributors')",
    #             "KIND2": "('tv movie', 'tv series', 'video game')",
    #             "ROLE": "('actor', 'miscellaneous crew')",
    #             "GENDER": "('m')",
    #         }
    #         self.assertEqual(bindings_list[0], expected_bindings)

    #     def test_query_3a_batch(self):
    #         """Test batch query generation for 3a"""
    #         self._test_query_generation("3a", num_queries=5)

    #     def test_query_5a_single(self):
    #         """Test single query generation for 5a"""
    #         template, sql_queries, bindings_list = self._test_query_generation(
    #             "5a", num_queries=1
    #         )

    #         print(sql_queries[0])

    #         expected_template = """SELECT COUNT(*)
    # FROM title as t,
    # movie_info as mi1,
    # kind_type as kt,
    # info_type as it1,
    # info_type as it3,
    # info_type as it4,
    # movie_info_idx as mii1,
    # movie_info_idx as mii2,
    # movie_keyword as mk,
    # keyword as k
    # WHERE
    # t.id = mi1.movie_id
    # AND t.id = mii1.movie_id
    # AND t.id = mii2.movie_id
    # AND t.id = mk.movie_id
    # AND mii2.movie_id = mii1.movie_id
    # AND mi1.movie_id = mii1.movie_id
    # AND mk.movie_id = mi1.movie_id
    # AND mk.keyword_id = k.id
    # AND mi1.info_type_id = it1.id
    # AND mii1.info_type_id = it3.id
    # AND mii2.info_type_id = it4.id
    # AND t.kind_id = kt.id
    # AND (kt.kind IN KIND)
    # AND (t.production_year <= YEAR1)
    # AND (t.production_year >= YEAR2)
    # AND (mi1.info IN INFO1)
    # AND (it1.id IN ID1)
    # AND it3.id = ID2
    # AND it4.id = ID3
    # AND (mii2.info ~ '^(?:[1-9]\\d*|0)?(?:\\.\\d+)?$' AND mii2.info::float <= INFO2)
    # AND (mii2.info ~ '^(?:[1-9]\\d*|0)?(?:\\.\\d+)?$' AND INFO3 <= mii2.info::float)
    # AND (mii1.info ~ '^(?:[1-9]\\d*|0)?(?:\\.\\d+)?$' AND INFO4 <= mii1.info::float)
    # AND (mii1.info ~ '^(?:[1-9]\\d*|0)?(?:\\.\\d+)?$' AND mii1.info::float <= INFO5)"""

    #         self.maxDiff = None
    #         self.assertEqual(template, expected_template)

    #         expected_bindings = {
    #             "KIND": "('episode', 'movie', 'video movie')",
    #             "YEAR1": "2015",
    #             "YEAR2": "1975",
    #             "INFO1": "('Color')",
    #             "ID1": "('2')",
    #             "ID2": "100",
    #             "ID3": "101",
    #             "INFO2": "4.0",
    #             "INFO3": "0.0",
    #             "INFO4": "800.0",
    #             "INFO5": "31000.0",
    #         }
    #         self.assertEqual(bindings_list[0], expected_bindings)

    #     def test_query_5a_batch(self):
    #         """Test batch query generation for 5a"""
    #         self._test_query_generation("5a", num_queries=3)

    #     def test_query_10a_single(self):
    #         """Test single query generation for 10a"""
    #         self._test_query_generation("10a", num_queries=1)

    #     def test_query_10a_batch(self):
    #         """Test batch query generation for 10a"""
    #         self._test_query_generation("10a", num_queries=4)

    #     def test_query_11a_single(self):
    #         """Test single query generation for 11a"""
    #         self._test_query_generation("11a", num_queries=1)

    #     def test_query_11a_batch(self):
    #         """Test batch query generation for 11a"""
    #         self._test_query_generation("11a", num_queries=3)

    #     def test_query_1a(self):
    #         """Test query generation for 1a"""
    #         self._test_query_generation("1a", num_queries=2)

    #     def test_query_2a(self):
    #         """Test query generation for 2a"""
    #         self._test_query_generation("2a", num_queries=2)

    #     def test_randomness(self):
    #         """Test that different seeds produce different results"""
    #         import random

    #         rnd1 = random.Random(42)
    #         rnd2 = random.Random(123)

    #         template1, sql1, bindings1 = gen_query(
    #             self.CEB_DIR, query_name="3a", rnd=rnd1, num_queries=3
    #         )

    #         template2, sql2, bindings2 = gen_query(
    #             self.CEB_DIR, query_name="3a", rnd=rnd2, num_queries=3
    #         )

    #         # Templates should be the same
    #         self.assertEqual(template1, template2, "Templates should be identical")

    #         # But generated queries might differ (depending on available bindings)
    #         # We just verify they're both valid
    #         self.assertEqual(len(sql1), 3)
    #         self.assertEqual(len(sql2), 3)
    #         self.assertEqual(len(bindings1), 3)
    #         self.assertEqual(len(bindings2), 3)

    def test_move_is_null_to_in_clause(self):
        query = """SELECT COUNT(*)
FROM title as t,
movie_info as mi1,
kind_type as kt,
info_type as it1,
info_type as it3,
info_type as it4,
movie_info_idx as mii1,
movie_info_idx as mii2,
aka_name as an,
name as n,
info_type as it5,
person_info as pi1,
cast_info as ci,
role_type as rt
WHERE
t.id = mi1.movie_id
AND t.id = ci.movie_id
AND t.id = mii1.movie_id
AND t.id = mii2.movie_id
AND mii2.movie_id = mii1.movie_id
AND mi1.movie_id = mii1.movie_id
AND mi1.info_type_id = it1.id
AND mii1.info_type_id = it3.id
AND mii2.info_type_id = it4.id
AND t.kind_id = kt.id
AND (kt.kind IN ('tv movie','video movie'))
AND (t.production_year <= 2015)
AND (t.production_year >= 1975)
AND (mi1.info IN ('PFM:Video','RAT:1.33 : 1','RAT:1.78 : 1'))
AND (it1.id IN ('15','7'))
AND it3.id = '100'
AND it4.id = '101'
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii2.info::float <= 8.0)
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND 0.0 <= mii2.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND 0.0 <= mii1.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii1.info::float <= 10000.0)
AND n.id = ci.person_id
AND ci.person_id = pi1.person_id
AND it5.id = pi1.info_type_id
AND n.id = pi1.person_id
AND n.id = an.person_id
AND ci.person_id = an.person_id
AND an.person_id = pi1.person_id
AND rt.id = ci.role_id
AND (n.gender in ('f') OR n.gender IS NULL)
AND (n.name_pcode_nf in ('C6235') OR n.name_pcode_nf IS NULL)
AND (ci.note in ('(archive footage)') OR ci.note IS NULL)
AND (rt.role in ('actress'))
AND (it5.id in ('26'))"""
        query_lines: List[str] = query.splitlines()  # type: ignore
        rewritten = _move_is_null_to_in_clause(
            query_lines,
            target_rows_cols=[
                (45, "n.gender"),
                (46, "n.name_pcode_nf"),
                (47, "ci.note"),
            ],
        )
        assert rewritten is not None, "Rewritten query should not be None"

        assert rewritten[45] == "AND (n.gender in ('f',NULL))", (
            f"Unexpected rewrite: {rewritten[45]}"
        )
        assert rewritten[46] == "AND (n.name_pcode_nf in ('C6235',NULL))", (
            f"Unexpected rewrite: {rewritten[46]}"
        )
        assert rewritten[47] == "AND (ci.note in ('(archive footage)',NULL))", (
            f"Unexpected rewrite: {rewritten[47]}"
        )

        sql = """SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n,
movie_keyword as mk,
keyword as k
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND t.id = mk.movie_id
AND k.id = mk.keyword_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND (it1.id in ('18'))
AND (it2.id in ('2'))
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (mi1.info in ('Desilu Studios - 9336 W. Washington Blvd., Culver City, California, USA','General Service Studios - 1040 N. Las Palmas, Hollywood, Los Angeles, California, USA','Hal Roach Studios - 8822 Washington Blvd., Culver City, California, USA','Metro-Goldwyn-Mayer Studios - 10202 W. Washington Blvd., Culver City, California, USA','Mexico','New York City, New York, USA','Paramount Studios - 5555 Melrose Avenue, Hollywood, Los Angeles, California, USA','Republic Studios - 4024 Radford Avenue, North Hollywood, Los Angeles, California, USA','Revue Studios, Hollywood, Los Angeles, California, USA','Universal Studios - 100 Universal City Plaza, Universal City, California, USA','Warner Brothers Burbank Studios - 4000 Warner Boulevard, Burbank, California, USA'))
AND (mi2.info in ('Black and White','Color'))
AND (kt.kind in ('episode','movie','tv movie'))
AND (rt.role in ('director','miscellaneous crew'))
AND (n.gender in ('f') OR n.gender IS NULL)
AND (t.production_year <= 1975)
AND (t.production_year >= 1875)"""
        query_lines = sql.splitlines()  # type: ignore
        rewritten = _move_is_null_to_in_clause(
            query_lines, target_rows_cols=[(29, "n.gender")]
        )

        assert rewritten is not None, "Rewritten query should not be None"
        (
            self.assertEqual(rewritten[29], "AND (n.gender in ('f',NULL))"),
            f"Unexpected rewrite: {rewritten[29]}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
