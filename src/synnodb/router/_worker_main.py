"""Reference engine-worker subprocess (Python).

Proves the out-of-process architecture end to end: it maps ingest segments from
shared memory **zero-copy**, holds the tables resident, executes queries, and writes
results back through shared memory — exchanging only tiny control messages over the
stdin/stdout pipe. The real engine is a compiled C++ plugin that implements the same
contract (``ReadArrowTableFromShm`` + an Arrow-IPC shm result writer); this worker
executes with an in-process DuckDB over the ingested Arrow, which is both a faithful
reference and a usable out-of-process engine type.

Protocol (see worker_protocol):
  load   {tables: {name: {name: <segment>}}}      -> loaded {tables: N}
  run    {query_id, sql, params}                  -> result {name, nbytes, rows, elapsed_ms} | error {message}
  ping   {}                                        -> pong {}
  shutdown {}                                      -> (exit)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback

import duckdb

from synnodb.router.shm_transport import SegmentRef, ShmWriter, read_table
from synnodb.router.worker_protocol import read_message, write_message

# Log to STDERR (stdout is the binary control channel). Tunable via SYNNODB_WORKER_LOG.
log = logging.getLogger("synnodb.worker")


def _setup_logging() -> None:
    level = os.environ.get("SYNNODB_WORKER_LOG", "WARNING").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[worker pid=%(process)d] %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(getattr(logging, level, logging.WARNING))


def _execute(con: "duckdb.DuckDBPyConnection", sql: str, params):
    if params:
        return con.execute(sql, params)
    return con.execute(sql)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shm-dir", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    _setup_logging()
    log.info("started shm_dir=%s parent_pid=%d", args.shm_dir, args.parent_pid)

    con = duckdb.connect()
    writer = ShmWriter(base_dir=args.shm_dir, owner_pid=args.parent_pid)
    # Keep references to ingested tables so their mmaps stay alive for the session.
    held: dict[str, object] = {}

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        msg = read_message(stdin)
        if msg is None:
            log.info("control channel closed; exiting")
            break
        kind = msg.get("kind")
        try:
            if kind == "ping":
                write_message(stdout, {"kind": "pong"})
            elif kind == "load":
                for name, seg in msg["tables"].items():
                    table = read_table(SegmentRef(seg["name"], seg.get("nbytes", 0)), base_dir=args.shm_dir)
                    held[name] = table
                    con.register(name, table)
                    log.debug("loaded table=%s rows=%d segment=%s", name, table.num_rows, seg["name"])
                write_message(stdout, {"kind": "loaded", "tables": len(held)})
            elif kind == "run":
                start = time.perf_counter()
                cursor = _execute(con, msg["sql"], msg.get("params"))
                table = cursor.to_arrow_table() if hasattr(cursor, "to_arrow_table") else cursor.fetch_arrow_table()
                ref = writer.write_table(table)
                elapsed = (time.perf_counter() - start) * 1000.0
                log.debug("run query_id=%s rows=%d %.2fms", msg.get("query_id"), table.num_rows, elapsed)
                write_message(
                    stdout,
                    {
                        "kind": "result",
                        "name": ref.name,
                        "nbytes": ref.nbytes,
                        "rows": table.num_rows,
                        "elapsed_ms": elapsed,
                    },
                )
            elif kind == "shutdown":
                log.info("shutdown requested")
                break
            else:
                log.warning("unknown message kind %r", kind)
                write_message(stdout, {"kind": "error", "message": f"unknown message kind {kind!r}"})
        except Exception as exc:  # report (with traceback to stderr), keep serving
            log.error("error handling %s: %s\n%s", kind, exc, traceback.format_exc())
            try:
                write_message(stdout, {"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
            except Exception:
                log.critical("failed to report error to parent; exiting")
                break

    writer.close()
    log.info("exited")


if __name__ == "__main__":
    main()
