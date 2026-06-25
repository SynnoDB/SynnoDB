import datetime as dt
import logging
import random
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# --- GLOBAL DEFINITIONS & DOMAINS ---

SEGMENTS = [
    "AUTOMOBILE",
    "BUILDING",
    "FURNITURE",
    "HOUSEHOLD",
    "MACHINERY",
]

SHIP_MODES = [
    "AIR",
    "AIR REG",
    "RAIL",
    "SHIP",
    "TRUCK",
    "MAIL",
    "FOB",
]

CONTAINERS = [
    "SM CASE",
    "SM BOX",
    "SM PACK",
    "SM PKG",
    "MED BAG",
    "MED BOX",
    "MED PACK",
    "MED PKG",
    "LG CASE",
    "LG BOX",
    "LG PACK",
    "LG PKG",
]

COLORS = [
    "almond",
    "antique",
    "aquamarine",
    "azure",
    "beige",
    "bisque",
    "black",
    "blanched",
    "blue",
    "blush",
    "brown",
    "burlywood",
    "burnished",
    "chartreuse",
    "chiffon",
    "chocolate",
    "coral",
    "cornflower",
    "cornsilk",
    "cream",
    "cyan",
    "dark",
    "deep",
    "dim",
    "dodger",
    "drab",
    "firebrick",
    "floral",
    "forest",
    "frosted",
    "gainsboro",
    "ghost",
    "goldenrod",
    "green",
    "grey",
    "honeydew",
    "hot",
    "indian",
    "ivory",
    "khaki",
    "lace",
    "lavender",
    "lawn",
    "lemon",
    "light",
    "lime",
    "linen",
    "magenta",
    "maroon",
    "medium",
    "metallic",
    "midnight",
    "mint",
    "misty",
    "moccasin",
    "navajo",
    "navy",
    "olive",
    "orange",
    "orchid",
    "pale",
    "papaya",
    "peach",
    "peru",
    "pink",
    "plum",
    "powder",
    "puff",
    "purple",
    "red",
    "rose",
    "rosy",
    "royal",
    "saddle",
    "salmon",
    "sandy",
    "seashell",
    "sienna",
    "sky",
    "slate",
    "smoke",
    "snow",
    "spring",
    "steel",
    "tan",
    "thistle",
    "tomato",
    "turquoise",
    "violet",
    "wheat",
    "white",
    "yellow",
]

TYPE_SYLLABLE3 = ["TIN", "NICKEL", "BRASS", "STEEL", "COPPER"]

WORD1_OPTIONS = ["special", "pending", "unusual", "express"]

COUNTRY_CODES = ["13", "31", "23", "29", "30", "18", "17"]

# --- HELPER FUNCTIONS ---


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


# --- QUERY TEMPLATES ---

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
    c_acctbal desc;""",
}

# --- GENERATOR FUNCTIONS ---


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
