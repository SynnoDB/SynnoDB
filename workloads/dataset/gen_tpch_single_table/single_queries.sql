-- [1/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-02-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-02-01' 
    and o_orderdate < date '1993-02-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [2/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [3/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#21', 'CONTAINER': 'LG PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#21'
    and p_container = 'LG PACK'
order by
    p_retailprice desc;

-- [4/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-02-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-02-01' 
    and o_orderdate < date '1996-02-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [5/200] Generated STQ7
-- Placeholders used: {'COLOR': 'beige', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%beige%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [6/200] Generated STQ2
-- Placeholders used: {'DATE': '1994-02-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1994-02-01' 
    and o_orderdate < date '1994-02-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [7/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#55', 'CONTAINER': 'SM CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#55'
    and p_container = 'SM CASE'
order by
    p_retailprice desc;

-- [8/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#54', 'CONTAINER': 'SM PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#54'
    and p_container = 'SM PKG'
order by
    p_retailprice desc;

-- [9/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '23', 'I3': '13'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','23','13')
order by
    c_acctbal desc;

-- [10/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'HOUSEHOLD' 
order by 
    c_acctbal desc;

-- [11/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'RAIL', 'SHIPMODE2': 'AIR REG', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('RAIL', 'AIR REG')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [12/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR', 'SHIPMODE2': 'FOB', 'DATE': '1996-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR', 'FOB')
    and l_shipdate >= date '1996-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [13/200] Generated STQ2
-- Placeholders used: {'DATE': '1994-11-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1994-11-01' 
    and o_orderdate < date '1994-11-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [14/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'RAIL', 'DATE': '1993-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('TRUCK', 'RAIL')
    and l_shipdate >= date '1993-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [15/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '13', 'I3': '29'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','13','29')
order by
    c_acctbal desc;

-- [16/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-12-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-12-01' 
    and o_orderdate < date '1995-12-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [17/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [18/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#11', 'CONTAINER': 'LG PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#11'
    and p_container = 'LG PACK'
order by
    p_retailprice desc;

-- [19/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#31', 'CONTAINER': 'SM PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#31'
    and p_container = 'SM PKG'
order by
    p_retailprice desc;

-- [20/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-01-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [21/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [22/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR REG', 'SHIPMODE2': 'RAIL', 'DATE': '1995-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR REG', 'RAIL')
    and l_shipdate >= date '1995-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [23/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#31', 'CONTAINER': 'LG BOX'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#31'
    and p_container = 'LG BOX'
order by
    p_retailprice desc;

-- [24/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [25/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#24', 'CONTAINER': 'MED PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#24'
    and p_container = 'MED PACK'
order by
    p_retailprice desc;

-- [26/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [27/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'FOB', 'SHIPMODE2': 'AIR', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('FOB', 'AIR')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [28/200] Generated STQ1
-- Placeholders used: {'DELTA': '111'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '111' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [29/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'SHIP', 'SHIPMODE2': 'RAIL', 'DATE': '1993-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('SHIP', 'RAIL')
    and l_shipdate >= date '1993-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [30/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#53', 'CONTAINER': 'SM PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#53'
    and p_container = 'SM PKG'
order by
    p_retailprice desc;

-- [31/200] Generated STQ8
-- Placeholders used: {'I1': '29', 'I2': '18', 'I3': '17'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('29','18','17')
order by
    c_acctbal desc;

-- [32/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'FURNITURE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'FURNITURE' 
order by 
    c_acctbal desc;

-- [33/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'BUILDING'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'BUILDING' 
order by 
    c_acctbal desc;

-- [34/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [35/200] Generated STQ7
-- Placeholders used: {'COLOR': 'linen', 'TYPE': 'NICKEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%linen%'
    and p_type like '%NICKEL'
order by
    p_partkey;

-- [36/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [37/200] Generated STQ8
-- Placeholders used: {'I1': '13', 'I2': '17', 'I3': '18'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('13','17','18')
order by
    c_acctbal desc;

-- [38/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'BUILDING'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'BUILDING' 
order by 
    c_acctbal desc;

-- [39/200] Generated STQ7
-- Placeholders used: {'COLOR': 'seashell', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%seashell%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [40/200] Generated STQ7
-- Placeholders used: {'COLOR': 'maroon', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%maroon%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [41/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '23', 'I3': '17'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','23','17')
order by
    c_acctbal desc;

-- [42/200] Generated STQ1
-- Placeholders used: {'DELTA': '103'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '103' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [43/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-08-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-08-01' 
    and o_orderdate < date '1996-08-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [44/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [45/200] Generated STQ2
-- Placeholders used: {'DATE': '1994-07-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1994-07-01' 
    and o_orderdate < date '1994-07-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [46/200] Generated STQ7
-- Placeholders used: {'COLOR': 'cyan', 'TYPE': 'STEEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%cyan%'
    and p_type like '%STEEL'
order by
    p_partkey;

-- [47/200] Generated STQ1
-- Placeholders used: {'DELTA': '106'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '106' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [48/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [49/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-08-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-08-01' 
    and o_orderdate < date '1997-08-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [50/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [51/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'FURNITURE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'FURNITURE' 
order by 
    c_acctbal desc;

-- [52/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [53/200] Generated STQ1
-- Placeholders used: {'DELTA': '98'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '98' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [54/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'SHIP', 'SHIPMODE2': 'AIR', 'DATE': '1993-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('SHIP', 'AIR')
    and l_shipdate >= date '1993-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [55/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'FOB', 'SHIPMODE2': 'RAIL', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('FOB', 'RAIL')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [56/200] Generated STQ1
-- Placeholders used: {'DELTA': '75'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '75' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [57/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-06-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-06-01' 
    and o_orderdate < date '1993-06-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [58/200] Generated STQ8
-- Placeholders used: {'I1': '17', 'I2': '13', 'I3': '30'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('17','13','30')
order by
    c_acctbal desc;

-- [59/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'BUILDING'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'BUILDING' 
order by 
    c_acctbal desc;

-- [60/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '31', 'I3': '23'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','31','23')
order by
    c_acctbal desc;

-- [61/200] Generated STQ7
-- Placeholders used: {'COLOR': 'floral', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%floral%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [62/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#34', 'CONTAINER': 'LG PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#34'
    and p_container = 'LG PACK'
order by
    p_retailprice desc;

-- [63/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'SHIP', 'SHIPMODE2': 'TRUCK', 'DATE': '1996-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('SHIP', 'TRUCK')
    and l_shipdate >= date '1996-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [64/200] Generated STQ2
-- Placeholders used: {'DATE': '1994-04-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1994-04-01' 
    and o_orderdate < date '1994-04-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [65/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#13', 'CONTAINER': 'SM CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#13'
    and p_container = 'SM CASE'
order by
    p_retailprice desc;

-- [66/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#52', 'CONTAINER': 'SM CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#52'
    and p_container = 'SM CASE'
order by
    p_retailprice desc;

-- [67/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-10-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-10-01' 
    and o_orderdate < date '1996-10-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [68/200] Generated STQ1
-- Placeholders used: {'DELTA': '74'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '74' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [69/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-10-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-10-01' 
    and o_orderdate < date '1997-10-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [70/200] Generated STQ1
-- Placeholders used: {'DELTA': '115'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '115' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [71/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR', 'SHIPMODE2': 'TRUCK', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR', 'TRUCK')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [72/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [73/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#52', 'CONTAINER': 'LG PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#52'
    and p_container = 'LG PKG'
order by
    p_retailprice desc;

-- [74/200] Generated STQ8
-- Placeholders used: {'I1': '31', 'I2': '29', 'I3': '18'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('31','29','18')
order by
    c_acctbal desc;

-- [75/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#11', 'CONTAINER': 'LG PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#11'
    and p_container = 'LG PACK'
order by
    p_retailprice desc;

-- [76/200] Generated STQ7
-- Placeholders used: {'COLOR': 'lime', 'TYPE': 'STEEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%lime%'
    and p_type like '%STEEL'
order by
    p_partkey;

-- [77/200] Generated STQ7
-- Placeholders used: {'COLOR': 'orchid', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%orchid%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [78/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-04-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-04-01' 
    and o_orderdate < date '1993-04-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [79/200] Generated STQ7
-- Placeholders used: {'COLOR': 'lemon', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%lemon%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [80/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#22', 'CONTAINER': 'LG CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#22'
    and p_container = 'LG CASE'
order by
    p_retailprice desc;

-- [81/200] Generated STQ8
-- Placeholders used: {'I1': '31', 'I2': '29', 'I3': '17'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('31','29','17')
order by
    c_acctbal desc;

-- [82/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [83/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#14', 'CONTAINER': 'LG CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#14'
    and p_container = 'LG CASE'
order by
    p_retailprice desc;

-- [84/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-04-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-04-01' 
    and o_orderdate < date '1993-04-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [85/200] Generated STQ1
-- Placeholders used: {'DELTA': '65'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '65' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [86/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#24', 'CONTAINER': 'MED PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#24'
    and p_container = 'MED PKG'
order by
    p_retailprice desc;

-- [87/200] Generated STQ8
-- Placeholders used: {'I1': '31', 'I2': '29', 'I3': '13'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('31','29','13')
order by
    c_acctbal desc;

-- [88/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'HOUSEHOLD' 
order by 
    c_acctbal desc;

-- [89/200] Generated STQ1
-- Placeholders used: {'DELTA': '84'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '84' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [90/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [91/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [92/200] Generated STQ8
-- Placeholders used: {'I1': '31', 'I2': '17', 'I3': '23'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('31','17','23')
order by
    c_acctbal desc;

-- [93/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#15', 'CONTAINER': 'LG PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#15'
    and p_container = 'LG PKG'
order by
    p_retailprice desc;

-- [94/200] Generated STQ1
-- Placeholders used: {'DELTA': '107'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '107' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [95/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR', 'SHIPMODE2': 'FOB', 'DATE': '1997-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR', 'FOB')
    and l_shipdate >= date '1997-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [96/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '17', 'I3': '31'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','17','31')
order by
    c_acctbal desc;

-- [97/200] Generated STQ1
-- Placeholders used: {'DELTA': '92'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '92' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [98/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-07-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-07-01' 
    and o_orderdate < date '1997-07-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [99/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'AUTOMOBILE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'AUTOMOBILE' 
order by 
    c_acctbal desc;

-- [100/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-08-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-08-01' 
    and o_orderdate < date '1996-08-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [101/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#41', 'CONTAINER': 'LG BOX'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#41'
    and p_container = 'LG BOX'
order by
    p_retailprice desc;

-- [102/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#55', 'CONTAINER': 'SM CASE'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#55'
    and p_container = 'SM CASE'
order by
    p_retailprice desc;

-- [103/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-03-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-03-01' 
    and o_orderdate < date '1995-03-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [104/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'RAIL', 'SHIPMODE2': 'AIR REG', 'DATE': '1995-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('RAIL', 'AIR REG')
    and l_shipdate >= date '1995-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [105/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#34', 'CONTAINER': 'SM PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#34'
    and p_container = 'SM PACK'
order by
    p_retailprice desc;

-- [106/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [107/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'FOB', 'SHIPMODE2': 'AIR', 'DATE': '1993-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('FOB', 'AIR')
    and l_shipdate >= date '1993-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [108/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '17', 'I3': '13'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','17','13')
order by
    c_acctbal desc;

-- [109/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-11-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-11-01' 
    and o_orderdate < date '1995-11-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [110/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#53', 'CONTAINER': 'SM PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#53'
    and p_container = 'SM PACK'
order by
    p_retailprice desc;

-- [111/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR', 'SHIPMODE2': 'AIR REG', 'DATE': '1995-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR', 'AIR REG')
    and l_shipdate >= date '1995-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [112/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [113/200] Generated STQ8
-- Placeholders used: {'I1': '17', 'I2': '30', 'I3': '23'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('17','30','23')
order by
    c_acctbal desc;

-- [114/200] Generated STQ1
-- Placeholders used: {'DELTA': '102'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '102' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [115/200] Generated STQ5
-- Placeholders used: {'WORD1': 'special'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%special%'
order by
    s_acctbal desc;

-- [116/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'FURNITURE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'FURNITURE' 
order by 
    c_acctbal desc;

-- [117/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-09-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-09-01' 
    and o_orderdate < date '1997-09-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [118/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-12-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-12-01' 
    and o_orderdate < date '1996-12-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [119/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'FURNITURE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'FURNITURE' 
order by 
    c_acctbal desc;

-- [120/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [121/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR REG', 'SHIPMODE2': 'MAIL', 'DATE': '1995-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR REG', 'MAIL')
    and l_shipdate >= date '1995-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [122/200] Generated STQ8
-- Placeholders used: {'I1': '23', 'I2': '13', 'I3': '18'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('23','13','18')
order by
    c_acctbal desc;

-- [123/200] Generated STQ7
-- Placeholders used: {'COLOR': 'honeydew', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%honeydew%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [124/200] Generated STQ1
-- Placeholders used: {'DELTA': '81'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '81' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [125/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'FURNITURE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'FURNITURE' 
order by 
    c_acctbal desc;

-- [126/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'HOUSEHOLD' 
order by 
    c_acctbal desc;

-- [127/200] Generated STQ7
-- Placeholders used: {'COLOR': 'rosy', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%rosy%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [128/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-05-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-05-01' 
    and o_orderdate < date '1993-05-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [129/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [130/200] Generated STQ1
-- Placeholders used: {'DELTA': '113'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '113' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [131/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'FOB', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('TRUCK', 'FOB')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [132/200] Generated STQ7
-- Placeholders used: {'COLOR': 'coral', 'TYPE': 'TIN'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%coral%'
    and p_type like '%TIN'
order by
    p_partkey;

-- [133/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [134/200] Generated STQ1
-- Placeholders used: {'DELTA': '117'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '117' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [135/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR REG', 'SHIPMODE2': 'MAIL', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR REG', 'MAIL')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [136/200] Generated STQ2
-- Placeholders used: {'DATE': '1994-11-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1994-11-01' 
    and o_orderdate < date '1994-11-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [137/200] Generated STQ7
-- Placeholders used: {'COLOR': 'slate', 'TYPE': 'NICKEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%slate%'
    and p_type like '%NICKEL'
order by
    p_partkey;

-- [138/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#22', 'CONTAINER': 'MED PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#22'
    and p_container = 'MED PACK'
order by
    p_retailprice desc;

-- [139/200] Generated STQ1
-- Placeholders used: {'DELTA': '71'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '71' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [140/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'FOB', 'SHIPMODE2': 'SHIP', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('FOB', 'SHIP')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [141/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [142/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-01-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [143/200] Generated STQ1
-- Placeholders used: {'DELTA': '114'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '114' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [144/200] Generated STQ8
-- Placeholders used: {'I1': '31', 'I2': '17', 'I3': '29'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('31','17','29')
order by
    c_acctbal desc;

-- [145/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'RAIL', 'SHIPMODE2': 'AIR REG', 'DATE': '1994-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('RAIL', 'AIR REG')
    and l_shipdate >= date '1994-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [146/200] Generated STQ1
-- Placeholders used: {'DELTA': '102'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '102' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [147/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#43', 'CONTAINER': 'MED BAG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#43'
    and p_container = 'MED BAG'
order by
    p_retailprice desc;

-- [148/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-02-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-02-01' 
    and o_orderdate < date '1997-02-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [149/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [150/200] Generated STQ7
-- Placeholders used: {'COLOR': 'tomato', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%tomato%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [151/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'AIR', 'SHIPMODE2': 'FOB', 'DATE': '1995-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('AIR', 'FOB')
    and l_shipdate >= date '1995-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [152/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [153/200] Generated STQ5
-- Placeholders used: {'WORD1': 'special'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%special%'
order by
    s_acctbal desc;

-- [154/200] Generated STQ2
-- Placeholders used: {'DATE': '1996-03-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-03-01' 
    and o_orderdate < date '1996-03-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [155/200] Generated STQ7
-- Placeholders used: {'COLOR': 'light', 'TYPE': 'BRASS'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%light%'
    and p_type like '%BRASS'
order by
    p_partkey;

-- [156/200] Generated STQ7
-- Placeholders used: {'COLOR': 'sienna', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%sienna%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [157/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-01-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [158/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#31', 'CONTAINER': 'LG PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#31'
    and p_container = 'LG PKG'
order by
    p_retailprice desc;

-- [159/200] Generated STQ7
-- Placeholders used: {'COLOR': 'almond', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%almond%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [160/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#34', 'CONTAINER': 'SM BOX'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#34'
    and p_container = 'SM BOX'
order by
    p_retailprice desc;

-- [161/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'RAIL', 'DATE': '1993-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('TRUCK', 'RAIL')
    and l_shipdate >= date '1993-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [162/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [163/200] Generated STQ7
-- Placeholders used: {'COLOR': 'lavender', 'TYPE': 'STEEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%lavender%'
    and p_type like '%STEEL'
order by
    p_partkey;

-- [164/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [165/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#44', 'CONTAINER': 'LG PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#44'
    and p_container = 'LG PACK'
order by
    p_retailprice desc;

-- [166/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'MACHINERY'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'MACHINERY' 
order by 
    c_acctbal desc;

-- [167/200] Generated STQ5
-- Placeholders used: {'WORD1': 'express'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%express%'
order by
    s_acctbal desc;

-- [168/200] Generated STQ1
-- Placeholders used: {'DELTA': '79'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '79' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [169/200] Generated STQ5
-- Placeholders used: {'WORD1': 'pending'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%pending%'
order by
    s_acctbal desc;

-- [170/200] Generated STQ7
-- Placeholders used: {'COLOR': 'salmon', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%salmon%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [171/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'SHIP', 'SHIPMODE2': 'FOB', 'DATE': '1996-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('SHIP', 'FOB')
    and l_shipdate >= date '1996-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [172/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#54', 'CONTAINER': 'LG PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#54'
    and p_container = 'LG PKG'
order by
    p_retailprice desc;

-- [173/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'AUTOMOBILE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'AUTOMOBILE' 
order by 
    c_acctbal desc;

-- [174/200] Generated STQ5
-- Placeholders used: {'WORD1': 'unusual'}
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%unusual%'
order by
    s_acctbal desc;

-- [175/200] Generated STQ2
-- Placeholders used: {'DATE': '1997-05-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-05-01' 
    and o_orderdate < date '1997-05-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [176/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#32', 'CONTAINER': 'SM PKG'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#32'
    and p_container = 'SM PKG'
order by
    p_retailprice desc;

-- [177/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'AUTOMOBILE'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'AUTOMOBILE' 
order by 
    c_acctbal desc;

-- [178/200] Generated STQ1
-- Placeholders used: {'DELTA': '75'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '75' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [179/200] Generated STQ8
-- Placeholders used: {'I1': '30', 'I2': '13', 'I3': '29'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('30','13','29')
order by
    c_acctbal desc;

-- [180/200] Generated STQ7
-- Placeholders used: {'COLOR': 'smoke', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%smoke%'
    and p_type like '%COPPER'
order by
    p_partkey;

-- [181/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#44', 'CONTAINER': 'MED PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#44'
    and p_container = 'MED PACK'
order by
    p_retailprice desc;

-- [182/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#21', 'CONTAINER': 'SM BOX'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#21'
    and p_container = 'SM BOX'
order by
    p_retailprice desc;

-- [183/200] Generated STQ7
-- Placeholders used: {'COLOR': 'forest', 'TYPE': 'NICKEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%forest%'
    and p_type like '%NICKEL'
order by
    p_partkey;

-- [184/200] Generated STQ8
-- Placeholders used: {'I1': '13', 'I2': '30', 'I3': '31'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('13','30','31')
order by
    c_acctbal desc;

-- [185/200] Generated STQ2
-- Placeholders used: {'DATE': '1995-06-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-06-01' 
    and o_orderdate < date '1995-06-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [186/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'HOUSEHOLD' 
order by 
    c_acctbal desc;

-- [187/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'FOB', 'SHIPMODE2': 'SHIP', 'DATE': '1997-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('FOB', 'SHIP')
    and l_shipdate >= date '1997-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [188/200] Generated STQ7
-- Placeholders used: {'COLOR': 'rose', 'TYPE': 'STEEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%rose%'
    and p_type like '%STEEL'
order by
    p_partkey;

-- [189/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'HOUSEHOLD' 
order by 
    c_acctbal desc;

-- [190/200] Generated STQ8
-- Placeholders used: {'I1': '23', 'I2': '31', 'I3': '17'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('23','31','17')
order by
    c_acctbal desc;

-- [191/200] Generated STQ8
-- Placeholders used: {'I1': '18', 'I2': '31', 'I3': '23'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('18','31','23')
order by
    c_acctbal desc;

-- [192/200] Generated STQ8
-- Placeholders used: {'I1': '13', 'I2': '18', 'I3': '23'}
select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('13','18','23')
order by
    c_acctbal desc;

-- [193/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#33', 'CONTAINER': 'MED BOX'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#33'
    and p_container = 'MED BOX'
order by
    p_retailprice desc;

-- [194/200] Generated STQ2
-- Placeholders used: {'DATE': '1993-09-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-09-01' 
    and o_orderdate < date '1993-09-01' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [195/200] Generated STQ3
-- Placeholders used: {'SEGMENT': 'BUILDING'}
select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = 'BUILDING' 
order by 
    c_acctbal desc;

-- [196/200] Generated STQ7
-- Placeholders used: {'COLOR': 'violet', 'TYPE': 'NICKEL'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%violet%'
    and p_type like '%NICKEL'
order by
    p_partkey;

-- [197/200] Generated STQ4
-- Placeholders used: {'BRAND': 'Brand#14', 'CONTAINER': 'MED PACK'}
select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = 'Brand#14'
    and p_container = 'MED PACK'
order by
    p_retailprice desc;

-- [198/200] Generated STQ6
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'SHIP', 'DATE': '1996-01-01'}
select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('TRUCK', 'SHIP')
    and l_shipdate >= date '1996-01-01'
group by
    l_shipmode
order by
    l_shipmode;

-- [199/200] Generated STQ1
-- Placeholders used: {'DELTA': '73'}
select 
    l_returnflag,  
    l_linestatus,  
    sum(l_quantity) as sum_qty, 
    sum(l_extendedprice) as sum_base_price, 
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price, 
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, 
    avg(l_quantity) as avg_qty,  
    avg(l_extendedprice) as avg_price, 
    avg(l_discount) as avg_disc,  
    count(*) as count_order 
from  
    lineitem 
where  
    l_shipdate <= date '1998-12-01' - interval '73' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [200/200] Generated STQ7
-- Placeholders used: {'COLOR': 'medium', 'TYPE': 'COPPER'}
select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%medium%'
    and p_type like '%COPPER'
order by
    p_partkey;

