-- [1/200] Generated Q21
-- Placeholders used: {'NATION': 'CANADA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'CANADA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [2/200] Generated Q1
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

-- [3/200] Generated Q9
-- Placeholders used: {'COLOR': 'ghost'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%ghost%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [4/200] Generated Q8
-- Placeholders used: {'NATION': 'EGYPT', 'REGION': 'AFRICA', 'TYPE': 'PROMO BRUSHED TIN'}
select 
    o_year,  
    sum(case  
        when nation = 'EGYPT'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'AFRICA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'PROMO BRUSHED TIN'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [5/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '7', 'QUANTITY2': '10', 'QUANTITY3': '20', 'BRAND1': 'Brand#12', 'BRAND2': 'Brand#25', 'BRAND3': 'Brand#51'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#12'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 7 and l_quantity <= 7 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#25'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 10 and l_quantity <= 10 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#51'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 20 and l_quantity <= 20 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [6/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [7/200] Generated Q21
-- Placeholders used: {'NATION': 'RUSSIA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'RUSSIA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [8/200] Generated Q18
-- Placeholders used: {'QUANTITY': '315'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 315
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [9/200] Generated Q8
-- Placeholders used: {'NATION': 'KENYA', 'REGION': 'MIDDLE EAST', 'TYPE': 'MEDIUM ANODIZED NICKEL'}
select 
    o_year,  
    sum(case  
        when nation = 'KENYA'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'MIDDLE EAST' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'MEDIUM ANODIZED NICKEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [10/200] Generated Q14
-- Placeholders used: {'DATE': '1994-10-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1994-10-01' 
    and l_shipdate < date '1994-10-01' + interval '1' month;

-- [11/200] Generated Q9
-- Placeholders used: {'COLOR': 'cream'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%cream%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [12/200] Generated Q7
-- Placeholders used: {'NATION1': 'UNITED STATES', 'NATION2': 'IRAN'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'UNITED STATES' and n2.n_name = 'IRAN') 
            or (n1.n_name = 'IRAN' and n2.n_name = 'UNITED STATES') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [13/200] Generated Q4
-- Placeholders used: {'DATE': '1993-06-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-06-01' 
    and o_orderdate < date '1993-06-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [14/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'accounts'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%accounts%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [15/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'RAIL', 'DATE': '1993-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('TRUCK', 'RAIL') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1993-01-01' 
    and l_receiptdate < date '1993-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [16/200] Generated Q15
-- Placeholders used: {'DATE': '1995-11-01', 'STREAM_ID': '2'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1995-11-01'
        and l_shipdate < date '1995-11-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [17/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'accounts'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%accounts%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [18/200] Generated Q21
-- Placeholders used: {'NATION': 'ROMANIA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'ROMANIA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [19/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'AIR REG', 'DATE': '1993-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('TRUCK', 'AIR REG') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1993-01-01' 
    and l_receiptdate < date '1993-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [20/200] Generated Q2
-- Placeholders used: {'SIZE': '43', 'TYPE': 'NICKEL', 'REGION': 'ASIA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 43
    and p_type like '%NICKEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'ASIA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'ASIA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [21/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-28'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-28' 
    and l_shipdate > date '1995-03-28' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [22/200] Generated Q4
-- Placeholders used: {'DATE': '1995-01-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [23/200] Generated Q9
-- Placeholders used: {'COLOR': 'orange'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%orange%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [24/200] Generated Q21
-- Placeholders used: {'NATION': 'IRAQ'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'IRAQ'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [25/200] Generated Q6
-- Placeholders used: {'DATE': '1995-01-01', 'DISCOUNT': '0.07', 'QUANTITY': '24'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1995-01-01' 
    and l_shipdate < date '1995-01-01' + interval '1' year 
    and l_discount between 0.07 - 0.01 and 0.07 + 0.01 
    and l_quantity < 24;

-- [26/200] Generated Q22
-- Placeholders used: {'I1': '23', 'I2': '18', 'I3': '13', 'I4': '31', 'I5': '17', 'I6': '30', 'I7': '29'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('23','18','13','31','17','30','29')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('23','18','13','31','17','30','29')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [27/200] Generated Q15
-- Placeholders used: {'DATE': '1995-01-01', 'STREAM_ID': '5'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1995-01-01'
        and l_shipdate < date '1995-01-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [28/200] Generated Q21
-- Placeholders used: {'NATION': 'RUSSIA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'RUSSIA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [29/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [30/200] Generated Q22
-- Placeholders used: {'I1': '23', 'I2': '13', 'I3': '31', 'I4': '18', 'I5': '30', 'I6': '17', 'I7': '29'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('23','13','31','18','30','17','29')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('23','13','31','18','30','17','29')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [31/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-30'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-30' 
    and l_shipdate > date '1995-03-30' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [32/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '6', 'QUANTITY2': '13', 'QUANTITY3': '30', 'BRAND1': 'Brand#44', 'BRAND2': 'Brand#42', 'BRAND3': 'Brand#32'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#44'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 6 and l_quantity <= 6 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#42'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 13 and l_quantity <= 13 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#32'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 30 and l_quantity <= 30 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [33/200] Generated Q8
-- Placeholders used: {'NATION': 'UNITED KINGDOM', 'REGION': 'MIDDLE EAST', 'TYPE': 'ECONOMY PLATED COPPER'}
select 
    o_year,  
    sum(case  
        when nation = 'UNITED KINGDOM'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'MIDDLE EAST' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'ECONOMY PLATED COPPER'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [34/200] Generated Q14
-- Placeholders used: {'DATE': '1997-10-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1997-10-01' 
    and l_shipdate < date '1997-10-01' + interval '1' month;

-- [35/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '7', 'QUANTITY2': '15', 'QUANTITY3': '23', 'BRAND1': 'Brand#25', 'BRAND2': 'Brand#41', 'BRAND3': 'Brand#11'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#25'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 7 and l_quantity <= 7 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#41'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 15 and l_quantity <= 15 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#11'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 23 and l_quantity <= 23 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [36/200] Generated Q5
-- Placeholders used: {'REGION': 'AMERICA', 'DATE': '1996-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'AMERICA' 
    and o_orderdate >= date '1996-01-01' 
    and o_orderdate < date '1996-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [37/200] Generated Q20
-- Placeholders used: {'COLOR': 'blue', 'DATE': '1996-01-01', 'NATION': 'JAPAN'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'blue%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1996-01-01')
                and l_shipdate < date('1996-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'JAPAN'
order by
    s_name;

-- [38/200] Generated Q20
-- Placeholders used: {'COLOR': 'orchid', 'DATE': '1997-01-01', 'NATION': 'INDIA'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'orchid%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1997-01-01')
                and l_shipdate < date('1997-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'INDIA'
order by
    s_name;

-- [39/200] Generated Q18
-- Placeholders used: {'QUANTITY': '312'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 312
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [40/200] Generated Q22
-- Placeholders used: {'I1': '18', 'I2': '13', 'I3': '30', 'I4': '23', 'I5': '29', 'I6': '31', 'I7': '17'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('18','13','30','23','29','31','17')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('18','13','30','23','29','31','17')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [41/200] Generated Q10
-- Placeholders used: {'DATE': '1994-03-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-03-01' 
    and o_orderdate < date '1994-03-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [42/200] Generated Q6
-- Placeholders used: {'DATE': '1996-01-01', 'DISCOUNT': '0.02', 'QUANTITY': '25'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1996-01-01' 
    and l_shipdate < date '1996-01-01' + interval '1' year 
    and l_discount between 0.02 - 0.01 and 0.02 + 0.01 
    and l_quantity < 25;

-- [43/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#25', 'CONTAINER': 'SM BOX'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#25'
    and p_container = 'SM BOX'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [44/200] Generated Q21
-- Placeholders used: {'NATION': 'INDONESIA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'INDONESIA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [45/200] Generated Q21
-- Placeholders used: {'NATION': 'MOZAMBIQUE'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'MOZAMBIQUE'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [46/200] Generated Q20
-- Placeholders used: {'COLOR': 'drab', 'DATE': '1994-01-01', 'NATION': 'IRAQ'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'drab%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1994-01-01')
                and l_shipdate < date('1994-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'IRAQ'
order by
    s_name;

-- [47/200] Generated Q6
-- Placeholders used: {'DATE': '1997-01-01', 'DISCOUNT': '0.02', 'QUANTITY': '25'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1997-01-01' 
    and l_shipdate < date '1997-01-01' + interval '1' year 
    and l_discount between 0.02 - 0.01 and 0.02 + 0.01 
    and l_quantity < 25;

-- [48/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#11', 'TYPE': 'MEDIUM PLATED', 'SIZE1': '16', 'SIZE2': '4', 'SIZE3': '50', 'SIZE4': '37', 'SIZE5': '6', 'SIZE6': '46', 'SIZE7': '32', 'SIZE8': '5'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#11' 
    and p_type not like 'MEDIUM PLATED%' 
    and p_size in (16, 4, 50, 37, 6, 46, 32, 5) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [49/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [50/200] Generated Q5
-- Placeholders used: {'REGION': 'EUROPE', 'DATE': '1997-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'EUROPE' 
    and o_orderdate >= date '1997-01-01' 
    and o_orderdate < date '1997-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [51/200] Generated Q6
-- Placeholders used: {'DATE': '1995-01-01', 'DISCOUNT': '0.08', 'QUANTITY': '24'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1995-01-01' 
    and l_shipdate < date '1995-01-01' + interval '1' year 
    and l_discount between 0.08 - 0.01 and 0.08 + 0.01 
    and l_quantity < 24;

-- [52/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [53/200] Generated Q10
-- Placeholders used: {'DATE': '1994-02-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-02-01' 
    and o_orderdate < date '1994-02-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [54/200] Generated Q22
-- Placeholders used: {'I1': '18', 'I2': '23', 'I3': '29', 'I4': '30', 'I5': '13', 'I6': '17', 'I7': '31'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('18','23','29','30','13','17','31')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('18','23','29','30','13','17','31')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [55/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'FURNITURE', 'DATE': '1995-03-01'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'FURNITURE' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-01' 
    and l_shipdate > date '1995-03-01' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [56/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '9', 'QUANTITY2': '13', 'QUANTITY3': '29', 'BRAND1': 'Brand#21', 'BRAND2': 'Brand#11', 'BRAND3': 'Brand#21'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#21'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 9 and l_quantity <= 9 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#11'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 13 and l_quantity <= 13 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#21'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 29 and l_quantity <= 29 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [57/200] Generated Q2
-- Placeholders used: {'SIZE': '22', 'TYPE': 'TIN', 'REGION': 'MIDDLE EAST'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 22
    and p_type like '%TIN'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'MIDDLE EAST'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'MIDDLE EAST'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [58/200] Generated Q8
-- Placeholders used: {'NATION': 'INDIA', 'REGION': 'EUROPE', 'TYPE': 'SMALL BRUSHED NICKEL'}
select 
    o_year,  
    sum(case  
        when nation = 'INDIA'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'EUROPE' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'SMALL BRUSHED NICKEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [59/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '10', 'QUANTITY2': '17', 'QUANTITY3': '23', 'BRAND1': 'Brand#44', 'BRAND2': 'Brand#21', 'BRAND3': 'Brand#14'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#44'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 10 and l_quantity <= 10 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#21'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 17 and l_quantity <= 17 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#14'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 23 and l_quantity <= 23 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [60/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'SHIP', 'SHIPMODE2': 'FOB', 'DATE': '1996-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('SHIP', 'FOB') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1996-01-01' 
    and l_receiptdate < date '1996-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [61/200] Generated Q2
-- Placeholders used: {'SIZE': '44', 'TYPE': 'TIN', 'REGION': 'AFRICA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 44
    and p_type like '%TIN'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'AFRICA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'AFRICA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [62/200] Generated Q13
-- Placeholders used: {'WORD1': 'unusual', 'WORD2': 'packages'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%unusual%packages%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [63/200] Generated Q8
-- Placeholders used: {'NATION': 'FRANCE', 'REGION': 'AMERICA', 'TYPE': 'ECONOMY POLISHED NICKEL'}
select 
    o_year,  
    sum(case  
        when nation = 'FRANCE'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'AMERICA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'ECONOMY POLISHED NICKEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [64/200] Generated Q14
-- Placeholders used: {'DATE': '1993-12-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1993-12-01' 
    and l_shipdate < date '1993-12-01' + interval '1' month;

-- [65/200] Generated Q9
-- Placeholders used: {'COLOR': 'orchid'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%orchid%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [66/200] Generated Q8
-- Placeholders used: {'NATION': 'BRAZIL', 'REGION': 'EUROPE', 'TYPE': 'ECONOMY ANODIZED TIN'}
select 
    o_year,  
    sum(case  
        when nation = 'BRAZIL'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'EUROPE' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'ECONOMY ANODIZED TIN'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [67/200] Generated Q21
-- Placeholders used: {'NATION': 'PERU'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'PERU'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [68/200] Generated Q1
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

-- [69/200] Generated Q8
-- Placeholders used: {'NATION': 'ETHIOPIA', 'REGION': 'EUROPE', 'TYPE': 'LARGE POLISHED NICKEL'}
select 
    o_year,  
    sum(case  
        when nation = 'ETHIOPIA'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'EUROPE' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'LARGE POLISHED NICKEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [70/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'requests'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%requests%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [71/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'deposits'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%deposits%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [72/200] Generated Q9
-- Placeholders used: {'COLOR': 'orange'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%orange%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [73/200] Generated Q10
-- Placeholders used: {'DATE': '1994-03-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-03-01' 
    and o_orderdate < date '1994-03-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [74/200] Generated Q18
-- Placeholders used: {'QUANTITY': '315'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 315
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [75/200] Generated Q5
-- Placeholders used: {'REGION': 'AMERICA', 'DATE': '1995-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'AMERICA' 
    and o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [76/200] Generated Q7
-- Placeholders used: {'NATION1': 'ARGENTINA', 'NATION2': 'CHINA'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'ARGENTINA' and n2.n_name = 'CHINA') 
            or (n1.n_name = 'CHINA' and n2.n_name = 'ARGENTINA') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [77/200] Generated Q18
-- Placeholders used: {'QUANTITY': '312'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 312
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [78/200] Generated Q11
-- Placeholders used: {'NATION': 'ARGENTINA', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'ARGENTINA' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'ARGENTINA'
        ) 
order by 
    value desc;

-- [79/200] Generated Q2
-- Placeholders used: {'SIZE': '38', 'TYPE': 'STEEL', 'REGION': 'MIDDLE EAST'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 38
    and p_type like '%STEEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'MIDDLE EAST'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'MIDDLE EAST'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [80/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#21', 'CONTAINER': 'LG CASE'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#21'
    and p_container = 'LG CASE'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [81/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-03'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-03' 
    and l_shipdate > date '1995-03-03' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [82/200] Generated Q20
-- Placeholders used: {'COLOR': 'blue', 'DATE': '1994-01-01', 'NATION': 'JAPAN'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'blue%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1994-01-01')
                and l_shipdate < date('1994-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'JAPAN'
order by
    s_name;

-- [83/200] Generated Q4
-- Placeholders used: {'DATE': '1997-09-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-09-01' 
    and o_orderdate < date '1997-09-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [84/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '4', 'QUANTITY2': '19', 'QUANTITY3': '29', 'BRAND1': 'Brand#15', 'BRAND2': 'Brand#14', 'BRAND3': 'Brand#55'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#15'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 4 and l_quantity <= 4 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#14'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 19 and l_quantity <= 19 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#55'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 29 and l_quantity <= 29 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [85/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#33', 'CONTAINER': 'SM PKG'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#33'
    and p_container = 'SM PKG'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [86/200] Generated Q22
-- Placeholders used: {'I1': '18', 'I2': '23', 'I3': '31', 'I4': '17', 'I5': '30', 'I6': '13', 'I7': '29'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('18','23','31','17','30','13','29')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('18','23','31','17','30','13','29')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [87/200] Generated Q15
-- Placeholders used: {'DATE': '1994-09-01', 'STREAM_ID': '2'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1994-09-01'
        and l_shipdate < date '1994-09-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [88/200] Generated Q1
-- Placeholders used: {'DELTA': '89'}
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
    l_shipdate <= date '1998-12-01' - interval '89' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [89/200] Generated Q20
-- Placeholders used: {'COLOR': 'royal', 'DATE': '1993-01-01', 'NATION': 'BRAZIL'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'royal%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1993-01-01')
                and l_shipdate < date('1993-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'BRAZIL'
order by
    s_name;

-- [90/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [91/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#32', 'CONTAINER': 'MED BOX'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#32'
    and p_container = 'MED BOX'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [92/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-12'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-12' 
    and l_shipdate > date '1995-03-12' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [93/200] Generated Q10
-- Placeholders used: {'DATE': '1993-07-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1993-07-01' 
    and o_orderdate < date '1993-07-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [94/200] Generated Q15
-- Placeholders used: {'DATE': '1997-06-01', 'STREAM_ID': '9'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1997-06-01'
        and l_shipdate < date '1997-06-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [95/200] Generated Q10
-- Placeholders used: {'DATE': '1994-09-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-09-01' 
    and o_orderdate < date '1994-09-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [96/200] Generated Q21
-- Placeholders used: {'NATION': 'MOZAMBIQUE'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'MOZAMBIQUE'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [97/200] Generated Q1
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

-- [98/200] Generated Q18
-- Placeholders used: {'QUANTITY': '314'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 314
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [99/200] Generated Q22
-- Placeholders used: {'I1': '13', 'I2': '31', 'I3': '23', 'I4': '17', 'I5': '29', 'I6': '30', 'I7': '18'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('13','31','23','17','29','30','18')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('13','31','23','17','29','30','18')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [100/200] Generated Q10
-- Placeholders used: {'DATE': '1994-09-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-09-01' 
    and o_orderdate < date '1994-09-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [101/200] Generated Q7
-- Placeholders used: {'NATION1': 'RUSSIA', 'NATION2': 'IRAN'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'RUSSIA' and n2.n_name = 'IRAN') 
            or (n1.n_name = 'IRAN' and n2.n_name = 'RUSSIA') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [102/200] Generated Q7
-- Placeholders used: {'NATION1': 'VIETNAM', 'NATION2': 'SAUDI ARABIA'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'VIETNAM' and n2.n_name = 'SAUDI ARABIA') 
            or (n1.n_name = 'SAUDI ARABIA' and n2.n_name = 'VIETNAM') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [103/200] Generated Q9
-- Placeholders used: {'COLOR': 'pink'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%pink%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [104/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#31', 'TYPE': 'STANDARD POLISHED', 'SIZE1': '18', 'SIZE2': '3', 'SIZE3': '1', 'SIZE4': '22', 'SIZE5': '9', 'SIZE6': '41', 'SIZE7': '17', 'SIZE8': '11'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#31' 
    and p_type not like 'STANDARD POLISHED%' 
    and p_size in (18, 3, 1, 22, 9, 41, 17, 11) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [105/200] Generated Q15
-- Placeholders used: {'DATE': '1995-12-01', 'STREAM_ID': '7'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1995-12-01'
        and l_shipdate < date '1995-12-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [106/200] Generated Q18
-- Placeholders used: {'QUANTITY': '312'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 312
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [107/200] Generated Q4
-- Placeholders used: {'DATE': '1993-05-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1993-05-01' 
    and o_orderdate < date '1993-05-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [108/200] Generated Q5
-- Placeholders used: {'REGION': 'MIDDLE EAST', 'DATE': '1993-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'MIDDLE EAST' 
    and o_orderdate >= date '1993-01-01' 
    and o_orderdate < date '1993-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [109/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'FOB', 'DATE': '1994-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('TRUCK', 'FOB') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1994-01-01' 
    and l_receiptdate < date '1994-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [110/200] Generated Q14
-- Placeholders used: {'DATE': '1993-09-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1993-09-01' 
    and l_shipdate < date '1993-09-01' + interval '1' month;

-- [111/200] Generated Q2
-- Placeholders used: {'SIZE': '20', 'TYPE': 'BRASS', 'REGION': 'AFRICA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 20
    and p_type like '%BRASS'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'AFRICA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'AFRICA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [112/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'AIR REG', 'SHIPMODE2': 'MAIL', 'DATE': '1994-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('AIR REG', 'MAIL') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1994-01-01' 
    and l_receiptdate < date '1994-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [113/200] Generated Q22
-- Placeholders used: {'I1': '13', 'I2': '23', 'I3': '30', 'I4': '29', 'I5': '18', 'I6': '17', 'I7': '31'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('13','23','30','29','18','17','31')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('13','23','30','29','18','17','31')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [114/200] Generated Q6
-- Placeholders used: {'DATE': '1994-01-01', 'DISCOUNT': '0.08', 'QUANTITY': '24'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1994-01-01' 
    and l_shipdate < date '1994-01-01' + interval '1' year 
    and l_discount between 0.08 - 0.01 and 0.08 + 0.01 
    and l_quantity < 24;

-- [115/200] Generated Q6
-- Placeholders used: {'DATE': '1995-01-01', 'DISCOUNT': '0.08', 'QUANTITY': '24'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1995-01-01' 
    and l_shipdate < date '1995-01-01' + interval '1' year 
    and l_discount between 0.08 - 0.01 and 0.08 + 0.01 
    and l_quantity < 24;

-- [116/200] Generated Q9
-- Placeholders used: {'COLOR': 'cyan'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%cyan%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [117/200] Generated Q4
-- Placeholders used: {'DATE': '1995-01-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1995-01-01' 
    and o_orderdate < date '1995-01-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [118/200] Generated Q2
-- Placeholders used: {'SIZE': '31', 'TYPE': 'NICKEL', 'REGION': 'AMERICA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 31
    and p_type like '%NICKEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'AMERICA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'AMERICA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [119/200] Generated Q15
-- Placeholders used: {'DATE': '1994-11-01', 'STREAM_ID': '5'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1994-11-01'
        and l_shipdate < date '1994-11-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [120/200] Generated Q8
-- Placeholders used: {'NATION': 'GERMANY', 'REGION': 'AFRICA', 'TYPE': 'PROMO BURNISHED STEEL'}
select 
    o_year,  
    sum(case  
        when nation = 'GERMANY'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'AFRICA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'PROMO BURNISHED STEEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [121/200] Generated Q11
-- Placeholders used: {'NATION': 'INDIA', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'INDIA' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'INDIA'
        ) 
order by 
    value desc;

-- [122/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'FURNITURE', 'DATE': '1995-03-12'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'FURNITURE' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-12' 
    and l_shipdate > date '1995-03-12' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [123/200] Generated Q21
-- Placeholders used: {'NATION': 'MOZAMBIQUE'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'MOZAMBIQUE'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [124/200] Generated Q13
-- Placeholders used: {'WORD1': 'unusual', 'WORD2': 'packages'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%unusual%packages%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [125/200] Generated Q4
-- Placeholders used: {'DATE': '1997-09-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1997-09-01' 
    and o_orderdate < date '1997-09-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [126/200] Generated Q9
-- Placeholders used: {'COLOR': 'deep'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%deep%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [127/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '5', 'QUANTITY2': '10', 'QUANTITY3': '21', 'BRAND1': 'Brand#54', 'BRAND2': 'Brand#33', 'BRAND3': 'Brand#45'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#54'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 5 and l_quantity <= 5 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#33'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 10 and l_quantity <= 10 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#45'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 21 and l_quantity <= 21 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [128/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#14', 'CONTAINER': 'LG BOX'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#14'
    and p_container = 'LG BOX'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [129/200] Generated Q7
-- Placeholders used: {'NATION1': 'INDIA', 'NATION2': 'ARGENTINA'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'INDIA' and n2.n_name = 'ARGENTINA') 
            or (n1.n_name = 'ARGENTINA' and n2.n_name = 'INDIA') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [130/200] Generated Q14
-- Placeholders used: {'DATE': '1993-01-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1993-01-01' 
    and l_shipdate < date '1993-01-01' + interval '1' month;

-- [131/200] Generated Q17
-- Placeholders used: {'BRAND': 'Brand#52', 'CONTAINER': 'MED BOX'}
select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = 'Brand#52'
    and p_container = 'MED BOX'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );

-- [132/200] Generated Q14
-- Placeholders used: {'DATE': '1993-05-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1993-05-01' 
    and l_shipdate < date '1993-05-01' + interval '1' month;

-- [133/200] Generated Q22
-- Placeholders used: {'I1': '23', 'I2': '30', 'I3': '17', 'I4': '13', 'I5': '18', 'I6': '31', 'I7': '29'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('23','30','17','13','18','31','29')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('23','30','17','13','18','31','29')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [134/200] Generated Q22
-- Placeholders used: {'I1': '29', 'I2': '23', 'I3': '17', 'I4': '18', 'I5': '30', 'I6': '13', 'I7': '31'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('29','23','17','18','30','13','31')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('29','23','17','18','30','13','31')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [135/200] Generated Q14
-- Placeholders used: {'DATE': '1996-07-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1996-07-01' 
    and l_shipdate < date '1996-07-01' + interval '1' month;

-- [136/200] Generated Q13
-- Placeholders used: {'WORD1': 'pending', 'WORD2': 'accounts'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%pending%accounts%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [137/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'accounts'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%accounts%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [138/200] Generated Q10
-- Placeholders used: {'DATE': '1993-08-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1993-08-01' 
    and o_orderdate < date '1993-08-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [139/200] Generated Q14
-- Placeholders used: {'DATE': '1997-03-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1997-03-01' 
    and l_shipdate < date '1997-03-01' + interval '1' month;

-- [140/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '10', 'QUANTITY2': '20', 'QUANTITY3': '25', 'BRAND1': 'Brand#44', 'BRAND2': 'Brand#42', 'BRAND3': 'Brand#54'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#44'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 10 and l_quantity <= 10 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#42'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 20 and l_quantity <= 20 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#54'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 25 and l_quantity <= 25 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [141/200] Generated Q6
-- Placeholders used: {'DATE': '1993-01-01', 'DISCOUNT': '0.06', 'QUANTITY': '25'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1993-01-01' 
    and l_shipdate < date '1993-01-01' + interval '1' year 
    and l_discount between 0.06 - 0.01 and 0.06 + 0.01 
    and l_quantity < 25;

-- [142/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-22'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-22' 
    and l_shipdate > date '1995-03-22' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [143/200] Generated Q10
-- Placeholders used: {'DATE': '1993-09-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1993-09-01' 
    and o_orderdate < date '1993-09-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [144/200] Generated Q7
-- Placeholders used: {'NATION1': 'EGYPT', 'NATION2': 'ALGERIA'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'EGYPT' and n2.n_name = 'ALGERIA') 
            or (n1.n_name = 'ALGERIA' and n2.n_name = 'EGYPT') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [145/200] Generated Q2
-- Placeholders used: {'SIZE': '16', 'TYPE': 'STEEL', 'REGION': 'MIDDLE EAST'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 16
    and p_type like '%STEEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'MIDDLE EAST'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'MIDDLE EAST'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [146/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'HOUSEHOLD', 'DATE': '1995-03-14'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'HOUSEHOLD' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-14' 
    and l_shipdate > date '1995-03-14' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [147/200] Generated Q21
-- Placeholders used: {'NATION': 'CHINA'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'CHINA'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [148/200] Generated Q7
-- Placeholders used: {'NATION1': 'RUSSIA', 'NATION2': 'JAPAN'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'RUSSIA' and n2.n_name = 'JAPAN') 
            or (n1.n_name = 'JAPAN' and n2.n_name = 'RUSSIA') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [149/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#42', 'TYPE': 'SMALL ANODIZED', 'SIZE1': '49', 'SIZE2': '7', 'SIZE3': '28', 'SIZE4': '15', 'SIZE5': '12', 'SIZE6': '45', 'SIZE7': '34', 'SIZE8': '30'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#42' 
    and p_type not like 'SMALL ANODIZED%' 
    and p_size in (49, 7, 28, 15, 12, 45, 34, 30) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [150/200] Generated Q2
-- Placeholders used: {'SIZE': '36', 'TYPE': 'NICKEL', 'REGION': 'AFRICA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 36
    and p_type like '%NICKEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'AFRICA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'AFRICA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [151/200] Generated Q15
-- Placeholders used: {'DATE': '1993-09-01', 'STREAM_ID': '8'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1993-09-01'
        and l_shipdate < date '1993-09-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [152/200] Generated Q22
-- Placeholders used: {'I1': '30', 'I2': '17', 'I3': '18', 'I4': '23', 'I5': '31', 'I6': '29', 'I7': '13'}
select
    cntrycode,
    count(*) as numcust,
    sum(c_acctbal) as totacctbal
from (
    select
        substring(c_phone from 1 for 2) as cntrycode,
        c_acctbal
    from
        customer
    where
        substring(c_phone from 1 for 2) in ('30','17','18','23','31','29','13')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('30','17','18','23','31','29','13')
        )
        and not exists (
            select *
            from
                orders
            where
                o_custkey = c_custkey
        )
    ) as custsale
group by
    cntrycode
order by
    cntrycode;

-- [153/200] Generated Q6
-- Placeholders used: {'DATE': '1996-01-01', 'DISCOUNT': '0.09', 'QUANTITY': '25'}
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '1996-01-01' 
    and l_shipdate < date '1996-01-01' + interval '1' year 
    and l_discount between 0.09 - 0.01 and 0.09 + 0.01 
    and l_quantity < 25;

-- [154/200] Generated Q8
-- Placeholders used: {'NATION': 'SAUDI ARABIA', 'REGION': 'ASIA', 'TYPE': 'ECONOMY POLISHED NICKEL'}
select 
    o_year,  
    sum(case  
        when nation = 'SAUDI ARABIA'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'ASIA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'ECONOMY POLISHED NICKEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [155/200] Generated Q9
-- Placeholders used: {'COLOR': 'navy'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%navy%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [156/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'FURNITURE', 'DATE': '1995-03-08'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'FURNITURE' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-08' 
    and l_shipdate > date '1995-03-08' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [157/200] Generated Q9
-- Placeholders used: {'COLOR': 'lawn'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%lawn%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [158/200] Generated Q11
-- Placeholders used: {'NATION': 'PERU', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'PERU' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'PERU'
        ) 
order by 
    value desc;

-- [159/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'BUILDING', 'DATE': '1995-03-05'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'BUILDING' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-05' 
    and l_shipdate > date '1995-03-05' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [160/200] Generated Q8
-- Placeholders used: {'NATION': 'JAPAN', 'REGION': 'AMERICA', 'TYPE': 'PROMO BURNISHED TIN'}
select 
    o_year,  
    sum(case  
        when nation = 'JAPAN'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'AMERICA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'PROMO BURNISHED TIN'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [161/200] Generated Q14
-- Placeholders used: {'DATE': '1995-03-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1995-03-01' 
    and l_shipdate < date '1995-03-01' + interval '1' month;

-- [162/200] Generated Q11
-- Placeholders used: {'NATION': 'PERU', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'PERU' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'PERU'
        ) 
order by 
    value desc;

-- [163/200] Generated Q15
-- Placeholders used: {'DATE': '1995-03-01', 'STREAM_ID': '1'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1995-03-01'
        and l_shipdate < date '1995-03-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [164/200] Generated Q7
-- Placeholders used: {'NATION1': 'JORDAN', 'NATION2': 'JAPAN'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'JORDAN' and n2.n_name = 'JAPAN') 
            or (n1.n_name = 'JAPAN' and n2.n_name = 'JORDAN') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [165/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '1', 'QUANTITY2': '19', 'QUANTITY3': '26', 'BRAND1': 'Brand#41', 'BRAND2': 'Brand#33', 'BRAND3': 'Brand#44'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#41'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 1 and l_quantity <= 1 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#33'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 19 and l_quantity <= 19 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#44'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 26 and l_quantity <= 26 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [166/200] Generated Q18
-- Placeholders used: {'QUANTITY': '313'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 313
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [167/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#23', 'TYPE': 'LARGE POLISHED', 'SIZE1': '2', 'SIZE2': '25', 'SIZE3': '22', 'SIZE4': '43', 'SIZE5': '44', 'SIZE6': '26', 'SIZE7': '11', 'SIZE8': '30'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#23' 
    and p_type not like 'LARGE POLISHED%' 
    and p_size in (2, 25, 22, 43, 44, 26, 11, 30) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [168/200] Generated Q5
-- Placeholders used: {'REGION': 'MIDDLE EAST', 'DATE': '1997-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'MIDDLE EAST' 
    and o_orderdate >= date '1997-01-01' 
    and o_orderdate < date '1997-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [169/200] Generated Q1
-- Placeholders used: {'DELTA': '118'}
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
    l_shipdate <= date '1998-12-01' - interval '118' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [170/200] Generated Q13
-- Placeholders used: {'WORD1': 'special', 'WORD2': 'packages'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%special%packages%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [171/200] Generated Q21
-- Placeholders used: {'NATION': 'JORDAN'}
select
    s_name,
    count(*) as numwait
from
    supplier,
    lineitem l1,
    orders,
    nation
where
    s_suppkey = l1.l_suppkey
    and o_orderkey = l1.l_orderkey
    and o_orderstatus = 'F'
    and l1.l_receiptdate > l1.l_commitdate
    and exists (
        select *
        from
            lineitem l2
        where
            l2.l_orderkey = l1.l_orderkey
            and l2.l_suppkey <> l1.l_suppkey
    )
    and not exists (
        select *
        from
            lineitem l3
        where
            l3.l_orderkey = l1.l_orderkey
            and l3.l_suppkey <> l1.l_suppkey
            and l3.l_receiptdate > l3.l_commitdate
    )
    and s_nationkey = n_nationkey
    and n_name = 'JORDAN'
group by
    s_name
order by
    numwait desc,
    s_name;

-- [172/200] Generated Q5
-- Placeholders used: {'REGION': 'EUROPE', 'DATE': '1994-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'EUROPE' 
    and o_orderdate >= date '1994-01-01' 
    and o_orderdate < date '1994-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [173/200] Generated Q2
-- Placeholders used: {'SIZE': '17', 'TYPE': 'STEEL', 'REGION': 'ASIA'}
select
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
from
    part,
    supplier,
    partsupp,
    nation,
    region
where
    p_partkey = ps_partkey
    and s_suppkey = ps_suppkey
    and p_size = 17
    and p_type like '%STEEL'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = 'ASIA'
    and ps_supplycost = (
        select
            min (ps_supplycost)
        from
            partsupp, supplier,
            nation, region
        where
            p_partkey = ps_partkey
            and s_suppkey = ps_suppkey
            and s_nationkey = n_nationkey
            and n_regionkey = r_regionkey
            and r_name = 'ASIA'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;

-- [174/200] Generated Q7
-- Placeholders used: {'NATION1': 'KENYA', 'NATION2': 'IRAN'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'KENYA' and n2.n_name = 'IRAN') 
            or (n1.n_name = 'IRAN' and n2.n_name = 'KENYA') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [175/200] Generated Q11
-- Placeholders used: {'NATION': 'UNITED STATES', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'UNITED STATES' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'UNITED STATES'
        ) 
order by 
    value desc;

-- [176/200] Generated Q13
-- Placeholders used: {'WORD1': 'unusual', 'WORD2': 'deposits'}
select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%unusual%deposits%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc;

-- [177/200] Generated Q9
-- Placeholders used: {'COLOR': 'brown'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%brown%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [178/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#15', 'TYPE': 'STANDARD PLATED', 'SIZE1': '15', 'SIZE2': '42', 'SIZE3': '5', 'SIZE4': '49', 'SIZE5': '3', 'SIZE6': '2', 'SIZE7': '16', 'SIZE8': '13'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#15' 
    and p_type not like 'STANDARD PLATED%' 
    and p_size in (15, 42, 5, 49, 3, 2, 16, 13) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [179/200] Generated Q1
-- Placeholders used: {'DELTA': '99'}
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
    l_shipdate <= date '1998-12-01' - interval '99' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

-- [180/200] Generated Q5
-- Placeholders used: {'REGION': 'AMERICA', 'DATE': '1994-01-01'}
select n_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue 
FROM
    customer,  
    orders,  
    lineitem,  
    supplier,  
    nation,  
    region 
WHERE
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and l_suppkey = s_suppkey 
    and c_nationkey = s_nationkey 
    and s_nationkey = n_nationkey 
    and n_regionkey = r_regionkey 
    and r_name = 'AMERICA' 
    and o_orderdate >= date '1994-01-01' 
    and o_orderdate < date '1994-01-01' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;

-- [181/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#15', 'TYPE': 'SMALL POLISHED', 'SIZE1': '45', 'SIZE2': '17', 'SIZE3': '24', 'SIZE4': '11', 'SIZE5': '39', 'SIZE6': '46', 'SIZE7': '8', 'SIZE8': '47'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#15' 
    and p_type not like 'SMALL POLISHED%' 
    and p_size in (45, 17, 24, 11, 39, 46, 8, 47) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [182/200] Generated Q10
-- Placeholders used: {'DATE': '1993-05-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1993-05-01' 
    and o_orderdate < date '1993-05-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [183/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '1', 'QUANTITY2': '14', 'QUANTITY3': '29', 'BRAND1': 'Brand#44', 'BRAND2': 'Brand#21', 'BRAND3': 'Brand#52'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#44'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 1 and l_quantity <= 1 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#21'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 14 and l_quantity <= 14 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#52'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 29 and l_quantity <= 29 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [184/200] Generated Q4
-- Placeholders used: {'DATE': '1996-09-01'}
select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '1996-09-01' 
    and o_orderdate < date '1996-09-01' + interval '3' month 
    and exists ( 
        select 
            *
        from  
            lineitem 
        where  
            l_orderkey = o_orderkey 
            and l_commitdate < l_receiptdate
    ) 
group by  
    o_orderpriority 
order by  
    o_orderpriority;

-- [185/200] Generated Q10
-- Placeholders used: {'DATE': '1994-11-01'}
select 
    c_custkey,  
    c_name,  
    sum(l_extendedprice * (1 - l_discount)) as revenue, 
    c_acctbal,  
    n_name,  
    c_address,  
    c_phone,  
    c_comment 
from  
    customer,  
    orders,  
    lineitem,  
    nation 
where  
    c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate >= date '1994-11-01' 
    and o_orderdate < date '1994-11-01' + interval '3' month 
    and l_returnflag = 'R' 
    and c_nationkey = n_nationkey 
group by  
    c_custkey,  
    c_name,  
    c_acctbal,  
    c_phone,  
    n_name,  
    c_address,  
    c_comment 
order by  
    revenue desc;

-- [186/200] Generated Q20
-- Placeholders used: {'COLOR': 'chocolate', 'DATE': '1997-01-01', 'NATION': 'ARGENTINA'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'chocolate%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1997-01-01')
                and l_shipdate < date('1997-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'ARGENTINA'
order by
    s_name;

-- [187/200] Generated Q12
-- Placeholders used: {'SHIPMODE1': 'TRUCK', 'SHIPMODE2': 'SHIP', 'DATE': '1995-01-01'}
select 
    l_shipmode,  
    sum(case  
        when o_orderpriority ='1-URGENT' 
            or o_orderpriority ='2-HIGH' 
        then 1 
        else 0 
    end) as high_line_count, 
    sum(case  
        when o_orderpriority <> '1-URGENT' 
            and o_orderpriority <> '2-HIGH' 
        then 1 
        else 0 
    end) as low_line_count 
from  
    orders,  
    lineitem 
where  
    o_orderkey = l_orderkey 
    and l_shipmode in ('TRUCK', 'SHIP') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '1995-01-01' 
    and l_receiptdate < date '1995-01-01' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode;

-- [188/200] Generated Q3
-- Placeholders used: {'SEGMENT': 'MACHINERY', 'DATE': '1995-03-21'}
select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = 'MACHINERY' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '1995-03-21' 
    and l_shipdate > date '1995-03-21' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;

-- [189/200] Generated Q11
-- Placeholders used: {'NATION': 'ALGERIA', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'ALGERIA' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'ALGERIA'
        ) 
order by 
    value desc;

-- [190/200] Generated Q14
-- Placeholders used: {'DATE': '1997-05-01'}
select 
    100.00 * sum(case  
        when p_type like 'PROMO%' 
        then l_extendedprice*(1-l_discount) 
        else 0 
    end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue 
from  
    lineitem,  
    part 
where  
    l_partkey = p_partkey 
    and l_shipdate >= date '1997-05-01' 
    and l_shipdate < date '1997-05-01' + interval '1' month;

-- [191/200] Generated Q16
-- Placeholders used: {'BRAND': 'Brand#14', 'TYPE': 'MEDIUM POLISHED', 'SIZE1': '46', 'SIZE2': '10', 'SIZE3': '28', 'SIZE4': '12', 'SIZE5': '34', 'SIZE6': '42', 'SIZE7': '18', 'SIZE8': '40'}
select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> 'Brand#14' 
    and p_type not like 'MEDIUM POLISHED%' 
    and p_size in (46, 10, 28, 12, 34, 42, 18, 40) 
    and ps_suppkey not in ( 
        select  
            s_suppkey 
        from  
            supplier 
        where  
            s_comment like '%Customer%Complaints%' 
    ) 
group by  
    p_brand,  
    p_type,  
    p_size 
order by  
    supplier_cnt desc,  
    p_brand,  
    p_type,  
    p_size;

-- [192/200] Generated Q18
-- Placeholders used: {'QUANTITY': '315'}
select
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
from
    customer,
    orders,
    lineitem
where
    o_orderkey in (
        select
            l_orderkey
        from
            lineitem
        group by
            l_orderkey having
                sum(l_quantity) > 315
    )
    and c_custkey = o_custkey
    and o_orderkey = l_orderkey
group by
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
order by
    o_totalprice desc,
    o_orderdate;

-- [193/200] Generated Q15
-- Placeholders used: {'DATE': '1995-04-01', 'STREAM_ID': '10'}
with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '1995-04-01'
        and l_shipdate < date '1995-04-01' + interval '3' month
    group by
        l_suppkey
)
select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    supplier,
    revenue
where
    s_suppkey = supplier_no
    and total_revenue = (
        select
            max(total_revenue)
        from
            revenue
    )
order by
    s_suppkey;

-- [194/200] Generated Q9
-- Placeholders used: {'COLOR': 'lavender'}
select  
    nation,  
    o_year,  
    sum(amount) as sum_profit 
from ( 
    select  
        n_name as nation,  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount 
    from  
        part,  
        supplier,  
        lineitem,  
        partsupp,  
        orders,  
        nation 
    where  
        s_suppkey = l_suppkey 
        and ps_suppkey = l_suppkey 
        and ps_partkey = l_partkey 
        and p_partkey = l_partkey 
        and o_orderkey = l_orderkey 
        and s_nationkey = n_nationkey 
        and p_name like '%lavender%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc;

-- [195/200] Generated Q8
-- Placeholders used: {'NATION': 'BRAZIL', 'REGION': 'ASIA', 'TYPE': 'LARGE BURNISHED STEEL'}
select 
    o_year,  
    sum(case  
        when nation = 'BRAZIL'  
        then volume 
        else 0 
    end) / sum(volume) as mkt_share 
from ( 
    select  
        extract(year from o_orderdate) as o_year, 
        l_extendedprice * (1-l_discount) as volume,  
        n2.n_name as nation 
    from  
        part,  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2,  
        region 
    where  
        p_partkey = l_partkey 
        and s_suppkey = l_suppkey 
        and l_orderkey = o_orderkey 
        and o_custkey = c_custkey 
        and c_nationkey = n1.n_nationkey 
        and n1.n_regionkey = r_regionkey 
        and r_name = 'ASIA' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = 'LARGE BURNISHED STEEL'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year;

-- [196/200] Generated Q19
-- Placeholders used: {'QUANTITY1': '10', 'QUANTITY2': '20', 'QUANTITY3': '26', 'BRAND1': 'Brand#31', 'BRAND2': 'Brand#43', 'BRAND3': 'Brand#24'}
select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#31'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= 10 and l_quantity <= 10 + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#43'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= 20 and l_quantity <= 20 + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = 'Brand#24'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= 26 and l_quantity <= 26 + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );

-- [197/200] Generated Q7
-- Placeholders used: {'NATION1': 'IRAQ', 'NATION2': 'INDIA'}
select 
    supp_nation,  
    cust_nation,  
    l_year, sum(volume) as revenue 
from ( 
    select  
        n1.n_name as supp_nation,  
        n2.n_name as cust_nation,  
        extract(year from l_shipdate) as l_year, 
        l_extendedprice * (1 - l_discount) as volume 
    from  
        supplier,  
        lineitem,  
        orders,  
        customer,  
        nation n1,  
        nation n2 
    where  
        s_suppkey = l_suppkey 
        and o_orderkey = l_orderkey 
        and c_custkey = o_custkey 
        and s_nationkey = n1.n_nationkey 
        and c_nationkey = n2.n_nationkey 
        and ( 
            (n1.n_name = 'IRAQ' and n2.n_name = 'INDIA') 
            or (n1.n_name = 'INDIA' and n2.n_name = 'IRAQ') 
        ) 
        and l_shipdate between date '1995-01-01' and date '1996-12-31' 
    ) as shipping 
group by  
    supp_nation,  
    cust_nation,  
    l_year 
order by  
    supp_nation,  
    cust_nation,  
    l_year;

-- [198/200] Generated Q11
-- Placeholders used: {'NATION': 'INDIA', 'FRACTION': '0.0001'}
select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = 'INDIA' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * 0.0001 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = 'INDIA'
        ) 
order by 
    value desc;

-- [199/200] Generated Q20
-- Placeholders used: {'COLOR': 'wheat', 'DATE': '1995-01-01', 'NATION': 'PERU'}
select
    s_name,
    s_address
from
    supplier, nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    part
                where
                    p_name like 'wheat%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('1995-01-01')
                and l_shipdate < date('1995-01-01') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = 'PERU'
order by
    s_name;

-- [200/200] Generated Q1
-- Placeholders used: {'DELTA': '93'}
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
    l_shipdate <= date '1998-12-01' - interval '93' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;

