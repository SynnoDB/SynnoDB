"""Flat- vs. bespoke-storage comparison.

For a set of W&B runs (each one either a flat-storage or a bespoke-storage
engine for a given benchmark), measure how much the bespoke storage layout
speeds the engine up over the flat layout.

Two tables are produced: one for the base-implementation runs and one for the
fully optimized runs. Each table reports, per benchmark, the total speedup of
the flat and bespoke engines over DuckDB plus the direct bespoke-vs-flat
speedup, and a combined ``Total`` row.
"""

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(Path(__file__).resolve().parents[2].as_posix())

from synnodb.observability.plots.utils.per_stage_data_prep import _target_sf_for_benchmark
from synnodb.observability.plots.utils.wandb_utils import get_wandb_stats

_WANDB_CACHE = Path("/mnt/labstore/bespoke_olap/wandb_cache")
_BENCHMARK_NAMES = {"tpch": "TPC-H", "ceb": "CEB"}


def _query_ids(summary: dict) -> tuple[list[str], int]:
    """Return ``(query_ids, num_queries)``.

    ``num_queries`` is the full count of validated queries (including the 08a
    outlier) used to match the ``validation/num_queries`` filter, mirroring the
    ``num_queries`` filter in journal_plots.ipynb. ``query_ids`` is the analysed
    subset with the 08a outlier (DuckDB is occasionally pathologically slow on
    it) removed.
    """
    regex = re.compile(r"validation/query_([0-9a-zA-Z]+)/speedup")
    all_qids = sorted({m.group(1) for k in summary if (m := regex.match(k))})
    qids = [q for q in all_qids if q not in ("08a", "8a")]
    return qids, len(all_qids)


def _final_query_runtimes(
    history: pd.DataFrame,
    query_ids: list[str],
    num_queries: int,
    target_sf: int | float,
) -> dict[str, dict[str, float]]:
    """Final per-query runtimes (ms) at ``target_sf``, matching journal_plots.

    Filtering and selection replicate journal_plots.ipynb exactly: keep the
    full-benchmark, non-trace, optimized validation rows at ``target_sf``; for
    each query take the last row where both ``speedup`` and ``duckdb_runtime``
    are present; derive the engine runtime as ``duckdb / speedup``; read umbra
    from that same row. Returns ``{qid: {"impl", "duckdb", "umbra"?}}``.
    """
    f = history
    for col, want in (
        ("validation/scale_factor", target_sf),
        ("validation/trace_mode", False),
        ("validation/num_queries", num_queries),
        ("validation/compile_with_optimize", True),
    ):
        if col in f.columns:
            f = f[f[col] == want]

    runtimes: dict[str, dict[str, float]] = {}
    for qid in query_ids:
        sk = f"validation/query_{qid}/speedup"
        dk = f"validation/query_{qid}/duckdb_runtime_ms"
        uk = f"validation/query_{qid}/umbra_runtime_ms"
        if sk not in f.columns or dk not in f.columns:
            continue
        valid = f[f[sk].notna() & f[dk].notna()]
        if valid.empty:
            continue
        last = valid.iloc[-1]
        speedup = float(last[sk])
        duckdb_ms = float(last[dk])
        if not np.isfinite(duckdb_ms):
            continue
        # Mirror journal_plots: derive engine runtime from duckdb / speedup; a
        # non-finite speedup means the engine runtime rounded to ~0, so floor it
        # to 1ms instead of dropping the query.
        if not np.isfinite(speedup):
            impl_ms = 1.0
        elif speedup > 0:
            impl_ms = duckdb_ms / speedup
        else:
            impl_ms = duckdb_ms
        entry = {"impl": impl_ms, "duckdb": duckdb_ms}
        if uk in f.columns:
            umbra_ms = float(last[uk]) if pd.notna(last[uk]) else np.nan
            if np.isfinite(umbra_ms) and umbra_ms > 0:
                entry["umbra"] = umbra_ms
        runtimes[qid] = entry
    return runtimes


def _total_speedup(runtimes: dict[str, dict[str, float]], baseline: str) -> float:
    """sum(baseline_ms) / sum(impl_ms) over queries that have both."""
    base_total = impl_total = 0.0
    for entry in runtimes.values():
        if baseline in entry and "impl" in entry:
            base_total += entry[baseline]
            impl_total += entry["impl"]
    return base_total / impl_total if impl_total > 0 else float("nan")


def _bespoke_vs_flat(
    flat: dict[str, dict[str, float]],
    bespoke: dict[str, dict[str, float]],
) -> tuple[float, int]:
    """Total impl speedup of bespoke over flat storage on shared queries."""
    common = sorted(set(flat) & set(bespoke))
    flat_total = sum(flat[q]["impl"] for q in common)
    bespoke_total = sum(bespoke[q]["impl"] for q in common)
    speedup = flat_total / bespoke_total if bespoke_total > 0 else float("nan")
    return speedup, len(common)


def analyze_flat_vs_bespoke_storage(
    runs: dict[tuple[str, str], str],
) -> pd.DataFrame:
    """Build a flat-vs-bespoke storage speedup table.

    ``runs`` maps ``(benchmark, storage)`` to a W&B run id, where ``storage`` is
    ``"flat"`` or ``"bespoke"``. The storage label is taken from this mapping
    rather than the run config, because the ``bespoke_storage`` config flag is
    not reliably set on the optimization-loop runs. Each benchmark is compared
    at its fixed target scale factor (TPC-H: sf20, CEB: sf2).
    """
    # group runs: benchmark -> {"flat"/"bespoke": runtimes}
    grouped: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for (benchmark, storage), run_id in runs.items():
        summary, history, _config = get_wandb_stats(
            run_id, skip_cache=False, wandb_run_cache_path=_WANDB_CACHE
        )
        target_sf = _target_sf_for_benchmark(benchmark)
        query_ids, num_queries = _query_ids(summary)
        runtimes = _final_query_runtimes(history, query_ids, num_queries, target_sf)
        if not runtimes:
            print(
                f"  warning: {run_id} ({benchmark}/{storage}) has no runtime data "
                f"at sf{target_sf:g}"
            )
        grouped.setdefault(benchmark, {})[storage] = runtimes

    speedup_cols = [
        "Flat vs DuckDB",
        "Flat vs Umbra",
        "Bespoke vs DuckDB",
        "Bespoke vs Umbra",
        "Bespoke vs Flat",
    ]
    rows = []
    for benchmark in sorted(grouped):
        engines = grouped[benchmark]
        flat = engines.get("flat", {})
        bespoke = engines.get("bespoke", {})

        bvf, n_common = _bespoke_vs_flat(flat, bespoke)
        rows.append(
            {
                "Benchmark": _BENCHMARK_NAMES.get(benchmark, benchmark),
                "Queries": n_common,
                "Flat vs DuckDB": _total_speedup(flat, "duckdb"),
                "Flat vs Umbra": _total_speedup(flat, "umbra"),
                "Bespoke vs DuckDB": _total_speedup(bespoke, "duckdb"),
                "Bespoke vs Umbra": _total_speedup(bespoke, "umbra"),
                "Bespoke vs Flat": bvf,
            }
        )

    df = pd.DataFrame(rows)
    fmt = df.copy()
    for col in speedup_cols:
        fmt[col] = fmt[col].map(lambda v: f"{v:.2f}x" if np.isfinite(v) else "-")
    print(fmt.to_string(index=False))
    return df


ids_after_optim = {
    ("tpch", "flat"): "efmm03qe",
    ("tpch", "bespoke"): "3zdiw9ol",
    ("ceb", "flat"): "b5io9d9o",
    ("ceb", "bespoke"): "gava9bsh",
}

ids_base = {
    ("tpch", "flat"): "d9eqjpv0",
    ("tpch", "bespoke"): "86crnuc0",
    ("ceb", "flat"): "ivcttrx6",
    ("ceb", "bespoke"): "iv5w7m07",
}


if __name__ == "__main__":
    print("=" * 70)
    print("Base implementation: flat vs bespoke storage")
    print("=" * 70)
    analyze_flat_vs_bespoke_storage(ids_base)

    print()
    print("=" * 70)
    print("After optimization: flat vs bespoke storage")
    print("=" * 70)
    analyze_flat_vs_bespoke_storage(ids_after_optim)
