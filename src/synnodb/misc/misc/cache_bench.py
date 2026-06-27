#!/usr/bin/env python3
"""
Benchmark: NFS filesystem vs Redis latency for blob PUT/GET.

Creates 100 blobs of random size between 1 KiB and 1 MiB, then measures:
- PUT latency (write/store)
- GET latency (read/fetch)

For filesystem, it writes files under <nfs_dir>/bench_<runid>/ (atomic temp+rename).
For Redis, it stores bytes under keys bench:<runid>:<i> (SET + GET).

Usage:
  pip install redis
  python bench_nfs_vs_redis.py --nfs-dir /mnt/nfs/share --redis redis://host:6379/0

Notes:
- This measures client-perceived latency, including network + server IO.
- For fairer results, run multiple iterations and ensure both targets are warm.
"""

from __future__ import annotations

import argparse
import os
import random
import secrets
import statistics as stats
import sys
import time
from pathlib import Path
from typing import Iterable, List, Tuple

try:
    import redis  # pip install redis
except ImportError:
    redis = None


def ns_to_us(ns: int) -> float:
    return ns / 1_000.0


def ns_to_ms(ns: int) -> float:
    return ns / 1_000_000.0


def pct(xs: List[int], p: float) -> float:
    if not xs:
        return float("nan")
    xs_sorted = sorted(xs)
    k = (len(xs_sorted) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs_sorted) - 1)
    if f == c:
        return float(xs_sorted[f])
    d0 = xs_sorted[f] * (c - k)
    d1 = xs_sorted[c] * (k - f)
    return float(d0 + d1)


def summarize(label: str, ns_list: List[int]) -> str:
    if not ns_list:
        return f"{label}: (no samples)"
    mean = stats.mean(ns_list)
    med = stats.median(ns_list)
    p95 = pct(ns_list, 0.95)
    p99 = pct(ns_list, 0.99)
    return (
        f"{label}: "
        f"mean={ns_to_ms(int(mean)):.3f} ms, "
        f"median={ns_to_ms(int(med)):.3f} ms, "
        f"p95={ns_to_ms(int(p95)):.3f} ms, "
        f"p99={ns_to_ms(int(p99)):.3f} ms"
    )


def atomic_write(
    path: Path, data: bytes, mode: int = 0o664, fsync: bool = False
) -> None:
    """
    Atomic write via temp+rename. Optionally fsync file and directory for durability.
    On many NFS setups, fsync semantics may vary; enable if you want to include it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(8)}.tmp")

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp, path)  # atomic on POSIX for same directory/filesystem

    if fsync:
        # fsync the directory entry for rename durability
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def gen_blobs(n: int, min_size: int, max_size: int, seed: int) -> List[bytes]:
    rnd = random.Random(seed)
    blobs: List[bytes] = []
    for _ in range(n):
        size = rnd.randint(min_size, max_size)
        blobs.append(os.urandom(size))
    return blobs


def bench_filesystem(
    nfs_dir: Path, blobs: List[bytes], run_id: str, fsync: bool
) -> Tuple[List[int], List[int]]:
    base = nfs_dir / f"bench_{run_id}"
    base.mkdir(parents=True, exist_ok=True)

    put_ns: List[int] = []
    get_ns: List[int] = []

    # PUT
    for i, data in enumerate(blobs):
        path = base / f"{i:03d}.bin"
        t0 = time.perf_counter_ns()
        atomic_write(path, data, fsync=fsync)
        t1 = time.perf_counter_ns()
        put_ns.append(t1 - t0)

    # GET
    for i, data in enumerate(blobs):
        path = base / f"{i:03d}.bin"
        t0 = time.perf_counter_ns()
        got = read_bytes(path)
        t1 = time.perf_counter_ns()
        get_ns.append(t1 - t0)
        if got != data:
            raise RuntimeError(f"Filesystem data mismatch at {path}")

    return put_ns, get_ns


def bench_redis(
    redis_url: str, blobs: List[bytes], run_id: str
) -> Tuple[List[int], List[int]]:
    if redis is None:
        raise RuntimeError("redis package not installed. Run: pip install redis")

    r = redis.Redis.from_url(redis_url)
    # sanity check connection
    r.ping()

    prefix = f"bench:{run_id}:"
    keys = [f"{prefix}{i:03d}" for i in range(len(blobs))]

    put_ns: List[int] = []
    get_ns: List[int] = []

    # PUT (SET)
    for k, data in zip(keys, blobs):
        t0 = time.perf_counter_ns()
        r.set(k, data)
        t1 = time.perf_counter_ns()
        put_ns.append(t1 - t0)

    # GET
    for k, data in zip(keys, blobs):
        t0 = time.perf_counter_ns()
        got = r.get(k)
        t1 = time.perf_counter_ns()
        get_ns.append(t1 - t0)
        if got != data:
            raise RuntimeError(f"Redis data mismatch at key {k}")

    return put_ns, get_ns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--nfs-dir",
        type=str,
        required=True,
        help="Path to NFS-mounted directory (e.g. /mnt/nfs/share)",
    )
    ap.add_argument(
        "--redis",
        type=str,
        required=True,
        help="Redis URL (e.g. redis://localhost:6379/0)",
    )
    ap.add_argument(
        "--count", type=int, default=100, help="Number of blobs/files (default: 100)"
    )
    ap.add_argument(
        "--min-kib", type=int, default=1, help="Min size in KiB (default: 1)"
    )
    ap.add_argument(
        "--max-mib", type=int, default=1, help="Max size in MiB (default: 1)"
    )
    ap.add_argument(
        "--seed", type=int, default=12345, help="RNG seed for repeatability"
    )
    ap.add_argument(
        "--warmup", type=int, default=0, help="Warmup iterations (default: 0)"
    )
    ap.add_argument(
        "--fsync", action="store_true", help="Include fsync in filesystem atomic write"
    )
    args = ap.parse_args()

    nfs_dir = Path(args.nfs_dir)
    if not nfs_dir.exists():
        print(f"ERROR: --nfs-dir does not exist: {nfs_dir}", file=sys.stderr)
        return 2

    min_size = args.min_kib * 1024
    max_size = args.max_mib * 1024 * 1024
    if min_size > max_size:
        print("ERROR: min size > max size", file=sys.stderr)
        return 2

    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4)
    print(f"Run id: {run_id}")
    print(
        f"Blobs: {args.count} sizes in [{args.min_kib} KiB, {args.max_mib} MiB], seed={args.seed}"
    )
    print(f"NFS dir: {nfs_dir}")
    print(f"Redis: {args.redis}")
    print(f"Filesystem fsync: {args.fsync}")
    print()

    blobs = gen_blobs(args.count, min_size, max_size, args.seed)

    # Optional warmup (helps page cache / connection warmup)
    for w in range(args.warmup):
        _ = bench_filesystem(
            nfs_dir, blobs[:5], run_id + f"_warmfs{w}", fsync=args.fsync
        )
        _ = bench_redis(args.redis, blobs[:5], run_id + f"_warmr{w}")

    # Filesystem benchmark
    fs_put, fs_get = bench_filesystem(nfs_dir, blobs, run_id, fsync=args.fsync)

    # Redis benchmark
    rd_put, rd_get = bench_redis(args.redis, blobs, run_id)

    total_bytes = sum(len(b) for b in blobs)
    print(f"Total payload: {total_bytes / (1024 * 1024):.2f} MiB")
    print()

    print("Filesystem (NFS) results:")
    print("  " + summarize("PUT", fs_put))
    print("  " + summarize("GET", fs_get))
    print()

    print("Redis results:")
    print("  " + summarize("PUT (SET)", rd_put))
    print("  " + summarize("GET", rd_get))
    print()

    # Simple “who wins” lines (median)
    fs_put_med = stats.median(fs_put)
    fs_get_med = stats.median(fs_get)
    rd_put_med = stats.median(rd_put)
    rd_get_med = stats.median(rd_get)

    print("Medians:")
    print(
        f"  PUT: filesystem={ns_to_ms(int(fs_put_med)):.3f} ms, redis={ns_to_ms(int(rd_put_med)):.3f} ms"
    )
    print(
        f"  GET: filesystem={ns_to_ms(int(fs_get_med)):.3f} ms, redis={ns_to_ms(int(rd_get_med)):.3f} ms"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
