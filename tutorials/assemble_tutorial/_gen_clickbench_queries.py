"""One-shot script to (re)write the tutorial's self-describing ``clickbench_queries.json``.

The *official* 43 ClickBench queries are all static SQL - no placeholders. That is a poor fit for
demonstrating a bespoke *generated* engine: with a fixed, finite query set the "engine" could in
principle just memoize each of the 43 answers instead of implementing the actual filter/aggregate
logic, and there would be no way to tell the difference from the outside. TPC-H and CEB avoid this
because their queries are parameterized - a correct engine must implement the predicate/aggregate
generically, since it is evaluated against a fresh value on every call.

So instead of the official suite, this emits **our own 10 templated queries**, derived from ten of
the 43 originals (see the comment above each) but with representative literals replaced by typed
``[PLACEHOLDER]`` specs - the same ``params``/``param_groups`` shape TPC-H's ``queries.json`` uses.
Each placeholder's value space is sampled at run time (with the run's seeded RNG), so every call
substitutes a fresh, syntactically-identical-but-semantically-different query - exercising real
per-call computation, not lookup.

A few literals could not be responsibly turned into typed value spaces without access to the real
data (not available at generation time): most notably ``CounterID = 62``, the one specific counter
the official queries 37-43 all hardcode as the one confirmed to have substantial real traffic in
this dataset - an arbitrary alternative CounterID would almost always match nothing. Those are
left as plain literals; everything else that varies (thresholds, LIMIT/OFFSET, date windows,
tokens, an IN-list) is templated. Once ``hits.duckdb`` exists, a query like
``SELECT CounterID, COUNT(*) c FROM hits GROUP BY 1 ORDER BY c DESC LIMIT 20`` finds other
high-traffic counters worth promoting into a real ``categorical`` domain.
"""

import json
from pathlib import Path

TUTORIAL_DIR = Path(
    __file__
).parent.parent  # tutorials/, where the demo reads clickbench_queries.json

CLICKBENCH_TEMPLATED_QUERIES: dict[str, dict] = {
    # Based on official Q2/Q8 (AdvEngineID breakdown). The original filters `<> 0`; sweeping a
    # threshold instead varies selectivity from "almost everything" to "almost nothing".
    "1": {
        "sql": (
            "SELECT AdvEngineID, COUNT(*) AS c FROM hits "
            "WHERE AdvEngineID > [MIN_ADV_ENGINE_ID] GROUP BY AdvEngineID ORDER BY c DESC;"
        ),
        "params": {
            "MIN_ADV_ENGINE_ID": {"type": "int", "min": 0, "max": 44, "step": 1}
        },
    },
    # Based on official Q9 (top regions by distinct users). Templates the LIMIT.
    "2": {
        "sql": (
            "SELECT RegionID, COUNT(DISTINCT UserID) AS u FROM hits "
            "GROUP BY RegionID ORDER BY u DESC LIMIT [TOPK];"
        ),
        "params": {"TOPK": {"type": "int", "min": 5, "max": 50, "step": 5}},
    },
    # Based on official Q11 (mobile phone models). Adds a HAVING threshold and templates the
    # LIMIT, so both the group filter and the result size vary per call.
    "3": {
        "sql": (
            "SELECT MobilePhoneModel, COUNT(DISTINCT UserID) AS u FROM hits "
            "WHERE MobilePhoneModel <> '' GROUP BY MobilePhoneModel "
            "HAVING COUNT(*) >= [MIN_HITS] ORDER BY u DESC LIMIT [TOPK];"
        ),
        "params": {
            "MIN_HITS": {"type": "int", "min": 10, "max": 10000, "step": 10},
            "TOPK": {"type": "int", "min": 5, "max": 25, "step": 5},
        },
    },
    # Based on official Q13/Q22 (SearchPhrase text search). TOKEN is a generic substring
    # domain - not sampled from real data (unavailable at generation time), so some draws will
    # legitimately match nothing; that is a real, valid outcome for a text-search predicate.
    "4": {
        "sql": (
            "SELECT SearchPhrase, COUNT(DISTINCT UserID) AS u FROM hits "
            "WHERE SearchPhrase <> '' AND SearchPhrase ILIKE '%[TOKEN]%' "
            "GROUP BY SearchPhrase ORDER BY u DESC LIMIT 10;"
        ),
        "params": {
            "TOKEN": {
                "type": "categorical",
                "values": ["a", "e", "o", "com", "ru", "google", "news", "2013"],
            }
        },
    },
    # Based on official Q20 (point lookup by UserID). A literal UserID would need a real sampled
    # value (unavailable at generation time) or it would almost never match; a modulo bucket is
    # data-agnostic and always selects a well-defined ~1% slice, varying which slice per call.
    "5": {
        "sql": "SELECT COUNT(*), COUNT(DISTINCT UserID) FROM hits WHERE UserID % 100 = [REMAINDER];",
        "params": {"REMAINDER": {"type": "int", "min": 0, "max": 99, "step": 1}},
    },
    # Based on official Q40/Q41 (TraficSourceID IN-list). A k-distinct sample group, the same
    # pattern TPC-H Q7/Q16/Q22 use for a k-distinct IN-list.
    "6": {
        "sql": (
            "SELECT TraficSourceID, COUNT(*) AS c FROM hits "
            "WHERE TraficSourceID IN ([TS1], [TS2], [TS3]) GROUP BY TraficSourceID ORDER BY c DESC;"
        ),
        "param_groups": [
            {
                "type": "sample",
                "placeholders": ["TS1", "TS2", "TS3"],
                "domain": [-1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "distinct": True,
            }
        ],
    },
    # Based on official Q37 (top URLs by pageviews for the one known-hot CounterID=62). Templates
    # the date window (start + width) and the LIMIT; CounterID stays a literal (see module
    # docstring).
    "7": {
        "sql": (
            "SELECT URL, COUNT(*) AS PageViews FROM hits "
            "WHERE CounterID = 62 AND EventDate >= date '[DATE_FROM]' "
            "AND EventDate < date '[DATE_FROM]' + interval '[WINDOW_DAYS]' day "
            "AND DontCountHits = 0 AND IsRefresh = 0 AND URL <> '' "
            "GROUP BY URL ORDER BY PageViews DESC LIMIT [TOPK];"
        ),
        "params": {
            "DATE_FROM": {"type": "date", "min": "2013-07-01", "max": "2013-07-24"},
            "WINDOW_DAYS": {"type": "int", "min": 1, "max": 7, "step": 1},
            "TOPK": {"type": "int", "min": 5, "max": 25, "step": 5},
        },
    },
    # Based on official Q29 (referrer-domain extraction via regex). Templates the HAVING
    # threshold that gates which domains appear.
    "8": {
        "sql": (
            r"SELECT REGEXP_REPLACE(Referer, '^https?://(?:www\.)?([^/]+)/.*$', '\1') AS domain, "
            "AVG(STRLEN(Referer)) AS l, COUNT(*) AS c FROM hits WHERE Referer <> '' "
            "GROUP BY domain HAVING COUNT(*) > [MIN_COUNT] ORDER BY l DESC LIMIT 25;"
        ),
        "params": {
            "MIN_COUNT": {"type": "int", "min": 1000, "max": 200000, "step": 1000}
        },
    },
    # Based on official Q39 (paginated URL pageviews). Templates the date window start,
    # LIMIT, and OFFSET together, exercising pagination with a moving window.
    "9": {
        "sql": (
            "SELECT URL, COUNT(*) AS PageViews FROM hits "
            "WHERE CounterID = 62 AND EventDate >= date '[DATE_FROM]' "
            "AND EventDate < date '[DATE_FROM]' + interval '7' day "
            "AND IsRefresh = 0 AND IsLink <> 0 AND IsDownload = 0 "
            "GROUP BY URL ORDER BY PageViews DESC LIMIT [TOPK] OFFSET [OFFSET];"
        ),
        "params": {
            "DATE_FROM": {"type": "date", "min": "2013-07-01", "max": "2013-07-24"},
            "TOPK": {"type": "int", "min": 5, "max": 25, "step": 5},
            "OFFSET": {"type": "int", "min": 0, "max": 2000, "step": 100},
        },
    },
    # Based on official Q43 (minute-truncated pageview time series). Templates the window start,
    # width (in hours), LIMIT, and OFFSET.
    "10": {
        "sql": (
            "SELECT DATE_TRUNC('minute', EventTime) AS M, COUNT(*) AS PageViews FROM hits "
            "WHERE CounterID = 62 AND EventTime >= date '[DATE_FROM]' "
            "AND EventTime < date '[DATE_FROM]' + interval '[WINDOW_HOURS]' hour "
            "AND IsRefresh = 0 GROUP BY M ORDER BY M LIMIT [TOPK] OFFSET [OFFSET];"
        ),
        "params": {
            "DATE_FROM": {"type": "date", "min": "2013-07-01", "max": "2013-07-29"},
            "WINDOW_HOURS": {"type": "int", "min": 6, "max": 48, "step": 6},
            "TOPK": {"type": "int", "min": 5, "max": 20, "step": 5},
            "OFFSET": {"type": "int", "min": 0, "max": 500, "step": 50},
        },
    },
}


def build() -> dict:
    return CLICKBENCH_TEMPLATED_QUERIES


if __name__ == "__main__":
    data = build()
    out = TUTORIAL_DIR / "clickbench_queries.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Written: {out}")
