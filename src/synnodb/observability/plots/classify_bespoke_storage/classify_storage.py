"""
Storage Strategy Classifier for Bespoke OLAP Engine
====================================================
Reads the generated C++ code in output/ and asks an LLM to classify what
storage optimization strategies were applied, both per-column and per-query.

Uses llms.py (same directory) for LLM calls and caching.

Run from the repo root:
  python plots/classify_bespoke_storage/classify_storage.py
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ── path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[3]
THIS_DIR = Path(__file__).parent

sys.path.insert(0, str(THIS_DIR))  # for llms.py
sys.path.append(str(REPO_ROOT))

import synnodb.observability.plots.classify_bespoke_storage.llms as llms  # noqa: E402
from synnodb.observability.plots.classify_bespoke_execution.strategy_display_names import (  # noqa: E402
    STRATEGY_DISPLAY_NAMES,
)
from synnodb.observability.plots.classify_bespoke_storage.llms import (  # noqa: E402
    cost,
    count_tokens,
    execute,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.utils.logging_and_reporting.wandb_api_helper import wandb_retrieve_metrics_for_run

# point cache to this directory so it stays with the script
llms.CACHE_PATH = THIS_DIR / "llm_cache"

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = REPO_ROOT / "output"
MODEL = "gpt-5-2025-08-07"


# ── strategy taxonomy ─────────────────────────────────────────────────────────
#
# Canonical strategy names with descriptions. The LLM must use exactly these
# names so results can be aggregated into a table.
#
STRATEGY_TAXONOMY = {
    "dictionary_encoding": (
        "Low-cardinality string column encoded as integer codes "
        "(DictionaryColumn: codes vector + dictionary vector). "
        "Saves memory and enables fast equality comparison."
    ),
    "compact_int16_date": (
        "Date stored as 16-bit day offset from a base date (1992-01-01). "
        "Halves memory vs int32; all TPC-H dates fit in int16 range."
    ),
    "scaled_integer": (
        "Decimal/float stored as scaled int32 or int16 "
        "(e.g. price × 100, discount × 10000). "
        "Avoids floating-point in inner loops; enables integer arithmetic."
    ),
    "string_arena": (
        "Variable-length strings stored in a flat byte buffer "
        "with a uint32 offset array (StringColumn). "
        "Cache-friendly sequential access; cheap iteration."
    ),
    "temporal_sharding_zonemap": (
        "Data partitioned by year/month into shards, each with min/max "
        "metadata (zone maps) for dates and discount. "
        "Skips entire shards that cannot satisfy a date or discount predicate."
    ),
    "join_range_directory": (
        "Sparse range index mapping a join key (e.g. orderkey) to [start, end) row "
        "offsets in the sorted column. O(1) range lookup without a hash table."
    ),
    "join_hash_index": (
        "Hash map or dense array mapping a key to a row index for O(1) lookup "
        "(e.g. orderkey_to_row, nationkey_to_row, name_to_key)."
    ),
    "precomputed_predicate": (
        "Boolean flag (uint8_t) precomputed at build time to cache a derived "
        "condition (is_late, fast_ship, has_orders, order_has_late, "
        "order_has_other_supplier). Eliminates repeated multi-column checks at query time."
    ),
    "sorted_physical_layout": (
        "Rows physically ordered by a key column, enabling range scans and "
        "sequential access patterns (orders by orderdate, customer by nationkey, "
        "partsupp by partkey then suppkey)."
    ),
    "SoA_columnar_layout": (
        "Each column stored as its own flat vector (struct of arrays). "
        "Selective column access; only read columns touched by a query."
    ),
    "micro_AoS_pack": (
        "Hot columns (extendedprice, discount, tax, quantity) packed into a "
        "micro-struct (PriceBlock) for cache locality during revenue aggregation."
    ),
    "precomputed_lookup_table": (
        "Load-time lookup table replacing repeated arithmetic "
        "(e.g. discount_factors[], tax_factors[] arrays indexed by integer discount code)."
    ),
    "shipmode_indexed_shard": (
        "Receipt-date shards sub-indexed by shipmode dictionary code: "
        "row_indices_by_shipmode[shipmode_code] lists rows per shard. "
        "Allows Q12 to simultaneously filter on date range and shipmode."
    ),
    "per_order_aggregated_flag": (
        "Order-level summary flags aggregated once across all line items "
        "(order_has_late, order_has_other_supplier) to short-circuit per-order "
        "checks without re-scanning all line items (used by Q21)."
    ),
    "precomputed_derived_column": (
        "A column that does not exist in the original TPC-H schema, materialized "
        "at build time from an expression over other columns "
        "(e.g. discounted_price = extendedprice × (1 − discount)). "
        "Trades storage for compute by eliminating repeated arithmetic in hot loops."
    ),
    "bit_packed_record": (
        "Multiple narrow fields packed into a single 16- or 32-bit word, decoded "
        "at read time by shift + mask. Distinct from micro_AoS_pack which is "
        "byte-aligned struct packing; here sub-byte fields share one word. "
        "(e.g. Q12 16-bit filter word [shipmode<<13 | date_ok<<12 | rdate], "
        "Q13 ck_len [custkey<<8 | len], Q1 packed disc_tax byte [disc<<4 | tax])"
    ),
    "word_presence_bitmask": (
        "Per-row N-bit bitmask indicating which words from a fixed vocabulary "
        "appear in a string column. Enables O(1) 'does word X appear in row i?' "
        "test via single bit AND instead of std::string::find(). "
        "(e.g. p_name_word_mask_lo / p_name_word_mask_hi -- 128-bit per-row "
        "bitmask of p_name word vocabulary)"
    ),
    "compact_bitset_membership": (
        "Per-key 1-bit-per-element bitset encoding a boolean property "
        "(EXISTS, has_X, in_set). Distinct from precomputed_predicate which is "
        "a byte array; the bitset is 8x smaller and often L3-resident, turning "
        "random LLC misses into L3 hits. "
        "(e.g. ok_any_clr_bits, ck_has_order_bits, ok_high_prio_bits, "
        "o_row_clr_bits)"
    ),
    "inline_csr_payload": (
        "Value columns physically stored in CSR adjacency-list order alongside "
        "the row-id array, eliminating the row_id -> column[row] indirection at "
        "probe time. Trades column duplication for a single sequential stream "
        "in the probe hot loop. "
        "(e.g. ok_suppkey, ok_extendedprice, pk_quantity, pk_q9_rows, sk_payload "
        "as arrays parallel to ok_row_ids / pk_row_ids / sk_row_ids)"
    ),
    "prejoined_column": (
        "A column on the probe side whose value has been pre-resolved at load "
        "time by walking a foreign-key chain across multiple tables. Eliminates "
        "multi-hop joins from the query hot path. "
        "(e.g. pk_order_year_idx, pk_ps_supplycost, pk_nk pre-join "
        "lineitem -> orders / partsupp / supplier / nation; Q9 hot loop reads "
        "only one struct instead of probing four tables)"
    ),
    "cached_index_range": (
        "Pre-resolved (begin, end) CSR range pairs cached alongside an outer "
        "index entry, so the hot loop pays no random-access index lookup at "
        "probe time. "
        "(e.g. bc_part_li_begin / bc_part_li_end cache lineitem "
        "pk_row_offsets[partkey] and [partkey+1] for each entry in the brand-"
        "container index)"
    ),
    "compact_nonempty_key_list": (
        "Compact list of (key, csr-start, csr-end) triples for the NON-EMPTY "
        "subset of a key domain, avoiding traversal of the sparse "
        "N-million-slot CSR offset array when most keys have no rows. "
        "(e.g. ok_nz_list / ok_nz_beg / ok_nz_end / ok_nz_cnt for orderkeys "
        "with >=1 lineitem -- 30M entries vs 120M sparse slots at SF20)"
    ),
}

# Strategies about how a column's data is physically encoded/laid out.
# Meaningful metric: how many columns carry this encoding.
ENCODING_STRATEGIES: set[str] = {
    "dictionary_encoding",
    "compact_int16_date",
    "scaled_integer",
    "string_arena",
    "SoA_columnar_layout",
    "sorted_physical_layout",
    "bit_packed_record",
    "word_presence_bitmask",
}

# Strategies that are cross-column indexes or precomputed structures built to
# serve queries. Meaningful metric: how many queries exploit the structure.
QUERY_SUPPORT_STRATEGIES: set[str] = {
    "temporal_sharding_zonemap",
    "join_range_directory",
    "join_hash_index",
    "precomputed_predicate",
    "micro_AoS_pack",
    "precomputed_lookup_table",
    "shipmode_indexed_shard",
    "per_order_aggregated_flag",
    "precomputed_derived_column",
    "compact_bitset_membership",
    "inline_csr_payload",
    "prejoined_column",
    "cached_index_range",
    "compact_nonempty_key_list",
}


def _taxonomy_text(keys: set[str]) -> str:
    return "\n".join(
        f"- **{name}**: {desc}"
        for name, desc in STRATEGY_TAXONOMY.items()
        if name in keys
    )


ENCODING_TAXONOMY_TEXT = _taxonomy_text(ENCODING_STRATEGIES)
QUERY_SUPPORT_TAXONOMY_TEXT = _taxonomy_text(QUERY_SUPPORT_STRATEGIES)


# ── request helpers ───────────────────────────────────────────────────────────


def make_request(system: str, user: str, max_tokens: int = 100000) -> dict:
    # gpt-5 family: use "developer" role and max_completion_tokens (matches sample_request.py)
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


# ── file reading helpers ───────────────────────────────────────────────────────


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


# ── prompts ───────────────────────────────────────────────────────────────────

SYSTEM_EXPERT = """\
You are an expert in OLAP database systems, columnar storage layouts, and \
performance engineering. You analyze C++ source code for a custom bespoke \
in-memory columnar engine built for the TPC-H benchmark.

Classify which known storage optimization strategies are applied. \
Be precise and evidence-based: cite concrete code constructs (type names, \
variable names, function calls) that justify each classification. \
Only classify a strategy as applied if there is clear evidence in the code.

Always respond with a valid JSON object — no markdown, no prose outside the JSON.
"""


def build_column_prompt(hpp: str, cpp: str, storage_plan: str) -> str:
    return f"""\
Below are three source files for a bespoke OLAP engine targeting TPC-H.

=== storage_plan.txt (design intent) ===
{storage_plan}

=== db_loader.hpp (data structure definitions) ===
{hpp}

=== db_loader.cpp (storage construction logic) ===
{cpp}

### Task
Classify the storage optimization strategies applied to each entry below.
Use ONLY the strategy names from the two taxonomy groups that follow.

**Group A — Column Encoding Strategies** (how a column's data is physically \
represented). Apply these to individual column entries (one row per column).
{ENCODING_TAXONOMY_TEXT}

**Group B — Query-Support Structures** (cross-column indexes, precomputed data, \
or access-pattern-specific layouts built to serve queries). Apply these to \
cross-column structure entries rather than to individual columns.
{QUERY_SUPPORT_TAXONOMY_TEXT}

### Output format (JSON object)
{{
  "columns": [
    {{
      "table": "<TABLE_NAME>",
      "column": "<column_name or structure_name>",
      "strategies": ["<strategy_name>", ...],
      "evidence": "<one sentence citing the C++ construct that proves it>",
      "notes": "<optional: nuance or condition>"
    }},
    ...
  ]
}}

Include ALL columns from ALL tables (lineitem, orders, customer, part, partsupp,
supplier, nation, region) plus cross-column structures (shards, order_directory,
precomputed flags). For structures that don't map to a single column, use
column="<structure_name>" (e.g. "shards", "order_directory", "order_has_late").
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
Identify which storage strategies from the taxonomy below are *actively exploited*
at runtime by Q{q} (i.e. the query directly benefits from the strategy — not just
that the strategy exists in storage but is unused here).

### Strategy Taxonomy

**Group A — Column Encoding Strategies** (exploited by reading the encoded \
column representation — e.g. integer codes, scaled values, offset arrays):
{ENCODING_TAXONOMY_TEXT}

**Group B — Query-Support Structures** (exploited by traversing an index, \
skipping a shard, or reading a precomputed flag/table):
{QUERY_SUPPORT_TAXONOMY_TEXT}

### Output format (JSON object)
{{
  "query": "Q{q}",
  "strategies_used": ["<strategy_name>", ...],
  "how_each_is_used": {{
    "<strategy_name>": "<one sentence describing exactly how Q{q} exploits it>"
  }},
  "summary": "<2-3 sentence narrative for a paper explaining how the storage layout was tailored to Q{q}'s access pattern>"
}}
"""


# ── classification ────────────────────────────────────────────────────────────


def classify_columns() -> list[dict]:
    hpp = read_output_file("db_loader.hpp")
    cpp = read_output_file("db_loader.cpp")
    storage_plan = read_output_file("storage_plan.txt")

    req = make_request(
        SYSTEM_EXPERT, build_column_prompt(hpp, cpp, storage_plan), max_tokens=32000
    )

    tokens = count_tokens(req, silent=True)
    print(f"Column classification request: ~{tokens:,} input tokens")

    resp = execute(req, budget=2.0, silent=False)
    c = cost(resp, silent=True)
    print(f"  → cost ${c:.4f}")  # cost() returns float for single dict response

    assert isinstance(resp, dict), (
        "Expected single response dict for column classification"
    )
    text = extract_text(resp)
    try:
        return json.loads(text).get("columns", [])
    except json.JSONDecodeError as e:
        print(f"  [WARN] JSON parse error: {e}\n  Raw: {text[:400]}")
        return []


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

    token_counts = count_tokens(requests, silent=True)
    total_tokens = sum(token_counts) if isinstance(token_counts, list) else token_counts
    print(
        f"Per-query classification: {len(requests)} queries, ~{total_tokens:,} total input tokens"
    )

    responses = execute(requests, budget=2.0, silent=False)  # list[resp] for list input
    total_cost = sum(cost(responses, silent=True))  # type: ignore # list[float] → sum
    print(f"  → total cost ${total_cost:.4f}")

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
                    "summary": text[:300],
                }
            )
    return results


# ── output / reporting ────────────────────────────────────────────────────────


def print_column_table(columns: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("STORAGE STRATEGY CLASSIFICATION — PER COLUMN")
    print("=" * 80)

    by_table: dict[str, list[dict]] = {}
    for col in columns:
        by_table.setdefault(col.get("table", "?"), []).append(col)

    for table, cols in sorted(by_table.items()):
        print(f"\n### {table}")
        print(f"  {'Column':<38} Strategies")
        print(f"  {'-' * 38} {'-' * 38}")
        for col in cols:
            strats = ", ".join(col.get("strategies", []))
            print(f"  {col.get('column', '?'):<38} {strats}")


def print_query_summaries(queries: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("QUERY STRATEGY SUMMARIES")
    print("=" * 80)
    for qr in sorted(queries, key=lambda x: x["query"][1:]):
        print(f"\n### {qr['query']}")
        strats = ", ".join(qr.get("strategies_used", [])) or "(none)"
        print(f"  Strategies: {strats}")
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


# Human-readable labels for the "for paper" summary table.
# Internal strategy keys stay unchanged (LLM / JSON use them); only display differs.
DISPLAY_NAMES = STRATEGY_DISPLAY_NAMES


BAR_WIDTH = 30  # max chars for the visual bar


def _display_name(strategy: str) -> str:
    entry = DISPLAY_NAMES.get(strategy, strategy)
    return entry[0] if isinstance(entry, tuple) else entry


def _bar(n: int, total: int) -> str:
    if total == 0:
        return ""
    filled = round(BAR_WIDTH * n / total)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _print_strategy_subtable(
    strats: list[str],
    col_counts: dict[str, int],
    query_counts: dict[str, int],
    count_key: str,  # "col" or "query"
    count_label: str,  # e.g. "#Columns" or "#Queries"
    total: int = 0,
) -> None:
    W = 40
    counts = col_counts if count_key == "col" else query_counts
    print(f"\n  {'Strategy':<{W}} {count_label:>9}  {'':>{BAR_WIDTH}}")
    print(f"  {'-' * W} {'-' * 9}  {'-' * BAR_WIDTH}")
    for s in sorted(strats, key=lambda s: -counts.get(s, 0)):
        label = _display_name(s)
        n = counts.get(s, 0)
        pct = f"{100 * n / total:.0f}%" if total > 0 else "—"
        bar = _bar(n, total)
        print(f"  {label:<{W}} {n:>5} {pct:>3}  {bar}")


def print_heatmap(columns: list[dict], queries: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("STRATEGY USAGE SUMMARY  (for paper)")
    print("=" * 80)

    col_counts: dict[str, int] = {}
    for col in columns:
        for s in col.get("strategies", []):
            col_counts[s] = col_counts.get(s, 0) + 1

    query_counts: dict[str, int] = {}
    for qr in queries:
        for s in qr.get("strategies_used", []):
            query_counts[s] = query_counts.get(s, 0) + 1

    seen = set(col_counts) | set(query_counts)
    total_columns = len(columns)
    total_queries = len(queries)

    print("\n--- Column Encoding Strategies (how data is physically represented) ---")
    enc = [s for s in ENCODING_STRATEGIES if s in seen]
    _print_strategy_subtable(
        enc, col_counts, query_counts, "col", f"#Cols/{total_columns}", total_columns
    )

    print("\n--- Query-Support Structures (indexes / precomputed data for queries) ---")
    qss = [s for s in QUERY_SUPPORT_STRATEGIES if s in seen]
    _print_strategy_subtable(
        qss, col_counts, query_counts, "query", f"#Q/{total_queries}", total_queries
    )

    # Catch anything not yet categorised
    uncategorised = seen - ENCODING_STRATEGIES - QUERY_SUPPORT_STRATEGIES
    if uncategorised:
        print("\n--- Uncategorised ---")
        _print_strategy_subtable(
            list(uncategorised),
            col_counts,
            query_counts,
            "col",
            f"#Cols/{total_columns}",
            total_columns,
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

    sorted_strats = sorted(
        strats, key=lambda s: counts.get(s, 0)
    )  # low → high (top = highest)
    labels = [_display_name(s) for s in sorted_strats]
    values = [counts.get(s, 0) for s in sorted_strats]
    pcts = [100.0 * v / total if total else 0.0 for v in values]

    y = np.arange(len(labels))
    bars = ax.barh(
        y, pcts, color=color, alpha=0.85, edgecolor="white", linewidth=0.6, height=0.6
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


def save_results(columns: list[dict], queries: list[dict], benchmark: str) -> None:
    import matplotlib.pyplot as plt
    import numpy as np  # noqa: F401  (imported inside _draw_panel too)

    file_prefix = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{benchmark}"

    out_dir = THIS_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{file_prefix}_column_strategies.json").write_text(
        json.dumps(columns, indent=2)
    )
    (out_dir / f"{file_prefix}_query_strategies.json").write_text(
        json.dumps(queries, indent=2)
    )
    print(f"\nSaved JSON to {out_dir}/")

    # ── build counts ──────────────────────────────────────────────────────────
    col_counts: dict[str, int] = {}
    for col in columns:
        for s in col.get("strategies", []):
            col_counts[s] = col_counts.get(s, 0) + 1

    query_counts: dict[str, int] = {}
    for qr in queries:
        for s in qr.get("strategies_used", []):
            query_counts[s] = query_counts.get(s, 0) + 1

    seen = set(col_counts) | set(query_counts)
    total_columns = len(columns)
    total_queries = len(queries)

    enc_strats = [s for s in ENCODING_STRATEGIES if s in seen]
    qss_strats = [s for s in QUERY_SUPPORT_STRATEGIES if s in seen]

    # ── layout ────────────────────────────────────────────────────────────────
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 9,
            "axes.linewidth": 1.0,
            "figure.facecolor": "white",
        }
    )
    n_enc, n_qss = len(enc_strats), len(qss_strats)
    row_h, pad_h = 0.45, 1.5
    fig_h = n_enc * row_h + pad_h + n_qss * row_h + pad_h + 0.5

    fig, (ax_enc, ax_qss) = plt.subplots(
        2,
        1,
        figsize=(6.5, fig_h),
        gridspec_kw={"height_ratios": [n_enc, n_qss], "hspace": 0.6},
    )

    _draw_panel(
        ax_enc,
        enc_strats,
        col_counts,
        total_columns,
        color="#2E86AB",
        title="Column Encoding Strategies",
        xlabel=f"Fraction of columns  (out of {total_columns} total)",
    )
    _draw_panel(
        ax_qss,
        qss_strats,
        query_counts,
        total_queries,
        color="#5C8A3C",
        title="Query-Support Structures",
        xlabel=f"Fraction of queries  (out of {total_queries} total)",
    )

    fig.suptitle(
        "Bespoke Storage Optimization — Strategy Coverage",
        fontsize=11,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()

    pdf_path = out_dir / f"{file_prefix}_strategy_usage.pdf"
    fig.savefig(pdf_path, bbox_inches="tight", dpi=300)
    png_path = out_dir / f"{file_prefix}_strategy_usage.png"
    fig.savefig(png_path, bbox_inches="tight", dpi=180)
    plt.close(fig)
    print(f"Saved figure → {pdf_path}")
    print(f"Saved figure → {png_path}")

    # ── write raw statistics to CSV ───────────────────────────────────────────
    import pandas as pd

    rows = []
    for strategy in ENCODING_STRATEGIES:
        count = col_counts.get(strategy, 0)
        rows.append(
            {
                "group": "Column Encoding Strategies",
                "strategy": strategy,
                "count": count,
                "fraction": count / total_columns if total_columns else 0.0,
                "total": total_columns,
                "count_unit": "columns",
            }
        )
    for strategy in QUERY_SUPPORT_STRATEGIES:
        count = query_counts.get(strategy, 0)
        rows.append(
            {
                "group": "Query-Support Structures",
                "strategy": strategy,
                "count": count,
                "fraction": count / total_queries if total_queries else 0.0,
                "total": total_queries,
                "count_unit": "queries",
            }
        )
    csv_path = out_dir / f"{file_prefix}_strategy_usage.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Saved CSV    → {csv_path}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:

    parser = argparse.ArgumentParser(
        description="Classify storage strategies in bespoke OLAP engine"
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

    snapshotter = GitSnapshotter(
        cache_repo="git://c01/bespoke_cache.git",
        working_dir=REPO_ROOT / "output",
        extra_gitignore=[],
    )
    snapshotter.fetch_snapshots()

    print(f"Restoring snapshot: {snapshot}")
    snapshotter.restore(snapshot)

    # load .env file
    load_dotenv(REPO_ROOT / ".env")

    available_queries = sorted(
        p.stem.replace("query_q", "").replace("query", "")
        for p in [
            *OUTPUT_DIR.glob("query_q*.cpp"),
            *OUTPUT_DIR.glob("query[0-9a-z]*.cpp"),
        ]
    )
    print(
        f"Found {len(available_queries)} query files: Q{available_queries[0]}–Q{available_queries[-1]}"
    )
    print(f"Using model: {MODEL}")
    print(f"Cache: {llms.CACHE_PATH}")

    columns = classify_columns()
    queries = classify_all_queries(available_queries)

    print_column_table(columns)
    print_query_summaries(queries)
    print_heatmap(columns, queries)
    save_results(columns, queries, benchmark)


if __name__ == "__main__":
    main()
