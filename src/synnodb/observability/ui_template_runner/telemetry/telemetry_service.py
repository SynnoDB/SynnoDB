#!/usr/bin/env python3
"""Periodic system-resource telemetry for the OLAP demo stack.

Only emits SYSSTAT/PROCSTAT lines while a query is actively running on one of
the watched engine services. "Running" is detected by sampling per-process CPU
jiffies from /proc over a short window and checking whether any watched
process exceeds CPU_THRESHOLD_CORES. Idle periods produce no log output, so
the file does not grow unbounded when the stack is sitting idle.

The deploy script pipes stdout into logs/<datetime>_telemetry.log via tee.
"""

import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
SAMPLE_WINDOW_S = 0.5
CPU_THRESHOLD_CORES = 0.20
WATCHED_SCRIPTS = frozenset(
    {"umbra_service", "bespoke_service", "run_generated_code_service"}
)
_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
    return info


def _find_watched_pids() -> dict[int, str]:
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,command", "--no-headers"],
            text=True,
            timeout=5,
        )
    except Exception as exc:
        logger.warning("ps collection failed: %s", exc)
        return {}
    pids: dict[int, str] = {}
    for line in out.splitlines():
        cols = line.split(None, 1)
        if len(cols) < 2:
            continue
        pid_str, cmd = cols
        matched = next((s for s in WATCHED_SCRIPTS if s in cmd), None)
        if matched:
            try:
                pids[int(pid_str)] = matched
            except ValueError:
                pass
    return pids


def _read_jiffies(pid: int) -> int:
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    # comm field is wrapped in parens and may contain spaces/parens; split
    # everything after the closing paren so the remaining fields are stable.
    rparen = data.rfind(")")
    fields = data[rparen + 2 :].split()
    # After the comm field the layout starts at "state"; utime is the 12th
    # field overall, stime the 13th — that's indices 11 and 12 here.
    return int(fields[11]) + int(fields[12])


def _read_rss_kb(pid: int) -> int:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def _sample_cpu_cores(pids: dict[int, str]) -> dict[int, float]:
    """Return {pid: cpu_cores_used} measured over SAMPLE_WINDOW_S."""
    before: dict[int, int] = {}
    for pid in pids:
        try:
            before[pid] = _read_jiffies(pid)
        except OSError:
            continue
    time.sleep(SAMPLE_WINDOW_S)
    result: dict[int, float] = {}
    for pid, j0 in before.items():
        try:
            j1 = _read_jiffies(pid)
        except OSError:
            continue
        result[pid] = (j1 - j0) / (_CLK_TCK * SAMPLE_WINDOW_S)
    return result


def _emit(pids: dict[int, str], cpu_cores: dict[int, float]) -> None:
    mem = _meminfo()
    total_gb = mem.get("MemTotal", 0) / 1_048_576
    avail_gb = mem.get("MemAvailable", 0) / 1_048_576
    logger.info(
        "SYSSTAT mem_used_gb=%.2f mem_total_gb=%.2f mem_avail_gb=%.2f",
        total_gb - avail_gb,
        total_gb,
        avail_gb,
    )
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    logger.info(
        "SYSSTAT load_1m=%s load_5m=%s load_15m=%s procs=%s",
        parts[0],
        parts[1],
        parts[2],
        parts[3],
    )
    for pid, name in pids.items():
        cores = cpu_cores.get(pid)
        if cores is None:
            continue
        try:
            rss_kb = _read_rss_kb(pid)
        except OSError:
            continue
        logger.info(
            "PROCSTAT service=%s pid=%s cpu_cores=%.2f rss_kb=%s",
            name,
            pid,
            cores,
            rss_kb,
        )


def main() -> None:
    logger.info(
        "Telemetry collector started (poll=%.1fs sample=%.1fs threshold=%.2f cores watching=%s)",
        POLL_INTERVAL_S,
        SAMPLE_WINDOW_S,
        CPU_THRESHOLD_CORES,
        sorted(WATCHED_SCRIPTS),
    )
    while True:
        pids = _find_watched_pids()
        if pids:
            cpu = _sample_cpu_cores(pids)
            if cpu and max(cpu.values(), default=0.0) >= CPU_THRESHOLD_CORES:
                _emit(pids, cpu)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
