"""
Multi-Threading Strategy Classifier for Bespoke OLAP Engine
============================================================
Reads the generated C++ code in output/ and asks an LLM to classify what
multi-threading strategies were applied per query -- coordination/dispatch,
work distribution, memory/cache discipline, and loader-side parallelism.

Mirrors the structure of classify_execution.py / classify_storage.py.

Run from the repo root:
  python plots/classify_bespoke_multi_threading/classify_multi_threading.py
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.append(str(REPO_ROOT))

from synnodb.observability.logging.wandb_api_helper import (
    wandb_retrieve_metrics_for_run,
)
from synnodb.observability.plots.classify_bespoke_execution.strategy_display_names import (
    STRATEGY_DISPLAY_NAMES,
)
from synnodb.observability.plots.classify_bespoke_storage import llms
from synnodb.synth_framework.git_snapshotter import GitSnapshotter

# -- path setup ----------------------------------------------------------------
THIS_DIR = Path(__file__).parent


# -- config --------------------------------------------------------------------
OUTPUT_DIR = REPO_ROOT / "output"
MODEL = "gpt-5-2025-08-07"


# -- multi-threading strategy taxonomy ----------------------------------------
#
# Canonical strategy names with descriptions. The LLM must use exactly these
# names so results can be aggregated into a table.
#

# ---- Coordination & Dispatch -------------------------------------------------
# Covers both the pool/dispatch infrastructure and per-task synchronization
# patterns -- the two were originally separate but they share a theme: how
# threads coordinate (start, finish, write to shared state).
POOL_DISPATCH_TAXONOMY = {
    "pinned_shared_thread_pool": (
        "A single process-long-lived ThreadPool whose workers are pinned to "
        "specific CPU cores (via CORE_IDS / pin_process_to_cpu). Avoids per-"
        "query pool construction and prevents OS thread migration. "
        "(e.g. static ThreadPool& pool = get_query_pool();)"
    ),
    "hybrid_spin_then_sleep": (
        "Workers wait for new work by spinning briefly (_mm_pause / yield), "
        "then fall back to condition_variable::wait once spin_limit is reached. "
        "Provides low-latency dispatch for back-to-back tasks while releasing "
        "the CPU when idle. "
        "(e.g. spin_yield_after, idle_spin_limit knobs in ThreadPool)"
    ),
    "zero_alloc_parallel_dispatch": (
        "parallel_for type-erases the callable into a function pointer plus "
        "stack-borrowed void* context; no heap allocation per dispatch. "
        "(e.g. task_fn / task_ctx pair instead of std::function)"
    ),
    "generation_counter_dispatch": (
        "Workers observe a monotonic 'generation' atomic to detect new work; "
        "joining waits on a 'done_count' atomic. Lock-free dispatch and join. "
        "(e.g. generation.fetch_add + done_count.fetch_add)"
    ),
    "per_thread_accumulator_merge": (
        "Each thread writes to a private accumulator (scalar, array, or "
        "thread-local vector); a sequential merge at the end combines them. "
        "Default pattern: no atomics on the hot path. "
        "(e.g. ThreadAcc local_revenue, then sum across n_threads)"
    ),
    "ownership_partitioning_no_atomics": (
        "Output space is partitioned such that each output cell has exactly "
        "ONE writing thread; the hot loop has no shared writes, no atomics. "
        "Often combined with per-destination sub-buffers."
    ),
    "relaxed_atomic_aggregation": (
        "Shared accumulator updated via std::atomic fetch_add with "
        "memory_order_relaxed (correctness only needs atomicity, not "
        "ordering). Reserved for sparse / rare writes. "
        "(e.g. atomic<int64_t> rev_atomic for sparse custkey scatter)"
    ),
    "conditional_atomic_set": (
        "Set-bit-in-shared-bitset done as: relaxed load -> skip if already "
        "set -> atomic OR otherwise. Eliminates the atomic when the bit is "
        "already set (common case)."
    ),
    "idempotent_byte_write": (
        "Many threads write the same constant value (typically 1) to a shared "
        "byte array; no atomics needed because x86 byte stores are atomic "
        "and the write is idempotent. "
        "(e.g. part_qualifies[pk] = 1 from any thread)"
    ),
}

# ---- Work Distribution -------------------------------------------------------
# Covers both how work is split across threads (partitioning) and how data
# is redistributed between phases. Both are about arranging WHICH thread sees
# WHICH portion of the data.
WORK_PARTITIONING_TAXONOMY = {
    "row_range_partition": (
        "Each thread owns a contiguous slice of input rows: "
        "chunk = (n + nt - 1) / nt; t_beg = tid * chunk. Default partition "
        "for sequential scans over a column or table."
    ),
    "key_range_partition": (
        "Each thread owns a contiguous range of join/group KEYS (rather than "
        "rows) and iterates the CSR adjacency lists for keys in that range. "
        "Natural unit when the access pattern is keyed (CSR-probing). "
        "(e.g. tid iterates [pk_begin, pk_end] over partkey CSR)"
    ),
    "nonempty_key_list_partition": (
        "Each thread owns a slice of a compact list of NON-EMPTY keys (with "
        "their CSR bounds), rather than scanning the full sparse key space. "
        "(e.g. partition over ok_nz_list[] for orderkeys with >=1 lineitem)"
    ),
    "bitmask_word_partition": (
        "Each thread owns a disjoint range of 64-bit bitset WORDS; iterates "
        "the bits within. Eliminates contention when the operand is a packed "
        "bitset shared across threads."
    ),
    "morsel_simd_aligned_partition": (
        "Per-thread chunk size is rounded up to a multiple of the SIMD vector "
        "width (e.g. 32 for AVX-512) so each thread's slice starts on an "
        "aligned vector boundary. "
        "(e.g. morsel_size = ((len + n - 1) / n + 31) & ~31)"
    ),
    "prefix_sum_load_balancing": (
        "Per-key row counts are accumulated into a prefix sum; each thread "
        "receives an equal *row* count rather than an equal *key* count, "
        "evening out work when per-key fan-out varies. "
        "(e.g. Q7 supp_prefix sum balances lineitem rows across threads)"
    ),
    "histogram_prefix_scatter": (
        "Classic parallel radix pass: phase 1 per-thread histogram of bucket "
        "counts; sequential prefix-sum -> per-thread output offsets; phase 2 "
        "parallel scatter to the destination buffer. Used for parallel sort "
        "and parallel radix partitioning."
    ),
    "per_destination_subbuffers": (
        "Phase 1: each thread writes into its OWN per-destination bucket "
        "(thread x destination matrix). Phase 2: each thread reads its "
        "ASSIGNED destination bucket. No atomics, no false sharing."
    ),
    "flat_stripe_buffer": (
        "All per-(thread, stripe) cells live in ONE contiguous allocation "
        "addressed by known strides (src * cell_row + tid * cell_cap), "
        "rather than as a std::vector<std::vector<>>. Eliminates heap-"
        "pointer chasing in the merge phase."
    ),
    "magic_constant_stripe_routing": (
        "Replace division by stripe count with a multiplication by a 32/64-"
        "bit magic constant + shift, avoiding 64-bit IDIV in the inner loop. "
        "(e.g. stripe = (pk * magic) >> 32 instead of pk / n_stripes)"
    ),
}

# ---- Memory & Cache Discipline -----------------------------------------------
MEMORY_CACHE_TAXONOMY = {
    "cacheline_padded_accumulators": (
        "Per-thread accumulator structs declared with alignas(64) so each "
        "thread's data sits on its own cache line; prevents false sharing "
        "of the merge phase's atomics or of adjacent thread-local fields."
    ),
    "nontemporal_prefetch_streaming": (
        "Sequential reads that should not pollute L3 use _MM_HINT_NTA "
        "(_mm_prefetch with NTA hint) so the data bypasses the LLC. "
        "(e.g. Q13 prefetch of o_comment_data bytes)"
    ),
    "hugepage_anonymous_mmap": (
        "Very large transient bitsets/buffers allocated via mmap "
        "(MAP_ANONYMOUS) + madvise(MADV_HUGEPAGE) so they sit on 2MB pages, "
        "bounding TLB-shootdown cost on munmap. "
        "(e.g. Q7 ok_nk1/ok_nk2 bitsets)"
    ),
    "parallel_first_touch_init": (
        "Newly allocated arrays are zeroed / first-touched IN PARALLEL by "
        "the same threads that will later read/write them, so NUMA-local "
        "page placement is achieved on first fault."
    ),
    "pooled_reusable_buffers": (
        "Hot scratch buffers (radix stripes, sparse accumulators) are kept "
        "as PROCESS-GLOBAL allocations sized on first use, then reused across "
        "subsequent invocations of the same query. "
        "(e.g. Q11 g_pv_buf, g_flat_data, g_flat_counts)"
    ),
}

# ---- Loader-Side Parallelism -------------------------------------------------
LOADER_PARALLELISM_TAXONOMY = {
    "parallel_column_extraction": (
        "Independent columns from the source format are decoded by separate "
        "std::threads at load time, overlapping I/O and decompression. "
        "(e.g. build_lineitem fans out column extracts via std::vector<std::thread>)"
    ),
    "parallel_radix_sort_permutation": (
        "A single permutation array sorted by a primary key (e.g. orderkey, "
        "shipdate) is built via parallel radix sort; every column is then "
        "scattered through that permutation in parallel. "
        "(e.g. radix_sort_perm_parallel + parallel scatter)"
    ),
    "concurrent_csr_index_build": (
        "Per-key CSR adjacency indexes (orderkey->rows, partkey->rows, "
        "suppkey->rows, ...) are constructed concurrently each on its own "
        "thread, since they touch disjoint output structures."
    ),
    "per_table_build_threads": (
        "Top-level table builds (supplier, customer, part, partsupp, orders, "
        "lineitem) run as one std::thread per table, exploiting independence "
        "between tables in the build phase."
    ),
}


ALL_STRATEGIES: dict[str, str] = {
    **POOL_DISPATCH_TAXONOMY,
    **WORK_PARTITIONING_TAXONOMY,
    **MEMORY_CACHE_TAXONOMY,
    **LOADER_PARALLELISM_TAXONOMY,
}

# Which group each strategy belongs to (for display).
# 'loader' is classified globally (not per-query); the rest are per-query.
GROUP_LABELS = {
    "pool": "Coordination & Dispatch",
    "partition": "Work Distribution",
    "memory": "Memory & Cache Discipline",
    "loader": "Loader-Side Parallelism",
}

STRATEGY_TO_GROUP: dict[str, str] = {}
for k in POOL_DISPATCH_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "pool"
for k in WORK_PARTITIONING_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "partition"
for k in MEMORY_CACHE_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "memory"
for k in LOADER_PARALLELISM_TAXONOMY:
    STRATEGY_TO_GROUP[k] = "loader"

# Per-query strategies (apply to a single query kernel)
PER_QUERY_STRATEGIES: set[str] = (
    set(POOL_DISPATCH_TAXONOMY)
    | set(WORK_PARTITIONING_TAXONOMY)
    | set(MEMORY_CACHE_TAXONOMY)
)

# Loader strategies (apply to db_loader.cpp -- classified globally)
LOADER_STRATEGIES: set[str] = set(LOADER_PARALLELISM_TAXONOMY)


def _taxonomy_text(taxonomy: dict[str, str]) -> str:
    return "\n".join(f"- **{name}**: {desc}" for name, desc in taxonomy.items())


POOL_TAXONOMY_TEXT = _taxonomy_text(POOL_DISPATCH_TAXONOMY)
PART_TAXONOMY_TEXT = _taxonomy_text(WORK_PARTITIONING_TAXONOMY)
MEM_TAXONOMY_TEXT = _taxonomy_text(MEMORY_CACHE_TAXONOMY)
LOADER_TAXONOMY_TEXT = _taxonomy_text(LOADER_PARALLELISM_TAXONOMY)


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
You are an expert in parallel database systems, multi-core scheduling, and \
performance engineering on x86 servers. You analyze C++ source code for a \
custom bespoke in-memory columnar engine built for the TPC-H benchmark.

Classify which known multi-threading strategies are applied. \
Be precise and evidence-based: cite concrete code constructs (type names, \
variable names, function calls, parallel_for usage, atomics, alignas) that \
justify each classification. \
Only classify a strategy as applied if there is clear evidence in the code.

Always respond with a valid JSON object -- no markdown, no prose outside the JSON.
"""


def build_query_prompt(q: str, code: str, thread_pool_hpp: str) -> str:
    return f"""\
Below is the implementation of TPC-H Query Q{q} for a bespoke columnar engine,
together with the thread pool definition it uses.

=== thread_pool.hpp (pool definition) ===
{thread_pool_hpp}

=== query_q{q}.cpp ===
{code}

### Task
Classify the multi-threading strategies used by Q{q}. Analyze how work is
partitioned across threads, how threads coordinate and accumulate, how data is
redistributed between phases, and what memory/cache discipline is applied.

Use ONLY the strategy names from the three taxonomy groups that follow.

**Group A -- Coordination & Dispatch** (pool design + how threads coordinate \
writes to shared state):
{POOL_TAXONOMY_TEXT}

**Group B -- Work Distribution** (how work is partitioned across threads and \
how data is redistributed between phases):
{PART_TAXONOMY_TEXT}

**Group C -- Memory & Cache Discipline** (NUMA, false sharing, prefetch):
{MEM_TAXONOMY_TEXT}

### Output format (JSON object)
{{
  "query": "Q{q}",
  "strategies_used": ["<strategy_name>", ...],
  "how_each_is_used": {{
    "<strategy_name>": "<one sentence describing exactly how Q{q} employs it>"
  }},
  "parallel_for_count": <integer: number of pool.parallel_for invocations>,
  "summary": "<2-3 sentence narrative for a paper explaining how Q{q}'s \
parallelization is tailored to its access pattern and synchronization needs>"
}}
"""


def build_loader_prompt(loader_hpp: str, loader_cpp: str) -> str:
    return f"""\
Below are the loader files for a bespoke OLAP engine targeting TPC-H. The
loader runs once at build time and prepares the in-memory database for the
query phase.

=== db_loader.hpp (data structure definitions) ===
{loader_hpp}

=== db_loader.cpp (storage construction logic) ===
{loader_cpp}

### Task
Identify which loader-side parallelism strategies from the taxonomy below
are applied during the build phase.

**Loader-Side Parallelism Taxonomy:**
{LOADER_TAXONOMY_TEXT}

### Output format (JSON object)
{{
  "strategies_used": ["<strategy_name>", ...],
  "how_each_is_used": {{
    "<strategy_name>": "<one sentence citing the C++ construct that proves it>"
  }},
  "summary": "<2-3 sentence narrative explaining how the loader build phase \
is parallelized>"
}}
"""


# -- classification ------------------------------------------------------------


def classify_all_queries(query_nums: list[str]) -> list[dict]:
    thread_pool_hpp = read_output_file("thread_pool.hpp")
    requests = []
    for q in query_nums:
        _, code = read_query_file(q)
        requests.append(
            make_request(
                SYSTEM_EXPERT,
                build_query_prompt(q, code, thread_pool_hpp),
                max_tokens=30000,
            )
        )

    token_counts = llms.count_tokens(requests, silent=True)
    total_tokens = sum(token_counts) if isinstance(token_counts, list) else token_counts
    print(
        f"Per-query MT classification: {len(requests)} queries, "
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
                    "parallel_for_count": 0,
                    "summary": text[:300],
                }
            )
    return results


def classify_loader() -> dict:
    loader_hpp = read_output_file("db_loader.hpp")
    loader_cpp = read_output_file("db_loader.cpp")

    req = make_request(
        SYSTEM_EXPERT, build_loader_prompt(loader_hpp, loader_cpp), max_tokens=32000
    )
    tokens = llms.count_tokens(req, silent=True)
    print(f"Loader MT classification request: ~{tokens:,} input tokens")

    resp = llms.execute(req, budget=2.0, silent=False)
    c = llms.cost(resp, silent=True)
    print(f"  -> cost ${c:.4f}")

    assert isinstance(resp, dict), (
        "Expected single response dict for loader classification"
    )
    text = extract_text(resp)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  [WARN] Loader JSON parse error: {e}\n  Raw: {text[:400]}")
        return {"strategies_used": [], "how_each_is_used": {}, "summary": text[:300]}


# -- output / reporting --------------------------------------------------------

DISPLAY_NAMES = STRATEGY_DISPLAY_NAMES


BAR_WIDTH = 30


def _display_name(strategy: str) -> str:
    entry = DISPLAY_NAMES.get(strategy, (strategy, ""))
    return entry[0] if isinstance(entry, tuple) else entry


def _bar(n: int, total: int) -> str:
    if total == 0:
        return ""
    filled = round(BAR_WIDTH * n / total)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def print_query_summaries(queries: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("QUERY MULTI-THREADING SUMMARIES")
    print("=" * 80)
    for qr in sorted(queries, key=lambda x: x["query"][1:]):
        print(f"\n### {qr['query']}")

        pf = qr.get("parallel_for_count", None)
        if pf is not None:
            print(f"  parallel_for invocations: {pf}")

        for group_key, group_label in GROUP_LABELS.items():
            if group_key == "loader":
                continue
            strats = [
                s
                for s in qr.get("strategies_used", [])
                if STRATEGY_TO_GROUP.get(s) == group_key
            ]
            if strats:
                print(f"  {group_label}: {', '.join(strats)}")

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


def print_loader_summary(loader: dict) -> None:
    print("\n" + "=" * 80)
    print("LOADER-SIDE MULTI-THREADING")
    print("=" * 80)
    strats = ", ".join(loader.get("strategies_used", [])) or "(none)"
    print(f"\n  Strategies: {strats}")
    summary = loader.get("summary", "")
    if summary:
        print(f"\n  {summary}")


def _print_strategy_subtable(
    strats: list[str],
    counts: dict[str, int],
    total: int,
    count_label: str,
) -> None:
    W = 42
    print(f"\n  {'Strategy':<{W}} {count_label:>9}  {'':>{BAR_WIDTH}}")
    print(f"  {'-' * W} {'-' * 9}  {'-' * BAR_WIDTH}")
    for s in sorted(strats, key=lambda s: -counts.get(s, 0)):
        label = _display_name(s)
        n = counts.get(s, 0)
        pct = f"{100 * n / total:.0f}%" if total > 0 else "—"
        bar = _bar(n, total)
        print(f"  {label:<{W}} {n:>5} {pct:>3}  {bar}")


def print_heatmap(queries: list[dict], loader: dict) -> None:
    print("\n" + "=" * 80)
    print("MULTI-THREADING STRATEGY USAGE SUMMARY  (for paper)")
    print("=" * 80)

    query_counts: dict[str, int] = {}
    for qr in queries:
        for s in qr.get("strategies_used", []):
            query_counts[s] = query_counts.get(s, 0) + 1

    total_queries = len(queries)
    seen = set(query_counts)

    for group_key, group_label in GROUP_LABELS.items():
        if group_key == "loader":
            continue
        group_taxonomy = {
            "pool": POOL_DISPATCH_TAXONOMY,
            "partition": WORK_PARTITIONING_TAXONOMY,
            "memory": MEMORY_CACHE_TAXONOMY,
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

    # Loader strategies: applied or not (count is 0/1 per strategy)
    print("\n--- Loader-Side Parallelism (build phase) ---")
    loader_counts: dict[str, int] = {s: 1 for s in loader.get("strategies_used", [])}
    loader_strats = [s for s in LOADER_PARALLELISM_TAXONOMY if s in loader_counts]
    if loader_strats:
        _print_strategy_subtable(loader_strats, loader_counts, 1, "applied")

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
    labels = []
    for s in sorted_strats:
        labels.append(_display_name(s))
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


def save_results(queries: list[dict], loader: dict, benchmark: str) -> None:
    import matplotlib.pyplot as plt

    file_prefix = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{benchmark}"

    out_dir = THIS_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{file_prefix}_multi_threading_strategies.json").write_text(
        json.dumps({"queries": queries, "loader": loader}, indent=2)
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
        ("pool", "Coordination & Dispatch", POOL_DISPATCH_TAXONOMY, "#2E86AB"),
        ("partition", "Work Distribution", WORK_PARTITIONING_TAXONOMY, "#A23B72"),
        ("memory", "Memory & Cache Discipline", MEMORY_CACHE_TAXONOMY, "#6F4E7C"),
    ]

    panels = []
    for _, label, taxonomy, color in group_data:
        strats = [s for s in taxonomy if s in seen]
        if strats:
            panels.append((label, strats, color, query_counts, total_queries))

    # Loader as its own panel
    loader_counts = {s: 1 for s in loader.get("strategies_used", [])}
    loader_strats = [s for s in LOADER_PARALLELISM_TAXONOMY if s in loader_counts]
    if loader_strats:
        panels.append(
            (
                "Loader-Side Parallelism (build phase)",
                loader_strats,
                "#8B5A2B",
                loader_counts,
                1,
            )
        )

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
        figsize=(7.5, fig_h),
        gridspec_kw={"height_ratios": n_rows, "hspace": 0.6},
    )
    if len(panels) == 1:
        axes = [axes]

    for ax, (label, strats, color, counts, total) in zip(axes, panels):
        is_loader = label.startswith("Loader-Side")
        xlabel = (
            "Applied (1) / Not applied (0)"
            if is_loader
            else f"Fraction of queries  (out of {total} total)"
        )
        _draw_panel(
            ax,
            strats,
            counts,
            total,
            color=color,
            title=label,
            xlabel=xlabel,
        )

    fig.suptitle(
        "Bespoke Engine -- Multi-Threading Strategy Coverage",
        fontsize=11,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()

    pdf_path = out_dir / f"{file_prefix}_multi_threading_strategy_usage.pdf"
    fig.savefig(pdf_path, bbox_inches="tight", dpi=300)
    png_path = out_dir / f"{file_prefix}_multi_threading_strategy_usage.png"
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
                    "total": total_queries,
                    "count_unit": "queries",
                }
            )
    for strategy in LOADER_PARALLELISM_TAXONOMY:
        applied = 1 if strategy in loader_counts else 0
        rows.append(
            {
                "group": "Loader-Side Parallelism",
                "strategy": strategy,
                "count": applied,
                "fraction": float(applied),
                "total": 1,
                "count_unit": "build",
            }
        )
    csv_path = out_dir / f"{file_prefix}_multi_threading_strategy_usage.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Saved CSV    -> {csv_path}")


# -- main ----------------------------------------------------------------------

#  python plots/classify_bespoke_multi_threading/classify_multi_threading.py --wandb_id 5qumtphx --benchmark ceb


def main() -> None:

    parser = argparse.ArgumentParser(
        description="Classify multi-threading strategies in bespoke OLAP engine"
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
        working_dir=REPO_ROOT / "output",
        extra_gitignore=[],
    )
    snapshotter.fetch_snapshots()

    print(f"Restoring snapshot: {snapshot}")
    snapshotter.restore(snapshot)

    # load .env file
    load_dotenv(REPO_ROOT / ".env")

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

    loader = classify_loader()
    queries = classify_all_queries(available_queries)

    print_query_summaries(queries)
    print_loader_summary(loader)
    print_heatmap(queries, loader)
    save_results(queries, loader, benchmark)


if __name__ == "__main__":
    main()
