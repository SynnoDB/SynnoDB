#!/usr/bin/env python3
from decimal import Decimal
from pathlib import Path
import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq


OUT = Path(__file__).resolve().parent / "input_parquet"


def money(values):
    return pa.array([Decimal(v) for v in values], type=pa.decimal128(15, 2))


def dates(values):
    return pa.array([dt.date.fromisoformat(v) for v in values], type=pa.date32())


def write(name, columns):
    table = pa.table(columns)
    pq.write_table(table, OUT / f"{name}.parquet")


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    write("customer", {
        "c_custkey": pa.array([1, 2, 3], pa.int64()),
        "c_name": ["Customer#000000001", "Customer#000000002", "Customer#000000003"],
        "c_address": ["1 Main Street", "2 Main Street", "3 Main Street"],
        "c_nationkey": pa.array([1, 2, 3], pa.int32()),
        "c_phone": ["13-123-456-7890", "14-123-456-7890", "15-123-456-7890"],
        "c_acctbal": money(["1000.00", "2000.00", "3000.00"]),
        "c_mktsegment": ["BUILDING", "AUTOMOBILE", "BUILDING"],
        "c_comment": ["sample customer 1", "sample customer 2", "sample customer 3"],
    })

    write("orders", {
        "o_orderkey": pa.array([1, 2, 3], pa.int64()),
        "o_custkey": pa.array([1, 2, 3], pa.int64()),
        "o_orderstatus": ["O", "F", "O"],
        "o_totalprice": money(["100.00", "250.00", "375.00"]),
        "o_orderdate": dates(["1995-03-10", "1995-03-12", "1995-03-15"]),
        "o_orderpriority": ["1-URGENT", "2-HIGH", "3-MEDIUM"],
        "o_clerk": ["Clerk#000000001", "Clerk#000000002", "Clerk#000000003"],
        "o_shippriority": pa.array([0, 0, 0], pa.int32()),
        "o_comment": ["sample order 1", "sample order 2", "sample order 3"],
    })

    write("lineitem", {
        "l_orderkey": pa.array([1, 1, 2, 3], pa.int64()),
        "l_partkey": pa.array([1, 2, 1, 3], pa.int64()),
        "l_suppkey": pa.array([1, 1, 2, 3], pa.int64()),
        "l_linenumber": pa.array([1, 2, 1, 1], pa.int32()),
        "l_quantity": money(["1.00", "2.00", "3.00", "4.00"]),
        "l_extendedprice": money(["10.00", "20.00", "30.00", "40.00"]),
        "l_discount": money(["0.05", "0.00", "0.10", "0.02"]),
        "l_tax": money(["0.01", "0.02", "0.03", "0.04"]),
        "l_returnflag": ["N", "N", "R", "A"],
        "l_linestatus": ["O", "O", "F", "F"],
        "l_shipdate": dates(["1995-03-11", "1995-03-12", "1995-03-13", "1995-03-16"]),
        "l_commitdate": dates(["1995-03-12", "1995-03-13", "1995-03-14", "1995-03-17"]),
        "l_receiptdate": dates(["1995-03-13", "1995-03-14", "1995-03-15", "1995-03-18"]),
        "l_shipinstruct": ["DELIVER IN PERSON", "COLLECT COD", "NONE", "DELIVER IN PERSON"],
        "l_shipmode": ["AIR", "RAIL", "SHIP", "AIR"],
        "l_comment": ["sample lineitem 1", "sample lineitem 2", "sample lineitem 3", "sample lineitem 4"],
    })

    write("part", {
        "p_partkey": pa.array([1, 2, 3], pa.int64()),
        "p_name": ["goldenrod part", "forest part", "blue part"],
        "p_mfgr": ["Manufacturer#1", "Manufacturer#2", "Manufacturer#3"],
        "p_brand": ["Brand#11", "Brand#23", "Brand#34"],
        "p_type": ["PROMO BURNISHED COPPER", "STANDARD POLISHED TIN", "ECONOMY ANODIZED STEEL"],
        "p_size": pa.array([1, 2, 3], pa.int32()),
        "p_container": ["SM BOX", "MED BAG", "LG CASE"],
        "p_retailprice": money(["901.00", "902.00", "903.00"]),
        "p_comment": ["sample part 1", "sample part 2", "sample part 3"],
    })

    write("supplier", {
        "s_suppkey": pa.array([1, 2, 3], pa.int64()),
        "s_name": ["Supplier#000000001", "Supplier#000000002", "Supplier#000000003"],
        "s_address": ["1 Supplier Street", "2 Supplier Street", "3 Supplier Street"],
        "s_nationkey": pa.array([1, 2, 3], pa.int32()),
        "s_phone": ["13-123-456-7890", "14-123-456-7890", "15-123-456-7890"],
        "s_acctbal": money(["100.00", "200.00", "300.00"]),
        "s_comment": ["sample supplier 1", "sample supplier 2", "sample supplier 3"],
    })


if __name__ == "__main__":
    main()
