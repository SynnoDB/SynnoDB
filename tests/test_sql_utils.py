import unittest

from synnodb.utils.sql_utils import extract_order_by_columns


class TestExtractOrderByColumns(unittest.TestCase):
    def test_single_column(self):
        sql = "SELECT * FROM users ORDER BY id"
        self.assertEqual(extract_order_by_columns(sql), [("id", "ASC")])

    def test_multiple_columns(self):
        sql = "SELECT * FROM users ORDER BY last_name, first_name"
        self.assertEqual(
            extract_order_by_columns(sql), [("last_name", "ASC"), ("first_name", "ASC")]
        )

    def test_case_insensitive(self):
        sql = "SELECT * FROM users order by name"
        self.assertEqual(extract_order_by_columns(sql), [("name", "ASC")])

    def test_no_order_by(self):
        sql = "SELECT * FROM users"
        self.assertEqual(extract_order_by_columns(sql), [])

    def test_with_whitespace(self):
        sql = "SELECT * FROM users ORDER BY  col1 ,  col2  , col3"
        self.assertEqual(
            extract_order_by_columns(sql),
            [("col1", "ASC"), ("col2", "ASC"), ("col3", "ASC")],
        )

    def test_mixed_case_order_by_keyword(self):
        sql = "SELECT * FROM users OrDeR bY status"
        self.assertEqual(extract_order_by_columns(sql), [("status", "ASC")])

    def test_empty_query(self):
        sql = ""
        self.assertEqual(extract_order_by_columns(sql), [])

    def test_tpch(self):
        sql = """select
    n_name,
    c_address,
    c_comment,
    revenue
from customer
order by
    revenue desc; """

        self.assertEqual(
            extract_order_by_columns(sql),
            [("revenue", "DESC")],
        )

    def test_tpch_q1_multiple_asc(self):
        sql = """select 
    l_returnflag,  
    l_linestatus
from  
    lineitem 
order by  
    l_returnflag,  
    l_linestatus;"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [("l_returnflag", "ASC"), ("l_linestatus", "ASC")],
        )

    def test_tpch_q2_mixed_asc_desc(self):
        sql = """select s_acctbal
from supplier
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [
                ("s_acctbal", "DESC"),
                ("n_name", "ASC"),
                ("s_name", "ASC"),
                ("p_partkey", "ASC"),
            ],
        )

    def test_tpch_q9_mixed_order(self):
        sql = """select nation, o_year
from profit
order by  
    nation,  
    o_year desc;"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [("nation", "ASC"), ("o_year", "DESC")],
        )

    def test_tpch_q13_multiple_desc(self):
        sql = """select c_count, custdist
from c_orders
order by  
    custdist desc,  
    c_count desc;"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [("custdist", "DESC"), ("c_count", "DESC")],
        )

    def test_tpch_q16_four_columns_mixed(self):
        sql = """select p_brand, p_type, p_size
from partsupp
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [
                ("supplier_cnt", "DESC"),
                ("p_brand", "ASC"),
                ("p_type", "ASC"),
                ("p_size", "ASC"),
            ],
        )

    def test_inner_window_order_by_ignored(self):
        # The ORDER BY inside the window function must not be picked up; only the
        # top-level ORDER BY year, n desc determines the result row order.
        sql = """select rc.date_year as year, rc.country, count(*) as n
from release_country rc
where rc.date_year between 1989 and 1992
group by rc.date_year, rc.country
qualify row_number() over (partition by rc.date_year order by count(*) desc) <= 2
order by year, n desc"""
        self.assertEqual(
            extract_order_by_columns(sql),
            [("year", "ASC"), ("n", "DESC")],
        )

    def test_only_window_order_by_returns_empty(self):
        # The single ORDER BY lives inside a window function, so there is no
        # top-level ordering and the result must be empty.
        sql = """select a, count(*) as n
from t
group by a
qualify row_number() over (order by count(*) desc) <= 2"""
        self.assertEqual(extract_order_by_columns(sql), [])

    def test_inner_subquery_order_by_ignored(self):
        sql = """select x
from (select x from t order by y desc) sub
order by x"""
        self.assertEqual(extract_order_by_columns(sql), [("x", "ASC")])

    def test_top_level_order_by_with_limit(self):
        sql = "select a, b from t order by a desc, b limit 10"
        self.assertEqual(
            extract_order_by_columns(sql),
            [("a", "DESC"), ("b", "ASC")],
        )

    def test_order_by_count_star(self):
        # sqlglot canonicalizes function names to uppercase; the validation consumer
        # compares case-insensitively (col.lower() == "count(*)"), so this is expected.
        sql = "select a, count(*) from t group by a order by count(*) desc, a"
        self.assertEqual(
            extract_order_by_columns(sql),
            [("COUNT(*)", "DESC"), ("a", "ASC")],
        )

    def test_set_operation_top_level_order_by(self):
        sql = "select a from t union select b from s order by a"
        self.assertEqual(extract_order_by_columns(sql), [("a", "ASC")])

    def test_unparseable_returns_empty(self):
        sql = "this is not sql"
        self.assertEqual(extract_order_by_columns(sql), [])


class TestExtractOrderByColumnsExpressions(unittest.TestCase):
    """Ordering directions and non-trivial ORDER BY expressions."""

    def test_explicit_asc_keyword(self):
        sql = "select a from t order by a asc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "ASC")])

    def test_positional_references(self):
        # ORDER BY 1, 2 references select-list positions; they survive as literals.
        sql = "select a, b from t order by 1, 2 desc"
        self.assertEqual(extract_order_by_columns(sql), [("1", "ASC"), ("2", "DESC")])

    def test_qualified_column(self):
        sql = "select t.a from t order by t.a desc"
        self.assertEqual(extract_order_by_columns(sql), [("t.a", "DESC")])

    def test_arithmetic_expression(self):
        # sqlglot re-emits with spaces around the operator.
        sql = "select a, b from t order by a + b desc"
        self.assertEqual(extract_order_by_columns(sql), [("a + b", "DESC")])

    def test_parenthesized_expression(self):
        sql = "select a from t order by (a + b) desc"
        self.assertEqual(extract_order_by_columns(sql), [("(a + b)", "DESC")])

    def test_function_call_uppercased(self):
        # sqlglot canonicalizes function names to uppercase.
        sql = "select a from t order by lower(a)"
        self.assertEqual(extract_order_by_columns(sql), [("LOWER(a)", "ASC")])

    def test_case_expression(self):
        sql = "select a from t order by case when a > 0 then 1 else 2 end desc"
        self.assertEqual(
            extract_order_by_columns(sql),
            [("CASE WHEN a > 0 THEN 1 ELSE 2 END", "DESC")],
        )

    def test_select_alias_referenced_in_order_by(self):
        sql = "select a as z from t order by z desc"
        self.assertEqual(extract_order_by_columns(sql), [("z", "DESC")])

    def test_quoted_identifier_preserved(self):
        sql = 'select "MyCol" from t order by "MyCol" desc'
        self.assertEqual(extract_order_by_columns(sql), [('"MyCol"', "DESC")])

    def test_nulls_ordering_stripped(self):
        # NULLS FIRST/LAST does not change the ASC/DESC direction we report.
        self.assertEqual(
            extract_order_by_columns("select a from t order by a nulls first"),
            [("a", "ASC")],
        )
        self.assertEqual(
            extract_order_by_columns("select a from t order by a desc nulls last"),
            [("a", "DESC")],
        )

    def test_string_literal_order_key(self):
        sql = "select a from t order by 'x'"
        self.assertEqual(extract_order_by_columns(sql), [("'x'", "ASC")])

    def test_desc_then_asc_same_column(self):
        sql = "select a from t order by a desc, a asc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "DESC"), ("a", "ASC")])


class TestExtractOrderByColumnsStructure(unittest.TestCase):
    """Top-level detection across CTEs, set operations and nesting."""

    def test_cte_body_order_by_is_top_level(self):
        # The ORDER BY on the outer SELECT (not the one inside the CTE) wins.
        sql = "with x as (select a from t order by a) select a from x order by a desc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "DESC")])

    def test_order_by_only_inside_cte_is_ignored(self):
        sql = "with x as (select a from t order by a desc) select a from x"
        self.assertEqual(extract_order_by_columns(sql), [])

    def test_except_top_level_order_by(self):
        sql = "select a from t except select a from s order by a desc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "DESC")])

    def test_intersect_top_level_order_by(self):
        sql = "select a from t intersect select a from s order by 1"
        self.assertEqual(extract_order_by_columns(sql), [("1", "ASC")])

    def test_union_all_top_level_order_by(self):
        sql = "select a from t union all select b from s order by 1 desc"
        self.assertEqual(extract_order_by_columns(sql), [("1", "DESC")])

    def test_order_by_inside_union_subquery_ignored(self):
        sql = (
            "select x from (select a from t union select b from s order by a) sub "
            "order by x desc"
        )
        self.assertEqual(extract_order_by_columns(sql), [("x", "DESC")])

    def test_order_by_with_offset(self):
        sql = "select a from t order by a desc offset 5 rows"
        self.assertEqual(extract_order_by_columns(sql), [("a", "DESC")])

    def test_distinct_on(self):
        sql = "select distinct on (a) a, b from t order by a, b desc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "ASC"), ("b", "DESC")])

    def test_scalar_subquery_alias_in_order_by(self):
        sql = "select (select max(x) from u) as m from t order by m"
        self.assertEqual(extract_order_by_columns(sql), [("m", "ASC")])


class TestExtractOrderByColumnsRobustness(unittest.TestCase):
    """Whitespace, malformed input and non-string input."""

    def test_whitespace_only(self):
        self.assertEqual(extract_order_by_columns("   \n\t "), [])

    def test_semicolon_only(self):
        self.assertEqual(extract_order_by_columns(";"), [])

    def test_irregular_whitespace(self):
        sql = "select a\nfrom t\n\torder\tby\n a  desc"
        self.assertEqual(extract_order_by_columns(sql), [("a", "DESC")])

    def test_comment_between_keywords_returns_empty(self):
        # A comment splitting the ORDER BY keyword is a parse error -> empty list.
        sql = "select a from t order /* c */ by a desc"
        self.assertEqual(extract_order_by_columns(sql), [])


if __name__ == "__main__":
    unittest.main()
