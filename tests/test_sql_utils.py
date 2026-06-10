import unittest

from utils.sql_utils import extract_order_by_columns


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
        sql = """    n_name,  
    c_address,  
    c_comment 
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


if __name__ == "__main__":
    unittest.main()
