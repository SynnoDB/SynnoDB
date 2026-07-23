"""Declarative parameter-value spaces for the TPC-H queries.

This is the *authoritative declarative* encoding of each query's substitution-parameter space,
mirroring the ranges/choices the imperative generator
(:func:`synnodb.workloads.dataset.gen_tpch.gen_tpch_query.gen_query`) draws. Each entry is in
the same typed-spec form a bring-your-own ``queries.json`` uses (see
:mod:`synnodb.workloads.query_params`), so it both (a) seeds the self-describing tutorial
``queries.json`` and (b) drives live-UI input widgets for built-in TPC-H runs.

The categorical domains are imported from ``gen_tpch_query`` so the two stay in sync. Composed
domains (BRAND = ``Brand#<1-5><1-5>``, the part-type syllable products) are built here.

NOTE: ``gen_query`` remains the sampler used at run time today; this table currently duplicates
its ranges. A future change can have ``gen_query`` consume these specs so there is a single
source of truth (see plan).
"""

from __future__ import annotations

from tutorials.workloads.tpch.gen_tpch_query import (
    COLORS,
    CONTAINERS,
    COUNTRY_CODES,
    NATIONS,
    REGIONS,
    SEGMENTS,
    SHIP_MODES,
    TYPE_SYLLABLE1,
    TYPE_SYLLABLE2,
    TYPE_SYLLABLE3,
    WORD1_OPTIONS,
    WORD2_OPTIONS,
)

# Composed categorical domains (the generator builds these on the fly).
BRANDS = [f"Brand#{i}{j}" for i in range(1, 6) for j in range(1, 6)]
TYPES_FULL = [
    f"{a} {b} {c}"
    for a in TYPE_SYLLABLE1
    for b in TYPE_SYLLABLE2
    for c in TYPE_SYLLABLE3
]
TYPES_PREFIX = [f"{a} {b}" for a in TYPE_SYLLABLE1 for b in TYPE_SYLLABLE2]


def _int(lo: int, hi: int, step: int = 1) -> dict:
    return {"type": "int", "min": lo, "max": hi, "step": step}


def _float(lo: float, hi: float, step: float) -> dict:
    return {"type": "float", "min": lo, "max": hi, "step": step}


def _date(lo: str, hi: str) -> dict:
    return {"type": "date", "min": lo, "max": hi}


def _cat(values) -> dict:
    return {"type": "categorical", "values": list(values)}


def _sample(placeholders, domain, distinct: bool = True) -> dict:
    return {
        "type": "sample",
        "placeholders": list(placeholders),
        "domain": list(domain),
        "distinct": distinct,
    }


# Per-query value spaces. Keys are bare query ids; each maps to a section with optional
# ``params`` (scalar specs) and ``param_groups`` (joint specs). Placeholder coverage matches
# the templates in :data:`tpch_queries.tpc_h` exactly.
TPCH_PARAM_SPECS: dict[str, dict] = {
    "1": {"params": {"DELTA": _int(60, 120)}},
    "2": {
        "params": {
            "SIZE": _int(1, 50),
            "TYPE": _cat(TYPE_SYLLABLE3),
            "REGION": _cat(REGIONS),
        }
    },
    "3": {
        "params": {
            "SEGMENT": _cat(SEGMENTS),
            "DATE": _date("1995-03-01", "1995-03-31"),
        }
    },
    "4": {"params": {"DATE": _date("1993-01-01", "1997-10-01")}},
    "5": {
        "params": {
            "REGION": _cat(REGIONS),
            "DATE": _date("1993-01-01", "1997-01-01"),
        }
    },
    "6": {
        "params": {
            "DATE": _date("1993-01-01", "1997-01-01"),
            "DISCOUNT": _float(0.02, 0.09, 0.01),
            "QUANTITY": _int(24, 25),
        }
    },
    "7": {"param_groups": [_sample(["NATION1", "NATION2"], NATIONS)]},
    "8": {
        "params": {
            "NATION": _cat(NATIONS),
            "REGION": _cat(REGIONS),
            "TYPE": _cat(TYPES_FULL),
        }
    },
    "9": {"params": {"COLOR": _cat(COLORS)}},
    "10": {"params": {"DATE": _date("1993-02-01", "1995-01-01")}},
    "11": {"params": {"NATION": _cat(NATIONS), "FRACTION": _cat(["0.0001"])}},
    "12": {
        "params": {"DATE": _date("1993-01-01", "1997-01-01")},
        "param_groups": [_sample(["SHIPMODE1", "SHIPMODE2"], SHIP_MODES)],
    },
    "13": {"params": {"WORD1": _cat(WORD1_OPTIONS), "WORD2": _cat(WORD2_OPTIONS)}},
    "14": {"params": {"DATE": _date("1993-01-01", "1997-12-01")}},
    "15": {"params": {"DATE": _date("1993-01-01", "1997-12-01")}},
    "16": {
        "params": {"BRAND": _cat(BRANDS), "TYPE": _cat(TYPES_PREFIX)},
        "param_groups": [
            _sample([f"SIZE{i}" for i in range(1, 9)], list(range(1, 51)))
        ],
    },
    "17": {"params": {"BRAND": _cat(BRANDS), "CONTAINER": _cat(CONTAINERS)}},
    "18": {"params": {"QUANTITY": _int(312, 315)}},
    "19": {
        "params": {
            "QUANTITY1": _int(1, 10),
            "QUANTITY2": _int(10, 20),
            "QUANTITY3": _int(20, 30),
            "BRAND1": _cat(BRANDS),
            "BRAND2": _cat(BRANDS),
            "BRAND3": _cat(BRANDS),
        }
    },
    "20": {
        "params": {
            "COLOR": _cat(COLORS),
            "DATE": _date("1993-01-01", "1997-01-01"),
            "NATION": _cat(NATIONS),
        }
    },
    "21": {"params": {"NATION": _cat(NATIONS)}},
    "22": {"param_groups": [_sample([f"I{i}" for i in range(1, 8)], COUNTRY_CODES)]},
}
