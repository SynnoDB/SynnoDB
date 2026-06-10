import argparse
import logging
import sys
from pathlib import Path

from observability.logging.logger import setup_logging

sys.path.append(str(Path(__file__).parent.parent.parent))

from observability.benchmark.plot import plot_logs
from observability.benchmark.run import run_benchmark
from utils.cli_config import add_common_args


def build_run_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "--snapshots",
        type=str,
        default=None,
        help="Comma-separated list of snapshot commit hashes to iterate (for bespoke).",
    )
    parser.add_argument(
        "--wandb_ids",
        type=str,
        default=None,
        help="Comma-separated list of wand run-ids to iterate (for bespoke).",
    )
    parser.add_argument(
        "--scale_factors",
        type=str,
        default="1",
        help="Comma-separated scale factors to benchmark.",
    )
    parser.add_argument(
        "--query_ids",
        type=str,
        default=None,
        help="Comma-separated list of query IDs to benchmark.",
    )
    parser.add_argument(
        "--instantiations",
        type=int,
        default=1,
        help="Number of distinct query parameter sets (different random seeds).",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Number of times to repeat each instantiation (same SQL, for timing stability).",
    )
    parser.add_argument(
        "--num_threads",
        type=str,
        default="1",
        help="Comma-separated list of thread counts to benchmark (bespoke only). E.g. '1,4,8'.",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Write benchmark results to this CSV file. Defaults to bench_<system>_....csv.",
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default="tpch",
        help="Benchmark to run (e.g., tpch, ceb).",
    )
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="System to benchmark (e.g. bespoke, duckdb, umbra, clickhouse).",
    )
    parser.add_argument(
        "--systems",
        type=str,
        default=None,
        help="Deprecated alias for --system. Only one system is supported per run.",
    )
    add_common_args(
        parser,
        include_notify=True,
        include_disable_repo_sync=True,
        include_artifacts_dir=True,
        include_base_parquet_dir=True,
        include_db_storage=True,
    )
    return parser


def build_plot_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "logs",
        nargs="+",
        help="Benchmark CSV logs to combine.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output image path.",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Optional plot title.",
    )
    parser.add_argument(
        "--x",
        required=True,
        choices=["scale_factor", "num_threads", "query_id"],
        default="scale_factor",
        help="What to show on the x-axis (default: scale_factor).",
    )
    parser.add_argument(
        "--by-query",
        action="store_true",
        help="Deprecated. Use --x query_id instead.",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=None,
        help="Only include rows with num_threads less than or equal to this value.",
    )
    parser.add_argument(
        "--legend-pos",
        choices=["up", "top", "bottom"],
        default=None,
        help="Legend position for thread-scaling plots. Use 'bottom' to place legends below the plot.",
    )
    parser.add_argument(
        "--product-plot",
        action="store_true",
        help="Use a larger, presentation-oriented visual style for plots.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    args = args[1:] if len(args) > 1 else []

    setup_logging(logging.DEBUG)

    if command == "plot":
        plot_args = build_plot_parser().parse_args(args)
        plot_logs(plot_args)
    elif command == "run":
        run_args = build_run_parser().parse_args(args)
        run_benchmark(run_args)
    else:
        raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
