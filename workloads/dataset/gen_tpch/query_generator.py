import datetime as dt
import logging
import random
import argparse
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# --- GLOBAL DEFINITIONS & DOMAINS ---

REGIONS = [
    "AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST",
]

NATIONS = [
    "ALGERIA", "ARGENTINA", "BRAZIL", "CANADA", "EGYPT", "ETHIOPIA",
    "FRANCE", "GERMANY", "INDIA", "INDONESIA", "IRAN", "IRAQ",
    "JAPAN", "JORDAN", "KENYA", "MOROCCO", "MOZAMBIQUE", "PERU",
    "CHINA", "ROMANIA", "SAUDI ARABIA", "VIETNAM", "RUSSIA",
    "UNITED KINGDOM", "UNITED STATES",
]

SEGMENTS = [
    "AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY",
]

SHIP_MODES = [
    "AIR", "AIR REG", "RAIL", "SHIP", "TRUCK", "MAIL", "FOB",
]

CONTAINERS = [
    "SM CASE", "SM BOX", "SM PACK", "SM PKG",
    "MED BAG", "MED BOX", "MED PACK", "MED PKG",
    "LG CASE", "LG BOX", "LG PACK", "LG PKG",
]

COLORS = [
    "almond", "antique", "aquamarine", "azure", "beige", "bisque",
    "black", "blanched", "blue", "blush", "brown", "burlywood",
    "burnished", "chartreuse", "chiffon", "chocolate", "coral",
    "cornflower", "cornsilk", "cream", "cyan", "dark", "deep",
    "dim", "dodger", "drab", "firebrick", "floral", "forest",
    "frosted", "gainsboro", "ghost", "goldenrod", "green", "grey",
    "honeydew", "hot", "indian", "ivory", "khaki", "lace",
    "lavender", "lawn", "lemon", "light", "lime", "linen",
    "magenta", "maroon", "medium", "metallic", "midnight", "mint",
    "misty", "moccasin", "navajo", "navy", "olive", "orange",
    "orchid", "pale", "papaya", "peach", "peru", "pink", "plum",
    "powder", "puff", "purple", "red", "rose", "rosy", "royal",
    "saddle", "salmon", "sandy", "seashell", "sienna", "sky",
    "slate", "smoke", "snow", "spring", "steel", "tan", "thistle",
    "tomato", "turquoise", "violet", "wheat", "white", "yellow",
]

TYPE_SYLLABLE1 = ["STANDARD", "SMALL", "MEDIUM", "LARGE", "ECONOMY", "PROMO"]
TYPE_SYLLABLE2 = ["ANODIZED", "BURNISHED", "PLATED", "POLISHED", "BRUSHED"]
TYPE_SYLLABLE3 = ["TIN", "NICKEL", "BRASS", "STEEL", "COPPER"]

WORD1_OPTIONS = ["special", "pending", "unusual", "express"]
WORD2_OPTIONS = ["packages", "requests", "accounts", "deposits"]

COUNTRY_CODES = ["13", "31", "23", "29", "30", "18", "17"]

SCALE_FACTOR = 1.0

# --- HELPER FUNCTIONS ---

def _random_date(rnd: random.Random, start: dt.date, end: dt.date) -> dt.date:
    delta_days = (end - start).days
    return start + dt.timedelta(days=rnd.randint(0, delta_days))

def _random_month_start(
    rnd: random.Random, start_year: int, start_month: int, end_year: int, end_month: int
) -> dt.date:
    start_index = start_year * 12 + (start_month - 1)
    end_index = end_year * 12 + (end_month - 1)
    month_index = rnd.randint(start_index, end_index)
    year = month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, 1)

def _random_brand(rnd: random.Random) -> str:
    return f"Brand#{rnd.randint(1, 5)}{rnd.randint(1, 5)}"

def _random_type_full(rnd: random.Random) -> str:
    return f"{rnd.choice(TYPE_SYLLABLE1)} {rnd.choice(TYPE_SYLLABLE2)} {rnd.choice(TYPE_SYLLABLE3)}"

def _random_type_prefix(rnd: random.Random) -> str:
    return f"{rnd.choice(TYPE_SYLLABLE1)} {rnd.choice(TYPE_SYLLABLE2)}"

def _format_fraction(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


# --- QUERY TEMPLATES ---

tpc_h = {
    "Q1": """select 
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
    l_shipdate <= date '1998-12-01' - interval '[DELTA]' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus; """,
    "Q2": """select
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
    and p_size = [SIZE]
    and p_type like '%[TYPE]'
    and s_nationkey = n_nationkey
    and n_regionkey = r_regionkey
    and r_name = '[REGION]'
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
            and r_name = '[REGION]'
        )
order by
    s_acctbal desc,
    n_name,
    s_name,
    p_partkey;""",
    "Q3": """select l_orderkey,  
    sum(l_extendedprice*(1-l_discount)) as revenue, 
    o_orderdate,  
    o_shippriority 
FROM
    customer,  
    orders,  
    lineitem 
WHERE
    c_mktsegment = '[SEGMENT]' 
    and c_custkey = o_custkey 
    and l_orderkey = o_orderkey 
    and o_orderdate < date '[DATE]' 
    and l_shipdate > date '[DATE]' 
GROUP BY
    l_orderkey,  
    o_orderdate,  
    o_shippriority 
ORDER BY
    revenue desc,  
    o_orderdate;""",
    "Q4": """select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '[DATE]' 
    and o_orderdate < date '[DATE]' + interval '3' month 
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

""",
    "Q5": """select n_name,  
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
    and r_name = '[REGION]' 
    and o_orderdate >= date '[DATE]' 
    and o_orderdate < date '[DATE]' + interval '1' year 
GROUP BY
    n_name 
ORDER BY
    revenue desc;""",
    "Q6": """
select 
    sum(l_extendedprice*l_discount) as revenue 
from  
    lineitem 
where  
    l_shipdate >= date '[DATE]' 
    and l_shipdate < date '[DATE]' + interval '1' year 
    and l_discount between [DISCOUNT] - 0.01 and [DISCOUNT] + 0.01 
    and l_quantity < [QUANTITY];
""",
    "Q7": """select 
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
            (n1.n_name = '[NATION1]' and n2.n_name = '[NATION2]') 
            or (n1.n_name = '[NATION2]' and n2.n_name = '[NATION1]') 
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
    l_year; """,
    "Q8": """select 
    o_year,  
    sum(case  
        when nation = '[NATION]'  
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
        and r_name = '[REGION]' 
        and s_nationkey = n2.n_nationkey 
        and o_orderdate between date '1995-01-01' and date '1996-12-31' 
        and p_type = '[TYPE]'  
    ) as all_nations 
group by  
    o_year 
order by  
    o_year; """,
    "Q9": """select  
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
        and p_name like '%[COLOR]%' 
    ) as profit 
group by  
    nation,  
    o_year 
order by  
    nation,  
    o_year desc; """,
    "Q10": """select 
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
    and o_orderdate >= date '[DATE]' 
    and o_orderdate < date '[DATE]' + interval '3' month 
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
    revenue desc; """,
    "Q11": """select 
    ps_partkey,  
    sum(ps_supplycost * ps_availqty) as value 
from  
    partsupp,  
    supplier,  
    nation 
where  
    ps_suppkey = s_suppkey 
    and s_nationkey = n_nationkey 
    and n_name = '[NATION]' 
group by  
    ps_partkey having  
        sum(ps_supplycost * ps_availqty) > ( 
            select  
                sum(ps_supplycost * ps_availqty) * [FRACTION] 
            from  
                partsupp,  
                supplier,  
                nation 
            where  
                ps_suppkey = s_suppkey 
                and s_nationkey = n_nationkey 
                and n_name = '[NATION]'
        ) 
order by 
    value desc;""",
    "Q12": """select 
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
    and l_shipmode in ('[SHIPMODE1]', '[SHIPMODE2]') 
    and l_commitdate < l_receiptdate 
    and l_shipdate < l_commitdate 
    and l_receiptdate >= date '[DATE]' 
    and l_receiptdate < date '[DATE]' + interval '1' year 
group by  
    l_shipmode 
order by  
    l_shipmode; """,
    "Q13": """select  
    c_count, count(*) as custdist  
from ( 
    select  
        c_custkey, 
        count(o_orderkey)  
    from  
        customer left outer join orders on  
            c_custkey = o_custkey 
            and o_comment not like '%[WORD1]%[WORD2]%' 
    group by  
        c_custkey 
    )as c_orders (c_custkey, c_count) 
group by  
    c_count 
order by  
    custdist desc,  
    c_count desc; """,
    "Q14": """select 
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
    and l_shipdate >= date '[DATE]' 
    and l_shipdate < date '[DATE]' + interval '1' month; """,
    "Q15": """with revenue (supplier_no, total_revenue) as (
    select
        l_suppkey,
        sum(l_extendedprice * (1 - l_discount))
    from
        lineitem
    where
        l_shipdate >= date '[DATE]'
        and l_shipdate < date '[DATE]' + interval '3' month
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
    s_suppkey;""",
    "Q16": """select 
    p_brand,  
    p_type,  
    p_size,  
    count(distinct ps_suppkey) as supplier_cnt 
from  
    partsupp,  
    part 
where  
    p_partkey = ps_partkey 
    and p_brand <> '[BRAND]' 
    and p_type not like '[TYPE]%' 
    and p_size in ([SIZE1], [SIZE2], [SIZE3], [SIZE4], [SIZE5], [SIZE6], [SIZE7], [SIZE8]) 
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
    p_size;""",
    "Q17": """select
    sum (l_extendedprice) / 7.0 as avg_yearly
from
    lineitem,
    part
where
    p_partkey = l_partkey
    and p_brand = '[BRAND]'
    and p_container = '[CONTAINER]'
    and l_quantity < (
        select
            0.2 * avg(l_quantity)
        from
            lineitem
        where
            l_partkey = p_partkey
    );""",
    "Q18": """select
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
                sum(l_quantity) > [QUANTITY]
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
    o_orderdate;""",
    "Q19": """select
    sum(l_extendedprice * (1 - l_discount) ) as revenue
from
    lineitem,
    part
where
    (
        p_partkey = l_partkey
        and p_brand = '[BRAND1]'
        and p_container in ( 'SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
        and l_quantity >= [QUANTITY1] and l_quantity <= [QUANTITY1] + 10
        and p_size between 1 and 5
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = '[BRAND2]'
        and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        and l_quantity >= [QUANTITY2] and l_quantity <= [QUANTITY2] + 10
        and p_size between 1 and 10
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    )
    or
    (
        p_partkey = l_partkey
        and p_brand = '[BRAND3]'
        and p_container in ( 'LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        and l_quantity >= [QUANTITY3] and l_quantity <= [QUANTITY3] + 10
        and p_size between 1 and 15
        and l_shipmode in ('AIR', 'AIR REG')
        and l_shipinstruct = 'DELIVER IN PERSON'
    );""",
    "Q20": """select
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
                    p_name like '[COLOR]%'
                    )
        and ps_availqty > (
            select
                0.5 * sum(l_quantity)
            from
                lineitem
            where
                l_partkey = ps_partkey
                and l_suppkey = ps_suppkey
                and l_shipdate >= date('[DATE]')
                and l_shipdate < date('[DATE]') + interval '1' year
        )
    )
    and s_nationkey = n_nationkey
    and n_name = '[NATION]'
order by
    s_name;""",
    "Q21": """select
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
    and n_name = '[NATION]'
group by
    s_name
order by
    numwait desc,
    s_name;""",
    "Q22": """select
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
        substring(c_phone from 1 for 2) in ('[I1]','[I2]','[I3]','[I4]','[I5]','[I6]','[I7]')
        and c_acctbal > (
            select
                avg(c_acctbal)
            from
                customer
            where
                c_acctbal > 0.00
                and substring (c_phone from 1 for 2) in ('[I1]','[I2]','[I3]','[I4]','[I5]','[I6]','[I7]')
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
    cntrycode;"""
}


single_table_queries = {
    "STQ1": """select 
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
    l_shipdate <= date '1998-12-01' - interval '[DELTA]' day 
group by  
    l_returnflag,  
    l_linestatus 
order by  
    l_returnflag,  
    l_linestatus;""",

    "STQ2": """select
    o_orderpriority,  
    count(*) as order_count 
from  
    orders 
where  
    o_orderdate >= date '[DATE]' 
    and o_orderdate < date '[DATE]' + interval '3' month 
group by  
    o_orderpriority 
order by  
    o_orderpriority;""",

    "STQ3": """select 
    c_custkey, 
    c_name, 
    c_acctbal, 
    c_phone 
from 
    customer 
where 
    c_mktsegment = '[SEGMENT]' 
order by 
    c_acctbal desc;""",

    "STQ4": """select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = '[BRAND]'
    and p_container = '[CONTAINER]'
order by
    p_retailprice desc;""",

    "STQ5": """select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%[WORD1]%'
order by
    s_acctbal desc;""",

    "STQ6": """select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('[SHIPMODE1]', '[SHIPMODE2]')
    and l_shipdate >= date '[DATE]'
group by
    l_shipmode
order by
    l_shipmode;""",

    "STQ7": """select
    p_partkey,
    p_name,
    p_type,
    p_size
from
    part
where
    p_name like '%[COLOR]%'
    and p_type like '%[TYPE]'
order by
    p_partkey;""",

    "STQ8": """select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('[I1]','[I2]','[I3]')
order by
    c_acctbal desc;"""
}

# --- GENERATOR FUNCTIONS ---

def gen_query(
    query_name: str = "Q1", rnd: Optional[random.Random] = None, seed: int = 42
) -> Tuple[str, str, Dict[str, str]]:
    if query_name not in tpc_h:
        raise KeyError(f"Unknown TPC-H query name: {query_name}")

    if rnd is None:
        rnd = random.Random(seed)
    placeholders: Dict[str, str] = {}

    if query_name == "Q1":
        placeholders["DELTA"] = str(rnd.randint(60, 120))
    elif query_name == "Q2":
        placeholders["SIZE"] = str(rnd.randint(1, 50))
        placeholders["TYPE"] = rnd.choice(TYPE_SYLLABLE3)
        placeholders["REGION"] = rnd.choice(REGIONS)
    elif query_name == "Q3":
        placeholders["SEGMENT"] = rnd.choice(SEGMENTS)
        date_val = _random_date(rnd, dt.date(1995, 3, 1), dt.date(1995, 3, 31))
        placeholders["DATE"] = date_val.isoformat()
    elif query_name == "Q4":
        date_val = _random_month_start(rnd, 1993, 1, 1997, 10)
        placeholders["DATE"] = date_val.isoformat()
    elif query_name == "Q5":
        placeholders["REGION"] = rnd.choice(REGIONS)
        placeholders["DATE"] = f"{rnd.randint(1993, 1997)}-01-01"
    elif query_name == "Q6":
        placeholders["DATE"] = f"{rnd.randint(1993, 1997)}-01-01"
        placeholders["DISCOUNT"] = f"{rnd.randint(2, 9) / 100:.2f}"
        placeholders["QUANTITY"] = str(rnd.randint(24, 25))
    elif query_name == "Q7":
        nation1, nation2 = rnd.sample(NATIONS, 2)
        placeholders["NATION1"] = nation1
        placeholders["NATION2"] = nation2
    elif query_name == "Q8":
        placeholders["NATION"] = rnd.choice(NATIONS)
        placeholders["REGION"] = rnd.choice(REGIONS)
        placeholders["TYPE"] = _random_type_full(rnd)
    elif query_name == "Q9":
        placeholders["COLOR"] = rnd.choice(COLORS)
    elif query_name == "Q10":
        date_val = _random_month_start(rnd, 1993, 2, 1995, 1)
        placeholders["DATE"] = date_val.isoformat()
    elif query_name == "Q11":
        placeholders["NATION"] = rnd.choice(NATIONS)
        placeholders["FRACTION"] = _format_fraction(0.0001 / SCALE_FACTOR)
    elif query_name == "Q12":
        shipmode1, shipmode2 = rnd.sample(SHIP_MODES, 2)
        placeholders["SHIPMODE1"] = shipmode1
        placeholders["SHIPMODE2"] = shipmode2
        placeholders["DATE"] = f"{rnd.randint(1993, 1997)}-01-01"
    elif query_name == "Q13":
        placeholders["WORD1"] = rnd.choice(WORD1_OPTIONS)
        placeholders["WORD2"] = rnd.choice(WORD2_OPTIONS)
    elif query_name == "Q14":
        date_val = _random_month_start(rnd, 1993, 1, 1997, 12)
        placeholders["DATE"] = date_val.isoformat()
    elif query_name == "Q15":
        date_val = _random_month_start(rnd, 1993, 1, 1997, 12)
        placeholders["DATE"] = date_val.isoformat()
        placeholders["STREAM_ID"] = str(rnd.randint(1, 10))
    elif query_name == "Q16":
        placeholders["BRAND"] = _random_brand(rnd)
        placeholders["TYPE"] = _random_type_prefix(rnd)
        sizes = rnd.sample(range(1, 51), 8)
        for idx, size in enumerate(sizes, start=1):
            placeholders[f"SIZE{idx}"] = str(size)
    elif query_name == "Q17":
        placeholders["BRAND"] = _random_brand(rnd)
        placeholders["CONTAINER"] = rnd.choice(CONTAINERS)
    elif query_name == "Q18":
        placeholders["QUANTITY"] = str(rnd.randint(312, 315))
    elif query_name == "Q19":
        placeholders["QUANTITY1"] = str(rnd.randint(1, 10))
        placeholders["QUANTITY2"] = str(rnd.randint(10, 20))
        placeholders["QUANTITY3"] = str(rnd.randint(20, 30))
        placeholders["BRAND1"] = _random_brand(rnd)
        placeholders["BRAND2"] = _random_brand(rnd)
        placeholders["BRAND3"] = _random_brand(rnd)
    elif query_name == "Q20":
        placeholders["COLOR"] = rnd.choice(COLORS)
        placeholders["DATE"] = f"{rnd.randint(1993, 1997)}-01-01"
        placeholders["NATION"] = rnd.choice(NATIONS)
    elif query_name == "Q21":
        placeholders["NATION"] = rnd.choice(NATIONS)
    elif query_name == "Q22":
        codes = rnd.sample(COUNTRY_CODES, 7)
        for idx, code in enumerate(codes, start=1):
            placeholders[f"I{idx}"] = code
    else:
        raise ValueError(f"No placeholder generator defined for {query_name}")

    template = tpc_h[query_name]
    query = template
    for key, value in placeholders.items():
        query = query.replace(f"[{key}]", value)

    return template, query, placeholders

def gen_single_table_query(
    query_name: str = "STQ1", rnd: Optional[random.Random] = None, seed: int = 42
) -> Tuple[str, str, Dict[str, str]]:
    if query_name not in single_table_queries:
        raise KeyError(f"Unknown Single Table query name: {query_name}")

    if rnd is None:
        rnd = random.Random(seed)
    placeholders: Dict[str, str] = {}

    if query_name == "STQ1":
        placeholders["DELTA"] = str(rnd.randint(60, 120))
    elif query_name == "STQ2":
        date_val = _random_month_start(rnd, 1993, 1, 1997, 10)
        placeholders["DATE"] = date_val.isoformat()
    elif query_name == "STQ3":
        placeholders["SEGMENT"] = rnd.choice(SEGMENTS)
    elif query_name == "STQ4":
        placeholders["BRAND"] = _random_brand(rnd)
        placeholders["CONTAINER"] = rnd.choice(CONTAINERS)
    elif query_name == "STQ5":
        placeholders["WORD1"] = rnd.choice(WORD1_OPTIONS)
    elif query_name == "STQ6":
        shipmode1, shipmode2 = rnd.sample(SHIP_MODES, 2)
        placeholders["SHIPMODE1"] = shipmode1
        placeholders["SHIPMODE2"] = shipmode2
        placeholders["DATE"] = f"{rnd.randint(1993, 1997)}-01-01"
    elif query_name == "STQ7":
        placeholders["COLOR"] = rnd.choice(COLORS)
        placeholders["TYPE"] = rnd.choice(TYPE_SYLLABLE3)
    elif query_name == "STQ8":
        codes = rnd.sample(COUNTRY_CODES, 3)
        placeholders["I1"] = codes[0]
        placeholders["I2"] = codes[1]
        placeholders["I3"] = codes[2]
    else:
        raise ValueError(f"No placeholder generator defined for {query_name}")

    template = single_table_queries[query_name]
    query = template
    for key, value in placeholders.items():
        query = query.replace(f"[{key}]", value)

    return template, query, placeholders


# --- MAIN EXECUTION BLOCK ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate TPC-H queries (Single-Table, Multi-Table, or Mixed).")
    parser.add_argument("-n", "--num", type=int, default=5, help="Number of queries to generate (default: 5)")
    parser.add_argument("-q", "--query", type=str, default="ALL", help="Specific query to generate (e.g., Q1 or STQ1) or 'ALL'")
    parser.add_argument("-m", "--mode", type=str, choices=["single", "multi", "mixed"], default="mixed", help="Query selection mode: 'single', 'multi' (original TPC-H), or 'mixed'")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("-o", "--output", type=str, default=None, help="File to save the generated SQL queries")

    args = parser.parse_args()

    rnd_gen = random.Random(args.seed)
    
    valid_single = list(single_table_queries.keys())
    valid_multi = list(tpc_h.keys())
    
    # Setup query pool based on mode
    if args.mode == "single":
        query_pool = valid_single
    elif args.mode == "multi":
        query_pool = valid_multi
    else: # mixed
        query_pool = valid_single + valid_multi
    
    output_file = None
    if args.output:
        output_file = open(args.output, 'w', encoding='utf-8')
        print(f"--- Generating {args.num} queries to {args.output} (Mode: {args.mode}, Query: {args.query}, Seed: {args.seed}) ---")
    else:
        print(f"--- Generating {args.num} queries (Mode: {args.mode}, Query: {args.query}, Seed: {args.seed}) ---")
    
    for i in range(args.num):
        q_name = args.query if args.query != "ALL" else rnd_gen.choice(query_pool)
        
        try:
            if q_name in valid_single:
                template, final_query, placeholders = gen_single_table_query(query_name=q_name, rnd=rnd_gen)
            elif q_name in valid_multi:
                template, final_query, placeholders = gen_query(query_name=q_name, rnd=rnd_gen)
            else:
                raise ValueError(f"Query {q_name} not found in available templates.")
            
            if output_file:
                output_file.write(f"-- [{i+1}/{args.num}] Generated {q_name}\n")
                output_file.write(f"-- Placeholders used: {placeholders}\n")
                output_file.write(final_query.strip() + "\n\n")
            else:
                print(f"\n[{i+1}/{args.num}] Generated {q_name}")
                print(f"Placeholders used: {placeholders}")
                print("-" * 40)
                print(final_query.strip())
                print("-" * 40)
        except Exception as e:
            msg = f"-- Error generating {q_name}: {e}"
            if output_file:
                output_file.write(msg + "\n\n")
            else:
                print(f"\n[{i+1}/{args.num}] {msg}")
                
    if output_file:
        output_file.close()
        print("Done!")
