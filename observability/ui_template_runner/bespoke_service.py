#!/usr/bin/env python3
"""Standalone BespokeOLAP HTTP service.

Restores a git snapshot, compiles the generated binary once, then serves
queries via a simple HTTP API so other processes can run queries without
re-initializing the runner.

Endpoints:
  GET  /health              -> {"status": "ok"}
  POST /query               -> body: {"query_id": "1", "placeholders": {"k": "v"}, "sf": 1}
                               response: {"csv": "...", "time_ms": 123.4}

Usage:
  python bespoke_service.py tpch --wandb_snapshot <hash> --sf 1 --port 7657
"""

import argparse
import http.server
import json
import logging
import socketserver

# add parent to path
import sys
import threading
from pathlib import Path

from cpp_runner.compiler.compiler_utils import make_compiler
from observability.logging.logger import setup_logging
from tools.validate.query_validator_class import format_args_string
from workloads.dataset.dataset_tables_dict import get_dataset_name
from workloads.dataset.query_gen_factory import get_query_gen

sys.path.append(str(Path(__file__).parent.parent.parent))

from observability.benchmark.run import get_all_query_ids
from observability.ui_template_runner.service_notify import (
    notify_5xx_response,
    notify_service_crash,
)
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool, RunWorkerResult, delete_result_csv_files

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)


class _State:
    db_engine: RunTool = None  # type: ignore
    sf: float = None  # type: ignore
    optimize: bool = None  # type: ignore
    workspace_dir: Path = None  # type: ignore
    lock: threading.Lock = threading.Lock()


STATE = _State()


def _discover_first_query_id(benchmark: str) -> tuple[str, dict]:
    """Return the first query_id and its default placeholders (seed=42)."""
    import random

    query_ids = get_all_query_ids(benchmark)
    gen_query_fn = get_query_gen(benchmark)
    rnd = random.Random(42)
    first_qid = query_ids[0]
    _, _, placeholders = gen_query_fn(query_name=f"Q{first_qid}", rnd=rnd)
    return first_qid, placeholders


def init_service(args) -> None:
    """Restore git snapshot, compile binary, warm up."""
    THIS_DIR = Path(__file__).parent
    workspace_dir = THIS_DIR / "output"

    assert workspace_dir.exists(), f"Workspace directory not found: {workspace_dir}"
    if args.wandb_snapshot is not None:
        from observability.logging.wandb_api_helper import (
            wandb_retrieve_metrics_for_run,
        )

        statistics, _, _ = wandb_retrieve_metrics_for_run(
            benchmark=args.benchmark, run_id=args.wandb_snapshot, output_hist=False
        )
        git_snapshot = statistics["code/snapshot_hash"]

        snapshotter = GitSnapshotter(
            cache_repo="git://c01/bespoke_cache.git",
            working_dir=workspace_dir,
            extra_gitignore=[],
            do_not_snapshot=True,
        )
        assert snapshotter.has_snapshot(git_snapshot), (
            f"Snapshot {git_snapshot} not found in repo."
        )
        logger.info("Restoring snapshot %s", git_snapshot)

        # TODO: a prepare repo here is necessary!

        snapshotter.restore(git_snapshot)
    else:
        print(
            f"Take current code in {THIS_DIR} as snapshot since no wandb_snapshot provided."
        )

    sf = args.sf
    sf = float(sf) if "." in str(sf) else int(sf)

    parquet_dir = (
        Path(args.base_parquet_dir) / f"{get_dataset_name(args.benchmark)}_parquet"
    )
    assert parquet_dir.exists(), f"Parquet directory not found: {parquet_dir}"

    db_engine = RunTool(
        cwd=workspace_dir,
        dataset_name=args.benchmark,
        base_parquet_dir=(parquet_dir.as_posix()),
        run_stats_collector=None,
        db_storage=args.db_storage,
        compiler=make_compiler(
            cwd=workspace_dir,
            db_storage=args.db_storage,
            untracked_cpp_runner_content="",
        ),
    )

    STATE.db_engine = db_engine
    STATE.sf = sf
    STATE.optimize = args.optimize
    STATE.workspace_dir = workspace_dir

    # Warmup: compile binary with default placeholders of the first query.
    first_qid, placeholders = _discover_first_query_id(args.benchmark)
    warmup_args = format_args_string([first_qid], [placeholders])
    logger.info("Warmup compile: running Q%s with default placeholders…", first_qid)
    db_engine.run_worker(
        scale_factor=sf,
        optimize=args.optimize,
        stdin_args_data=warmup_args,
    )
    logger.info("Binary ready.")
    delete_result_csv_files(workspace_path=workspace_dir)


class _BespokeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("HTTP %s - " + fmt, self.client_address[0], *args)

    def _send(self, code: int, body: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")
        if code >= 500:
            notify_5xx_response("bespoke", self.path, code, data)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Use POST /query"})

    def do_POST(self):
        if self.path != "/query":
            self._send_json(404, {"error": "Unknown endpoint"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        query_id = req.get("query_id")
        run_id = req.get("run_id", "")
        placeholders = req.get("placeholders", {})
        sf = req.get("sf", STATE.sf)

        if not query_id:
            self._send_json(400, {"error": "Missing 'query_id' field"})
            return
        if not isinstance(placeholders, dict):
            self._send_json(400, {"error": "'placeholders' must be an object"})
            return

        if isinstance(sf, str):
            sf = float(sf) if "." in sf else int(sf)

        args_list = format_args_string([query_id], [placeholders])

        with STATE.lock:
            try:
                trace_mode = False
                result: RunWorkerResult = STATE.db_engine.run_worker(
                    scale_factor=sf,
                    optimize=STATE.optimize,
                    stdin_args_data=args_list,
                    trace_mode=trace_mode,
                )

                if trace_mode:
                    logger.warning(
                        f"Trace output for Q{query_id}: {result.trace_output}"
                    )
                logger.warning(result.out)
                logger.warning(result.err)

                assert result.metrics is not None, "Expected metrics from run_worker"
                if "run/total_rt" not in result.metrics:
                    self._send_json(
                        500,
                        {
                            "error": f"Query execution failed for Q{query_id} (no timing metrics — likely a crash or empty result). msg={result.msg!r} out={result.out!r} err={result.err!r}"
                        },
                    )
                    return
                time_ms = result.metrics["run/total_rt"]
                logger.info(
                    "TELEMETRY run_id=%s engine=bespoke query=%s time_ms=%.1f sf=%s",
                    run_id,
                    query_id,
                    time_ms,
                    sf,
                )
            except Exception as exc:
                logger.exception("run_worker failed for Q%s", query_id)
                self._send_json(
                    500,
                    {"error": f"run_worker failed for Q{query_id}: {exc}"},
                )
                return

            csv_path = STATE.workspace_dir / "results" / "result1.csv"
            if not csv_path.exists():
                self._send_json(500, {"error": f"Result file not found: {csv_path}"})
                return

            csv_text = csv_path.read_text()

        self._send_json(200, {"run_id": run_id, "csv": csv_text, "time_ms": time_ms})


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone BespokeOLAP HTTP service")
    parser.add_argument("benchmark", choices=["tpch", "ceb"])
    parser.add_argument("--sf", default=1, help="Scale factor (default: 1)")
    parser.add_argument(
        "--base-parquet-dir",
        required=True,
        help="Root directory containing benchmark parquet folders",
    )
    parser.add_argument(
        "--no-optimize",
        dest="optimize",
        action="store_false",
        default=True,
        help="Compile without optimization",
    )
    parser.add_argument(
        "--wandb_snapshot",
        type=str,
        required=False,
        help="Wandb run-id whose code snapshot to load",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7657)
    args = parser.parse_args()

    try:
        init_service(args)

        server = _ThreadedHTTPServer((args.host, args.port), _BespokeHandler)
        logger.info(
            "BespokeOLAP service listening on http://%s:%d", args.host, args.port
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            server.shutdown()
    except Exception as exc:
        logger.exception("Bespoke service crashed")
        notify_service_crash("bespoke", exc)
        raise


if __name__ == "__main__":
    main()
