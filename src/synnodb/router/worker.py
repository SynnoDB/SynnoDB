"""``WorkerEngine`` — a bespoke engine that runs out-of-process over shared memory.

This is the production engine shape: a persistent **warm subprocess** holding the
ingested tables, fed control messages over a pipe and exchanging bulk Arrow over
``/dev/shm``. It is crash-isolated — if the worker dies, ``run`` raises and the
router falls back to DuckDB (and the worker can be respawned) without taking down
the user's process.

The reference worker (`_worker_main`) executes with an in-process DuckDB over the
ingested Arrow; the C++ worker is a drop-in that runs the compiled plugin instead.
Both satisfy the ``BespokeEngine`` interface, so the router is unchanged.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Mapping, Optional

import pyarrow as pa

from .shm_transport import SHM_DIR, SegmentRef, ShmWriter, read_table, sweep_orphans
from .worker_protocol import read_message, write_message

log = logging.getLogger("synnodb.router.worker")


class WorkerEngineError(RuntimeError):
    pass


class WorkerEngine:
    """A subprocess-backed bespoke engine using the shm data plane.

    ``templates`` maps ``query_id -> SQL`` (the reference worker executes it over the
    ingested tables). Data is supplied once via :meth:`ingest` (the connect-time
    step); thereafter :meth:`run` is the hot path the router calls.
    """

    def __init__(
        self,
        engine_id: str,
        templates: Mapping[str, str],
        *,
        shm_dir: Optional[Path] = None,
        worker_argv: Optional[list] = None,
    ) -> None:
        self.engine_id = engine_id
        self._templates = dict(templates)
        self._shm_dir = Path(shm_dir) if shm_dir is not None else SHM_DIR
        self._worker_argv = worker_argv
        self._proc: Optional[subprocess.Popen] = None
        self._ingest = ShmWriter(base_dir=self._shm_dir)  # parent-owned ingest segments
        self._loaded = False

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self.health():
            return
        reaped = sweep_orphans(base_dir=self._shm_dir)
        if reaped:
            log.debug("engine=%s swept %d orphaned shm segment(s)", self.engine_id, reaped)
        argv = self._worker_argv or [
            sys.executable, "-m", "synnodb.router._worker_main",
            "--shm-dir", str(self._shm_dir), "--parent-pid", str(os.getpid()),
        ]
        log.info("engine=%s spawning worker: %s (shm_dir=%s)", self.engine_id, " ".join(argv), self._shm_dir)
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
        )
        log.debug("engine=%s worker pid=%s", self.engine_id, self._proc.pid)

    def health(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ingest(self, tables: Mapping[str, pa.Table]) -> None:
        """Hand the engine its data snapshot (Arrow, via shm). Call once after load."""
        self.start()
        payload: Dict[str, dict] = {}
        total = 0
        for name, table in tables.items():
            ref = self._ingest.write_table(table)
            payload[name] = {"name": ref.name, "nbytes": ref.nbytes}
            total += ref.nbytes
            log.debug(
                "engine=%s ingest table=%s rows=%d bytes=%d segment=%s",
                self.engine_id, name, table.num_rows, ref.nbytes, ref.name,
            )
        log.info("engine=%s ingesting %d table(s), %.1f MiB via shm", self.engine_id, len(payload), total / 1048576)
        reply = self._round_trip({"kind": "load", "tables": payload})
        if reply.get("kind") != "loaded":
            log.error("engine=%s load failed: %s", self.engine_id, reply)
            raise WorkerEngineError(f"load failed: {reply}")
        self._loaded = True
        log.info("engine=%s load complete (%s tables resident)", self.engine_id, reply.get("tables"))

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    write_message(self._proc.stdin, {"kind": "shutdown"})
                    self._proc.wait(timeout=5)
            except Exception:
                pass
            finally:
                if self._proc.poll() is None:
                    self._proc.kill()
                self._proc = None
        self._ingest.close()

    # ---- BespokeEngine.run ---------------------------------------------
    def run(self, query_id: str, placeholders: Mapping[str, object]) -> pa.Table:
        if not self.health():
            raise WorkerEngineError("worker not running")
        sql = self._templates.get(query_id)
        if sql is None:
            raise KeyError(f"engine {self.engine_id!r} has no query {query_id!r}")
        log.debug("engine=%s run query_id=%s placeholders=%s", self.engine_id, query_id, dict(placeholders))
        started = time.perf_counter()
        reply = self._round_trip({"kind": "run", "query_id": query_id, "sql": sql, "params": dict(placeholders)})
        if reply.get("kind") == "error":
            log.warning("engine=%s query_id=%s worker error: %s", self.engine_id, query_id, reply.get("message"))
            raise WorkerEngineError(reply.get("message", "engine error"))
        if reply.get("kind") != "result":
            log.error("engine=%s query_id=%s unexpected reply: %s", self.engine_id, query_id, reply)
            raise WorkerEngineError(f"unexpected reply: {reply}")
        log.debug(
            "engine=%s query_id=%s -> rows=%s bytes=%s worker_ms=%.2f rtt_ms=%.2f segment=%s",
            self.engine_id, query_id, reply.get("rows"), reply.get("nbytes"),
            reply.get("elapsed_ms", -1.0), (time.perf_counter() - started) * 1000.0, reply.get("name"),
        )
        ref = SegmentRef(reply["name"], reply["nbytes"])
        table = read_table(ref, base_dir=self._shm_dir)
        # Unlink now: on Linux the mmap keeps the data valid for the table's lifetime,
        # while the segment name is reclaimed immediately (parent owns cleanup).
        try:
            (self._shm_dir / ref.name).unlink()
        except FileNotFoundError:
            pass
        return table

    # ---- internals ------------------------------------------------------
    def _round_trip(self, message: dict) -> dict:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise WorkerEngineError("worker not started")
        try:
            write_message(self._proc.stdin, message)
            reply = read_message(self._proc.stdout)
        except BrokenPipeError:
            reply = None
        if reply is None:
            rc = self._proc.poll()
            log.warning(
                "engine=%s worker died during %s (exit=%s)", self.engine_id, message.get("kind"), rc
            )
            raise WorkerEngineError(f"worker died (broken pipe / no reply; exit={rc})")
        return reply

    def __enter__(self) -> "WorkerEngine":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False
