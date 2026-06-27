#!/usr/bin/env python3
"""
Fetch git snapshots from the last 5 days and report any that contain .col files,
.column_store directories, or exceed the configured total snapshot size threshold.

Uses the same git server and working dir as prepare_for_export.ipynb.
"""

import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, Path(__file__).parent.parent.parent.as_posix())

from synnodb.synth_framework.git_snapshotter import GitSnapshotter

SCRIPT_DIR = Path(__file__).parent
# working_dir = SCRIPT_DIR.parent / "prepare_code_for_export" / "output"
working_dir = SCRIPT_DIR.parent.parent / "output"
assert working_dir.is_dir(), f"Working dir {working_dir} does not exist"

print(f"Initialising snapshotter (working dir: {working_dir}) ...")
snapshotter = GitSnapshotter(
    cache_repo="git://c01/bespoke_cache.git",
    working_dir=working_dir,
    extra_gitignore=[],
)

env = snapshotter._env


def git(*args: str) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        env=env,
        cwd=working_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


DAYS = 31
include_also_large = False
MAX_SNAPSHOT_SIZE_BYTES = 512 * 1024
cutoff_ts = time.time() - DAYS * 24 * 3600

refs_raw = git(
    "for-each-ref",
    "--format=%(refname) %(creatordate:unix)",
    "refs/snapshots/",
)

recent: list[tuple[str, int]] = []
for line in refs_raw.splitlines():
    line = line.strip()
    if not line:
        continue
    ref, ts_str = line.rsplit(" ", 1)
    ts = int(ts_str)
    if ts >= cutoff_ts:
        recent.append((ref, ts))

recent.sort(key=lambda x: x[1])
print(f"Found {len(recent)} snapshot(s) from the last {DAYS} days.\n")

flagged_snapshots: list[tuple[str, int, str, list[tuple[str, int]], int]] = []

for ref, ts in tqdm(recent):
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    commit_hash = git("rev-parse", ref)

    ls_output = git("ls-tree", "-r", "-l", commit_hash)

    column_store_files: list[tuple[str, int]] = []
    snapshot_total_bytes = 0
    for line in ls_output.splitlines():
        # format: <mode> SP <type> SP <object> SP <size> TAB <path>
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta, name = parts
        meta_parts = meta.split()
        size = int(meta_parts[3]) if meta_parts[3] != "-" else 0
        snapshot_total_bytes += size
        if name.endswith(".col") or ".column_storage/" in f"/{name}":
            column_store_files.append((name, size))

    exceeds_size_limit = snapshot_total_bytes > MAX_SNAPSHOT_SIZE_BYTES
    if column_store_files or (include_also_large and exceeds_size_limit):
        flagged_snapshots.append(
            (ref, ts, commit_hash, column_store_files, snapshot_total_bytes)
        )
        total_mb = sum(s for _, s in column_store_files) / (1024 * 1024)
        snapshot_total_mb = snapshot_total_bytes / (1024 * 1024)
        print(f"[{date_str}] {ref.split('/')[-1]}")
        print(f"  commit : {commit_hash}")
        if include_also_large:
            print(f"  snapshot size: {snapshot_total_mb:.1f} MB")
        if include_also_large and exceeds_size_limit:
            print("  exceeds snapshot size limit: yes")
        print(
            f"  .col/.column_store files: {len(column_store_files)}, "
            f"total size: {total_mb:.1f} MB"
        )
        print()
    # else:
    #     print(f"[{date_str}] {ref.split('/')[-1]}  — no column-store files and under size limit")

print()
if flagged_snapshots:
    if include_also_large:
        print(
            f"==> {len(flagged_snapshots)} snapshot(s) contain column-store files "
            f"or exceed {MAX_SNAPSHOT_SIZE_BYTES / (1024 * 1024):.1f} MB:"
        )
    else:
        print(f"==> {len(flagged_snapshots)} snapshot(s) contain column-store files:")
    for (
        ref,
        ts,
        commit_hash,
        column_store_files,
        snapshot_total_bytes,
    ) in flagged_snapshots:
        total_mb = sum(s for _, s in column_store_files) / (1024 * 1024)
        snapshot_total_mb = snapshot_total_bytes / (1024 * 1024)
        if include_also_large:
            print(
                f"  {commit_hash}  "
                f"({snapshot_total_mb:.1f} MB total, "
                f"{total_mb:.1f} MB column-store data)  "
                f"{ref}"
            )
        else:
            print(f"  {commit_hash}  ({total_mb:.1f} MB column-store data)  {ref}")
else:
    if include_also_large:
        print(
            "==> No snapshots with column-store files or "
            f"> {MAX_SNAPSHOT_SIZE_BYTES / (1024 * 1024):.1f} MB total size found "
            f"in the last {DAYS} days."
        )
    else:
        print(
            f"==> No snapshots with column-store files found in the last {DAYS} days."
        )

out_path = SCRIPT_DIR / "snapshots_with_col_files.csv"
with out_path.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(
        [
            "ref",
            "ref_short",
            "commit_hash",
            "created_ts",
            "created_date",
            "snapshot_total_bytes",
            "snapshot_total_mb",
            "exceeds_1mb",
            "column_store_file_count",
            "column_store_total_bytes",
            "column_store_total_mb",
        ]
    )
    for (
        ref,
        ts,
        commit_hash,
        column_store_files,
        snapshot_total_bytes,
    ) in flagged_snapshots:
        column_store_total_bytes = sum(s for _, s in column_store_files)
        writer.writerow(
            [
                ref,
                ref.split("/")[-1],
                commit_hash,
                ts,
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                snapshot_total_bytes,
                round(snapshot_total_bytes / (1024 * 1024), 3),
                snapshot_total_bytes > MAX_SNAPSHOT_SIZE_BYTES,
                len(column_store_files),
                column_store_total_bytes,
                round(column_store_total_bytes / (1024 * 1024), 3),
            ]
        )
print(f"\nWrote {len(flagged_snapshots)} record(s) to {out_path}")
