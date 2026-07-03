#!/usr/bin/env python3
"""
Delete snapshot refs listed in snapshots_with_col_files.csv and sync the deletions.

By default this script only prints what it would delete. Pass --apply to delete the
local refs and push matching ref deletions to the remote snapshot cache.
"""

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

from synnodb.synth_framework.git_snapshotter import resolve_snapshot_repo_dir

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CSV_PATH = SCRIPT_DIR / "snapshots_with_col_files.csv"
DEFAULT_CACHE_REPO = os.environ.get(
    "GIT_SNAPSHOTTER_SERVER",
    "git://c01/bespoke_cache.git",
)
SNAPSHOT_REF_PREFIX = "refs/snapshots/"


def git(
    snapshotter_repo: Path,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # Snapshots live in a single shared bare repo (one refs/snapshots/* namespace
    # for all workspaces), so every operation targets it directly - no work tree.
    env = os.environ.copy()
    env["GIT_DIR"] = str(snapshotter_repo)

    return subprocess.run(
        ["git", *args],
        cwd=snapshotter_repo,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        input=input_text,
    )


def read_snapshot_refs(csv_path: Path) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "ref" not in reader.fieldnames:
            raise ValueError(f"{csv_path} must contain a 'ref' column")

        for row_number, row in enumerate(reader, start=2):
            ref = (row.get("ref") or "").strip()
            if not ref:
                continue
            if not ref.startswith(SNAPSHOT_REF_PREFIX):
                raise ValueError(
                    f"Ref on row {row_number} is not a snapshot ref: {ref!r}"
                )
            if ref not in seen:
                refs.append(ref)
                seen.add(ref)

    return refs


def ref_exists_locally(snapshotter_repo: Path, ref: str) -> bool:
    result = git(
        snapshotter_repo,
        "show-ref",
        "--verify",
        "--quiet",
        ref,
        check=False,
    )
    return result.returncode == 0


def delete_local_refs(snapshotter_repo: Path, refs: list[str]) -> tuple[int, int]:
    deleted = 0
    already_missing = 0

    for ref in refs:
        if ref_exists_locally(snapshotter_repo, ref):
            git(snapshotter_repo, "update-ref", "-d", ref)
            deleted += 1
        else:
            already_missing += 1

    return deleted, already_missing


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024.0
    raise AssertionError("unreachable")


def unique_object_size_estimate(
    snapshotter_repo: Path,
    refs_to_delete: list[str],
) -> tuple[int, int, int]:
    existing_refs = [
        ref for ref in refs_to_delete if ref_exists_locally(snapshotter_repo, ref)
    ]
    if not existing_refs:
        return 0, 0, 0

    all_refs_result = git(snapshotter_repo, "for-each-ref", "--format=%(refname)")
    refs_to_delete_set = set(existing_refs)
    refs_to_keep = [
        ref
        for ref in all_refs_result.stdout.splitlines()
        if ref and ref not in refs_to_delete_set
    ]

    rev_list_input = "\n".join(existing_refs + [f"^{ref}" for ref in refs_to_keep])
    rev_list_input += "\n"
    objects_result = git(
        snapshotter_repo,
        "rev-list",
        "--objects",
        "--no-object-names",
        "--stdin",
        input_text=rev_list_input,
    )
    object_ids = sorted(set(objects_result.stdout.splitlines()))
    if not object_ids:
        return 0, 0, 0

    batch_input = "\n".join(object_ids) + "\n"
    batch_result = git(
        snapshotter_repo,
        "cat-file",
        "--batch-check=%(objectname) %(objectsize) %(objectsize:disk)",
        input_text=batch_input,
    )

    logical_size = 0
    disk_size = 0
    object_count = 0
    for line in batch_result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        logical_size += int(parts[1])
        disk_size += int(parts[2])
        object_count += 1

    return object_count, logical_size, disk_size


def remote_refs(
    snapshotter_repo: Path,
    cache_repo: str,
    refs: list[str],
    batch_size: int,
) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(refs), batch_size):
        batch = refs[start : start + batch_size]
        result = git(
            snapshotter_repo,
            "ls-remote",
            cache_repo,
            *batch,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found.add(parts[1])
    return found


def push_remote_deletions(
    snapshotter_repo: Path,
    cache_repo: str,
    refs: list[str],
    batch_size: int,
) -> None:
    for start in range(0, len(refs), batch_size):
        batch = refs[start : start + batch_size]
        deletion_refspecs = [f":{ref}" for ref in batch]
        git(snapshotter_repo, "push", cache_repo, *deletion_refspecs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete snapshot refs listed in snapshots_with_col_files.csv.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"CSV file with a 'ref' column. Default: {DEFAULT_CSV_PATH}",
    )
    parser.add_argument(
        "--cache-repo",
        default=DEFAULT_CACHE_REPO,
        help=(
            "Remote snapshot cache to push deletions to. "
            "Default: GIT_SNAPSHOTTER_SERVER or git://c01/bespoke_cache.git"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of deletion refspecs per git push. Default: 100",
    )
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help="Only delete local refs; do not push remote deletions.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete refs. Without this flag the script is a dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = args.csv.resolve()
    snapshotter_repo = resolve_snapshot_repo_dir()

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if snapshotter_repo is None:
        raise RuntimeError(
            "No shared snapshot repo configured (set SYNNO_DATA_DIR or "
            "SYNNO_SNAPSHOTTER_DIR)."
        )
    if not (snapshotter_repo / "HEAD").is_file():
        raise FileNotFoundError(f"No snapshot repo found at: {snapshotter_repo}")

    refs = read_snapshot_refs(csv_path)
    if not refs:
        print(f"No snapshot refs found in {csv_path}")
        return 0

    existing_count = sum(1 for ref in refs if ref_exists_locally(snapshotter_repo, ref))
    missing_count = len(refs) - existing_count
    object_count, logical_size, disk_size = unique_object_size_estimate(
        snapshotter_repo,
        refs,
    )

    print(f"CSV: {csv_path}")
    print(f"Snapshot repo: {snapshotter_repo}")
    print(f"Snapshot refs in CSV: {len(refs)}")
    print(f"Local refs found: {existing_count}")
    print(f"Local refs already missing: {missing_count}")
    print(f"Unique local objects reachable only from these refs: {object_count}")
    print(f"Estimated unique local object payload: {human_bytes(logical_size)}")
    print(f"Estimated unique local object disk usage: {human_bytes(disk_size)}")
    found_remote: set[str] = set()
    if args.no_remote:
        print("Remote sync: disabled")
    else:
        print(f"Remote sync: push deletions to {args.cache_repo}")
        found_remote = remote_refs(
            snapshotter_repo,
            args.cache_repo,
            refs,
            args.batch_size,
        )
        print(f"Remote refs found: {len(found_remote)}")
        print(f"Remote refs already missing: {len(refs) - len(found_remote)}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to delete these snapshots.")
        print("First refs that would be deleted:")
        for ref in refs[:10]:
            print(f"  {ref}")
        if len(refs) > 10:
            print(f"  ... and {len(refs) - 10} more")
        return 0

    deleted_count, already_missing_count = delete_local_refs(snapshotter_repo, refs)
    print(f"\nDeleted local refs: {deleted_count}")
    print(f"Already missing locally: {already_missing_count}")

    if not args.no_remote:
        refs_on_remote = [r for r in refs if r in found_remote]
        if refs_on_remote:
            push_remote_deletions(
                snapshotter_repo,
                args.cache_repo,
                refs_on_remote,
                args.batch_size,
            )
        print(f"Pushed remote deletions for {len(refs_on_remote)} refs.")

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout.strip(), file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        raise SystemExit(exc.returncode)
