"""
Query Execution Strategy Classifier for Bespoke OLAP Engine
============================================================
Reads the generated C++ query code in output/ and asks an LLM to classify what
query execution strategies were applied per query -- join operators, scan methods,
aggregation/order techniques, and low-level execution optimizations.

Uses llms.py (same directory as classify_storage.py) for LLM calls and caching.

Run from the repo root:
  python plots/classify_bespoke_execution/classify_execution.py
"""

import argparse
import json

# add parent directory to sys.path for llms.py import
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.append(str(REPO_ROOT))

from synnodb.observability.plots.classify_bespoke_execution.strategy_display_names import (
    STRATEGY_DISPLAY_NAMES,
)
from synnodb.observability.plots.classify_bespoke_storage import llms
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.logging_and_reporting.wandb_api_helper import wandb_retrieve_metrics_for_run

# -- path setup ----------------------------------------------------------------
THIS_DIR = Path(__file__).parent


# -- config --------------------------------------------------------------------
OUTPUT_DIR = REPO_ROOT / "output"
MODEL = "gpt-5-2025-08-07"


# -- execution strategy taxonomy -----------------------------------------------
#
# Canonical strategy names with descriptions. The LLM must use exactly these
# names so results can be aggregated into a table.
#

# ---- Join Operators ----------------------------------------------------------
JOIN_OPERATOR_TAXONOMY = {
    "bitmap_semi_join": (
        "Build a dense bitmap or boolean vector keyed by the join key of the "
        "smaller (build) side, then probe it from the larger (probe) side with "
        "O(1) array lookups. Implements a semi-join: only the existence of a "
        "match matters, not the payload. "
        "(e.g. cust_in_segment[custkey], partkey_color[partkey], nation_in_region[nkey])"
    ),
    "index_nested_loop_join": (
        "For each qualifying row on the outer (driving) side, use a prebuilt "
        "positional index (order_directory, row_by_orderkey) to jump directly "
        "to the matching rows on the inner side -- no hash table, no sort-merge. "
        "Equivalent to a classic index nested-loop join (INLJ). "
        "(e.g. order_directory[orderkey] -> [start, end) range in lineitem)"
    ),
    "tag_array_join": (
        "Replace the join key with a compact integer tag (e.g. nation index) "
        "stored in a dense array keyed by the foreign key. At probe time the tag "
        "is fetched in O(1) and compared, combining the join and a filter predicate "
        "in one step. "
        "(e.g. cust_tag[custkey] = nation_idx, supp_tag[suppkey] = nation_idx, "
        "cust_nationkey[custkey], supp_nationkey[suppkey])"
    ),
    "hash_join": (
        "Build phase inserts (key, payload) pairs into a hash map; probe phase "
        "looks up matching keys via hashing. Used when the join key space is too "
        "sparse or too large for a dense bitmap/tag array. "
        "(e.g. FlatHashMap keyed on pack_key(partkey, suppkey), "
        "unordered_map for name_to_key)"
    ),
    "sort_merge_join": (
        "Both inputs sorted on the join key; a single forward pass merges them. "
        "Exploits physical sort order (e.g. partsupp sorted by suppkey) to avoid "
        "building any lookup structure."
    ),
    "streaming_group_join": (
        "Lineitem is physically sorted by orderkey; consecutive rows with the "
        "same orderkey are aggregated in a single streaming pass without any "
        "lookup structure. Conceptually combines a sort-merge join with inline "
        "aggregation."
    ),
}

# ---- Scan & Filter Strategies ------------------------------------------------
SCAN_FILTER_TAXONOMY = {
    "full_table_scan": (
        "Sequential scan over all rows of a table with predicate evaluation "
        "per row (no index, no shard skipping). Typical for the fact table "
        "when no temporal or secondary index applies."
    ),
    "shard_skip_scan": (
        "Iterate over temporal (or other) shards; use per-shard min/max "
        "metadata (zone maps) to skip entire shards that cannot satisfy the "
        "predicate. Only rows in qualifying shards are visited."
    ),
    "sorted_range_scan": (
        "Exploit physical sort order of a column (e.g. orders.orderdate) to "
        "locate the first and last qualifying rows via binary search "
        "(lower_bound / upper_bound), then scan only that contiguous range."
    ),
    "sorted_key_range_lookup": (
        "Use binary search (lower_bound / upper_bound) on a sorted foreign "
        "key column (e.g. partsupp.suppkey) to locate the range of rows "
        "belonging to a specific key, avoiding a full scan."
    ),
    "dictionary_predicate_rewrite": (
        "Evaluate a string/enum predicate once against the dictionary entries "
        "to produce a set of qualifying integer codes, then compare only "
        "integer codes in the hot scan loop. "
        "(e.g. type_has_suffix[], allowed_index[] for shipmode, segment_code)"
    ),
    "precomputed_flag_scan": (
        "Read a precomputed boolean flag column (is_late, fast_ship, "
        "order_has_late, orderpriority_is_high) instead of re-evaluating "
        "the underlying multi-column predicate at query time."
    ),
    "secondary_index_scan": (
        "Use a secondary per-shard index (e.g. row_indices_by_shipmode) to "
        "directly access only those rows matching a secondary predicate, "
        "avoiding scanning non-matching rows within a shard."
    ),
    "bitmap_substring_test": (
        "Substring / word-membership filter implemented as a single bit-AND "
        "against a precomputed per-row bitmask of word presence (built into "
        "storage). Eliminates std::string::find() in the hot loop. "
        "(e.g. Q9 evaluates p_name LIKE '%COLOR%' as "
        "(p_name_word_mask_lo[i] & lo_mask) | (p_name_word_mask_hi[i] & hi_mask))"
    ),
    "memmem_substring_filter": (
        "General substring LIKE evaluated with glibc memchr (SIMD-accelerated "
        "first-byte scan) plus memcmp confirmation. Used when the search "
        "vocabulary is open / not dictionary-encoded. "
        "(e.g. Q13 NOT LIKE '%WORD1%WORD2%' via nested memchr+memcmp on the "
        "flat o_comment_data byte arena)"
    ),
}

# ---- Aggregation & Ordering Strategies ---------------------------------------
AGGREGATION_TAXONOMY = {
    "direct_array_aggregation": (
        "Aggregate into a pre-allocated flat array indexed by a small integer "
        "group key (dictionary code, nationkey). O(1) per-row cost, no hash "
        "table overhead. "
        "(e.g. revenue_by_nation[], counts[orderpriority_code], "
        "aggregates[returnflag * status_count + linestatus])"
    ),
    "dense_key_aggregation": (
        "Aggregate into a dense vector indexed by a key with a known max "
        "value (e.g. partkey, orderkey, suppkey). Works when the key domain "
        "is bounded and not too sparse. "
        "(e.g. sum_qty[orderkey], part_sum[partkey], wait_counts[suppkey])"
    ),
    "hash_aggregation": (
        "Aggregate into a hash map keyed by a composite or sparse key. "
        "Used when the group key is too large or sparse for a dense array. "
        "(e.g. FlatHashMap keyed by pack_key(partkey, suppkey))"
    ),
    "scalar_aggregation": (
        "Accumulate into a single scalar variable (no grouping). "
        "The entire qualifying set reduces to one number. "
        "(e.g. Q6 revenue_raw, Q14 promo_revenue/total_revenue)"
    ),
    "inline_aggregation": (
        "Aggregation is fused into the innermost scan/join loop rather than "
        "being a separate operator. Avoids materializing intermediate join "
        "results. "
        "(e.g. Q3 computes revenue per orderkey inside the lineitem range loop; "
        "Q12 increments high/low counts inside the shard loop)"
    ),
    "two_phase_aggregation": (
        "First pass computes a per-group aggregate (e.g. sum, min); second "
        "pass uses the aggregate as a filter or for a HAVING clause. "
        "(e.g. Q2: first pass finds min supplycost per partkey, second pass "
        "selects rows matching the min; Q11: compute sum then filter by threshold; "
        "Q18: sum quantity per orderkey then filter by threshold)"
    ),
    "parallel_radix_sort": (
        "Multi-pass parallel MSD radix sort over integer-keyed records: "
        "per-thread histogram, sequential prefix-sum, parallel scatter; "
        "repeated per radix digit. Used for sorting result sets whose key "
        "width fits a small number of passes. "
        "(e.g. Q10 3-pass 12-bit radix sort over 36-bit revenue keys)"
    ),
    "pairwise_merge_tree_sort": (
        "Each thread std::sorts its local slice; sorted runs are merged in a "
        "pairwise tree (log n_threads levels of std::merge). Combines parallel "
        "local sort with a partially-parallel merge phase. "
        "(e.g. Q3 result-row sort by (revenue desc, orderdate))"
    ),
    "scalar_std_sort": (
        "Final ordering done with a single std::sort on the main thread. "
        "Used when result cardinality is small enough that parallel sort "
        "overhead would dominate. (e.g. Q1, Q5, Q9, Q11, Q22 final sort steps)"
    ),
}

# ---- Low-Level Execution Optimizations ---------------------------------------
EXECUTION_OPT_TAXONOMY = {
    "software_prefetching": (
        "Explicit __builtin_prefetch() calls in the inner loop to hide memory "
        "latency for upcoming array accesses. "
        "(e.g. prefetch extendedprice, discount, quantity at offset +32)"
    ),
    "loop_unrolling": (
        "Manual 4x or Nx loop unrolling to reduce branch overhead and increase "
        "ILP. May be combined with #pragma unroll-loops. "
        "(e.g. Q4 processes 4 orders per iteration)"
    ),
    "branch_hint_optimization": (
        "__builtin_expect() annotations on branch conditions to guide CPU "
        "branch prediction toward the common-case path. "
        "(e.g. __builtin_expect(*shipdate <= cutoff, 1) -- most rows qualify)"
    ),
    "pointer_restrict_scan": (
        "All column pointers declared with __restrict to guarantee no aliasing, "
        "enabling the compiler to generate tighter vectorized code."
    ),
    "compiler_target_directives": (
        '#pragma GCC optimize("O3,unroll-loops") and '
        '#pragma GCC target("avx2,bmi,bmi2,...") to enable aggressive '
        "auto-vectorization and modern instruction sets."
    ),
    "lookup_table_arithmetic": (
        "Precomputed lookup arrays (discount_factors[], tax_factors[]) replace "
        "repeated floating-point arithmetic in the inner loop with a single "
        "array dereference."
    ),
    "integer_scaled_arithmetic": (
        "Prices, discounts, and quantities stored as scaled integers; all "
        "arithmetic in the hot loop uses integer multiply/divide, avoiding "
        "floating-point until final output formatting."
    ),
    "branchless_range_check": (
        "Range predicates encoded as unsigned subtraction + comparison "
        "(static_cast<uint32_t>(x - lo) >= span_u) to eliminate a branch. "
        "Replaces `x >= lo && x < hi` with a single comparison."
    ),
    "hand_vectorized_simd": (
        "Inner loop hand-written with vector intrinsics (AVX-512, AVX2, SSE2) "
        "rather than relying on compiler auto-vectorization. Distinct from "
        "compiler_target_directives which only sets compile flags without "
        "writing intrinsics. "
        "(e.g. Q1 _mm512_mullo_epi32 / _mm512_reduce_add_epi64 reduction kernel; "
        "Q12 AVX2 packed-word scan; Q18 SSE2 hsum_u16_cnt for short qty sums; "
        "Q22 _mm512_cmpeq_epi8_mask country-code SIMD comparison)"
    ),
    "magic_number_division": (
        "Replace integer division by a constant divisor with a precomputed "
        "magic-constant multiplication + shift, avoiding IDIV in vector loops. "
        "(e.g. Q1 divide-by-100 implemented as ((int64_t)x * 1374389535LL) >> 37; "
        "Q11 stripe routing uses (pk * magic) >> 32 to avoid 64-bit IDIV)"
    ),
}


ALL_STRATEGIES: dict[str, str] = {
    **JOIN_OPERATOR_TAXONOMY,
    **SCAN_FILTER_TAXONOMY,
    **AGGREGATION_TAXONOMY,
    **EXECUTION_OPT_TAXONOMY,
}

# Which group each strategy belongs to (for display)
GROUP_LABELS = {
    "join": "Join Operators",
    "scan": "Scan & Filter Strategies",
    "aggregation": "Aggregation & Ordering Strategies",
    "execution_opt": "Low-Level Execution Optimizations",
}

STRATEGY_TO_GROUP: dict[str, str] = {}
for k in JOIN_OPERATOR_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "join"
for k in SCAN_FILTER_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "scan"
for k in AGGREGATION_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "aggregation"
for k in EXECUTION_OPT_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "execution_opt"


def _taxonomy_text(taxonomy: dict[str, str]) -> str:
    return "\n".join(f"- **{name}**: {desc}" for name, desc in taxonomy.items())


JOIN_TAXONOMY_TEXT = _taxonomy_text(JOIN_OPERATOR_TAXONOMY)
SCAN_TAXONOMY_TEXT = _taxonomy_text(SCAN_FILTER_TAXONOMY)
AGG_TAXONOMY_TEXT = _taxonomy_text(AGGREGATION_TAXONOMY)
OPT_TAXONOMY_TEXT = _taxonomy_text(EXECUTION_OPT_TAXONOMY)


# -- request helpers -----------------------------------------------------------


def make_request(system: str, user: str, max_tokens: int = 100000) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "developer", "content": system},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max_tokens,
    }


def extract_text(response: dict) -> str:
    return response["choices"][0]["message"]["content"]


# -- file reading helpers ------------------------------------------------------


def read_output_file(name: str) -> str:
    path = OUTPUT_DIR / name
    if not path.exists():
        return f"[File not found: {name}]"
    return path.read_text(errors="replace")


def read_query_file(q: str) -> tuple[str, str]:
    for name in [f"query_q{q}.cpp", f"query{q}.cpp"]:
        path = OUTPUT_DIR / name
        if path.exists():
            return name, path.read_text(errors="replace")
    name = f"query_q{q}.cpp"
    return name, f"[File not found: {name}]"


# -- prompts -------------------------------------------------------------------
SYSTEM_EXPERT = """\
You are an expert in OLAP database systems, query execution engines, and \
performance engineering. You analyze C++ source code for a custom bespoke \
in-memory columnar engine built for the TPC-H benchmark.

Classify which known query execution strategies are applied. \
Be precise and evidence-based: cite concrete code constructs (type names, \
variable names, function calls, loop patterns) that justify each classification. \
Only classify a strategy as applied if there is clear evidence in the code.

Always respond with a valid JSON object -- no markdown, no prose outside the JSON.
"""


def build_query_prompt(q: str, code: str, hpp: str) -> str:
    return f"""\
Below is the implementation of TPC-H Query Q{q} for a bespoke columnar engine,
together with the storage structure definitions it operates on.

=== db_loader.hpp (data structure definitions) ===
{hpp}

=== query_q{q}.cpp ===
{code}

### Task
Classify the query execution strategies used by Q{q}. Analyze how joins are
implemented, how scans and filters are performed, what aggregation technique is
used, how final ordering is produced, and what low-level execution optimizations
are applied.

Use ONLY the strategy names from the four taxonomy groups that follow.

**Group A -- Join Operators** (how tables are combined):
{JOIN_TAXONOMY_TEXT}

**Group B -- Scan & Filter Strategies** (how rows are accessed and filtered):
{SCAN_TAXONOMY_TEXT}

**Group C -- Aggregation & Ordering Strategies** (how groups are computed \
and how the final result is ordered):
{AGG_TAXONOMY_TEXT}

**Group D -- Low-Level Execution Optimizations** (micro-architectural tricks):
{OPT_TAXONOMY_TEXT}

### Output format (JSON object)
{{
  "query": "Q{q}",
  "strategies_used": ["<strategy_name>", ...],
  "how_each_is_used": {{
    "<strategy_name>": "<one sentence describing exactly how Q{q} employs it>"
  }},
  "join_order": "<brief description of the join order / table access order>",
  "summary": "<2-3 sentence narrative for a paper explaining how the execution \
plan was tailored to Q{q}'s access pattern and data characteristics>"
}}
"""


# -- classification ------------------------------------------------------------


def classify_all_queries(query_nums: list[str]) -> list[dict]:
    hpp = read_output_file("db_loader.hpp")
    requests = []
    for q in query_nums:
        _, code = read_query_file(q)
        requests.append(
            make_request(
                SYSTEM_EXPERT, build_query_prompt(q, code, hpp), max_tokens=30000
            )
        )

    token_counts = llms.count_tokens(requests, silent=True)
    total_tokens = sum(token_counts) if isinstance(token_counts, list) else token_counts
    print(
        f"Per-query classification: {len(requests)} queries, "
        f"~{total_tokens:,} total input tokens"
    )

    responses = llms.execute(requests, budget=2.0, silent=False)
    total_cost = sum(llms.cost(responses, silent=True))  # type: ignore
    print(f"  -> total cost ${total_cost:.4f}")

    results = []
    for q, resp in zip(query_nums, responses):
        text = extract_text(resp)
        try:
            results.append(json.loads(text))
        except json.JSONDecodeError as e:
            print(f"  [WARN] Q{q} JSON parse error: {e}")
            results.append(
                {
                    "query": f"Q{q}",
                    "strategies_used": [],
                    "how_each_is_used": {},
                    "join_order": "",
                    "summary": text[:300],
                }
            )
    return results


# -- output / reporting --------------------------------------------------------

DISPLAY_NAMES = STRATEGY_DISPLAY_NAMES


BAR_WIDTH = 30


def _display_name(strategy: str) -> str:
    entry = DISPLAY_NAMES.get(strategy, strategy)
    return entry[0] if isinstance(entry, tuple) else entry


def _bar(n: int, total: int) -> str:
    if total == 0:
        return ""
    filled = round(BAR_WIDTH * n / total)
    return "\u2588" * filled + "\u2591" * (BAR_WIDTH - filled)


def print_query_summaries(queries: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("QUERY EXECUTION STRATEGY SUMMARIES")
    print("=" * 80)
    for qr in sorted(queries, key=lambda x: x["query"][1:]):
        print(f"\n### {qr['query']}")

        # Group strategies by category
        for group_key, group_label in GROUP_LABELS.items():
            strats = [
                s
                for s in qr.get("strategies_used", [])
                if STRATEGY_TO_GROUP.get(s) == group_key
            ]
            if strats:
                print(f"  {group_label}: {', '.join(strats)}")

        join_order = qr.get("join_order", "")
        if join_order:
            print(f"  Join order: {join_order}")

        summary = qr.get("summary", "")
        if summary:
            words = summary.split()
            line = "  "
            for w in words:
                if len(line) + len(w) + 1 > 76:
                    print(line)
                    line = "  " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)


def _print_strategy_subtable(
    strats: list[str],
    counts: dict[str, int],
    total: int,
    count_label: str,
) -> None:
    W = 40
    print(f"\n  {'Strategy':<{W}} {count_label:>9}  {'':>{BAR_WIDTH}}")
    print(f"  {'-' * W} {'-' * 9}  {'-' * BAR_WIDTH}")
    for s in sorted(strats, key=lambda s: -counts.get(s, 0)):
        label = _display_name(s)
        n = counts.get(s, 0)
        pct = f"{100 * n / total:.0f}%" if total > 0 else "\u2014"
        bar = _bar(n, total)
        print(f"  {label:<{W}} {n:>5} {pct:>3}  {bar}")


def print_heatmap(queries: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("EXECUTION STRATEGY USAGE SUMMARY  (for paper)")
    print("=" * 80)

    query_counts: dict[str, int] = {}
    for qr in queries:
        for s in qr.get("strategies_used", []):
            query_counts[s] = query_counts.get(s, 0) + 1

    total_queries = len(queries)
    seen = set(query_counts)

    for group_key, group_label in GROUP_LABELS.items():
        group_taxonomy = {
            "join": JOIN_OPERATOR_TAXONOMY,
            "scan": SCAN_FILTER_TAXONOMY,
            "aggregation": AGGREGATION_TAXONOMY,
            "execution_opt": EXECUTION_OPT_TAXONOMY,
        }[group_key]
        group_strats = [s for s in group_taxonomy if s in seen]
        if group_strats:
            print(f"\n--- {group_label} ---")
            _print_strategy_subtable(
                group_strats,
                query_counts,
                total_queries,
                f"#Q/{total_queries}",
            )

    uncategorised = seen - set(STRATEGY_TO_GROUP.keys())
    if uncategorised:
        print("\n--- Uncategorised ---")
        _print_strategy_subtable(
            list(uncategorised),
            query_counts,
            total_queries,
            f"#Q/{total_queries}",
        )


def _draw_panel(
    ax,
    strats: list[str],
    counts: dict[str, int],
    total: int,
    color: str,
    title: str,
    xlabel: str,
) -> None:
    import matplotlib.ticker as mticker
    import numpy as np

    sorted_strats = sorted(strats, key=lambda s: counts.get(s, 0))
    labels = [_display_name(s) for s in sorted_strats]
    values = [counts.get(s, 0) for s in sorted_strats]
    pcts = [100.0 * v / total if total else 0.0 for v in values]

    y = np.arange(len(labels))
    bars = ax.barh(
        y,
        pcts,
        color=color,
        alpha=0.85,
        edgecolor="white",
        linewidth=0.6,
        height=0.6,
    )

    for bar, v, pct in zip(bars, values, pcts):
        w = bar.get_width()
        text = f"{v}/{total}  ({pct:.0f}%)"
        if w > 20:
            ax.text(
                w - 1.0,
                bar.get_y() + bar.get_height() / 2,
                text,
                va="center",
                ha="right",
                fontsize=7.5,
                color="white",
                fontweight="bold",
            )
        else:
            ax.text(
                w + 0.8,
                bar.get_y() + bar.get_height() / 2,
                text,
                va="center",
                ha="left",
                fontsize=7.5,
                color="#333333",
            )

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.5)
    ax.set_xlim(0, 115)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax.set_xlabel(xlabel, fontsize=9, color="#333333")
    ax.set_title(title, fontsize=10, fontweight="bold", pad=6, loc="left")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_alpha(0.3)
    ax.spines["bottom"].set_alpha(0.3)
    ax.xaxis.grid(True, linestyle="--", linewidth=0.4, alpha=0.4, color="#888888")
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", which="both", length=0)


def save_results(queries: list[dict], benchmark: str) -> None:
    import matplotlib.pyplot as plt

    # prefix output files with date/time
    file_prefix = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{benchmark}"

    out_dir = THIS_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{file_prefix}_execution_strategies.json").write_text(
        json.dumps(queries, indent=2)
    )
    print(f"\nSaved JSON to {out_dir}/")

    # -- build counts ----------------------------------------------------------
    query_counts: dict[str, int] = {}
    for qr in queries:
        for s in qr.get("strategies_used", []):
            query_counts[s] = query_counts.get(s, 0) + 1

    total_queries = len(queries)
    seen = set(query_counts)

    group_data: list[tuple[str, str, dict[str, str], str]] = [
        ("join", "Join Operators", JOIN_OPERATOR_TAXONOMY, "#2E86AB"),
        ("scan", "Scan & Filter Strategies", SCAN_FILTER_TAXONOMY, "#A23B72"),
        (
            "aggregation",
            "Aggregation & Ordering Strategies",
            AGGREGATION_TAXONOMY,
            "#5C8A3C",
        ),
        (
            "execution_opt",
            "Low-Level Execution Optimizations",
            EXECUTION_OPT_TAXONOMY,
            "#E8871E",
        ),
    ]

    panels = []
    for _, label, taxonomy, color in group_data:
        strats = [s for s in taxonomy if s in seen]
        if strats:
            panels.append((label, strats, color))

    if not panels:
        print("No strategies found, skipping figure.")
        return

    # -- layout ----------------------------------------------------------------
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 9,
            "axes.linewidth": 1.0,
            "figure.facecolor": "white",
        }
    )

    n_rows = [len(p[1]) for p in panels]
    row_h, pad_h = 0.45, 1.5
    fig_h = sum(n * row_h + pad_h for n in n_rows) + 0.5

    fig, axes = plt.subplots(
        len(panels),
        1,
        figsize=(7.0, fig_h),
        gridspec_kw={"height_ratios": n_rows, "hspace": 0.6},
    )
    if len(panels) == 1:
        axes = [axes]

    for ax, (label, strats, color) in zip(axes, panels):
        _draw_panel(
            ax,
            strats,
            query_counts,
            total_queries,
            color=color,
            title=label,
            xlabel=f"Fraction of queries  (out of {total_queries} total)",
        )

    fig.suptitle(
        "Bespoke Query Execution -- Strategy Coverage",
        fontsize=11,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()

    pdf_path = out_dir / f"{file_prefix}_execution_strategy_usage.pdf"
    fig.savefig(pdf_path, bbox_inches="tight", dpi=300)
    png_path = out_dir / f"{file_prefix}_execution_strategy_usage.png"
    fig.savefig(png_path, bbox_inches="tight", dpi=180)
    plt.close(fig)
    print(f"Saved figure -> {pdf_path}")
    print(f"Saved figure -> {png_path}")

    # -- write raw statistics to CSV -------------------------------------------
    import pandas as pd

    rows = []
    for _, label, taxonomy, _ in group_data:
        for strategy in taxonomy:
            count = query_counts.get(strategy, 0)
            rows.append(
                {
                    "group": label,
                    "strategy": strategy,
                    "count": count,
                    "fraction": count / total_queries if total_queries else 0.0,
                    "total_queries": total_queries,
                }
            )
    csv_path = out_dir / f"{file_prefix}_execution_strategy_usage.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Saved CSV    -> {csv_path}")


# -- main ----------------------------------------------------------------------

#  python plots/classify_bespoke_execution/classify_execution.py --wandb_id 5qumtphx --benchmark ceb


def main() -> None:

    parser = argparse.ArgumentParser(
        description="Classify query execution strategies in bespoke OLAP engine"
    )
    parser.add_argument(
        "--wandb_id",
        type=str,
        default=None,
        help="Weights & Biases run ID for logging (optional)",
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default="ceb",
        help="Benchmark name for logging (default: ceb)",
    )

    args = parser.parse_args()

    wandb_id = args.wandb_id
    benchmark = args.benchmark

    statistics, config, hist = wandb_retrieve_metrics_for_run(
        benchmark=benchmark, run_id=wandb_id, output_hist=True
    )
    snapshot = statistics["code/snapshot_hash"]

    # load snapshot
    snapshotter = GitSnapshotter(
        cache_repo="git://c01/bespoke_cache.git",
        working_dir=THIS_DIR.parent.parent.parent / "output",
        extra_gitignore=[],
    )
    snapshotter.fetch_snapshots()

    print(f"Restoring snapshot: {snapshot}")
    snapshotter.restore(snapshot)

    # load .env file
    load_dotenv(THIS_DIR / ".." / ".." / ".env")

    available_queries: List[str] = sorted(
        p.stem.replace("query_q", "").replace("query", "")
        for p in [
            *OUTPUT_DIR.glob("query_q*.cpp"),
            *OUTPUT_DIR.glob("query[0-9a-z]*.cpp"),
        ]
    )
    print(
        f"Found {len(available_queries)} query files: "
        f"Q{available_queries[0]}--Q{available_queries[-1]}"
    )
    print(f"Using model: {MODEL}")
    print(f"Cache: {llms.CACHE_PATH}")

    queries = classify_all_queries(available_queries)

    print_query_summaries(queries)
    print_heatmap(queries)
    save_results(queries, benchmark)


if __name__ == "__main__":
    main()
