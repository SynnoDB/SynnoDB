import datetime as dt
import logging
import random
from typing import Dict, Optional, Tuple

from synnodb.workloads.dataset.tpch.tpch_queries import tpc_h

logger = logging.getLogger(__name__)

REGIONS = [
    "AFRICA",
    "AMERICA",
    "ASIA",
    "EUROPE",
    "MIDDLE EAST",
]

NATIONS = [
    "ALGERIA",
    "ARGENTINA",
    "BRAZIL",
    "CANADA",
    "EGYPT",
    "ETHIOPIA",
    "FRANCE",
    "GERMANY",
    "INDIA",
    "INDONESIA",
    "IRAN",
    "IRAQ",
    "JAPAN",
    "JORDAN",
    "KENYA",
    "MOROCCO",
    "MOZAMBIQUE",
    "PERU",
    "CHINA",
    "ROMANIA",
    "SAUDI ARABIA",
    "VIETNAM",
    "RUSSIA",
    "UNITED KINGDOM",
    "UNITED STATES",
]

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

TYPE_SYLLABLE1 = ["STANDARD", "SMALL", "MEDIUM", "LARGE", "ECONOMY", "PROMO"]
TYPE_SYLLABLE2 = ["ANODIZED", "BURNISHED", "PLATED", "POLISHED", "BRUSHED"]
TYPE_SYLLABLE3 = ["TIN", "NICKEL", "BRASS", "STEEL", "COPPER"]

WORD1_OPTIONS = ["special", "pending", "unusual", "express"]
WORD2_OPTIONS = ["packages", "requests", "accounts", "deposits"]

COUNTRY_CODES = ["13", "31", "23", "29", "30", "18", "17"]

SCALE_FACTOR = 1.0


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
        placeholders["STREAM_ID"] = str(rnd.randint(1, 10))  # only the view name
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
