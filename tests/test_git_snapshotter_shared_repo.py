"""Snapshotter behaviour when many workspaces share one bare repo as linked
worktrees: shared objects/refs (cross-workspace cache hits) but isolated
HEAD/index (no cross-corruption).
The autouse ``_isolate_snapshotter_repo`` fixture pins a per-test repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from synnodb.synth_framework.git_snapshotter import (
    GitSnapshotter,
    resolve_snapshot_repo_dir,
)


def _git(git_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_workspace(root: Path, name: str, files: dict[str, str]) -> Path:
    ws = root / name
    ws.mkdir(parents=True)
    for rel, content in files.items():
        (ws / rel).write_text(content)
    return ws


def test_repo_is_shared_bare_and_workspace_holds_only_a_gitfile(tmp_path):
    ws = _make_workspace(tmp_path, "ws", {"a.txt": "hello"})
    snap = GitSnapshotter(working_dir=ws)

    repo = resolve_snapshot_repo_dir()
    assert repo is not None
    assert snap._shared is True
    # The shared repo is bare and lives at the configured location...
    assert (repo / "HEAD").is_file()
    assert _git(repo, "rev-parse", "--is-bare-repository") == "true"
    # ...the workspace only carries a lightweight gitfile pointing at its worktree.
    gitfile = ws / ".git"
    assert gitfile.is_file()
    assert gitfile.read_text().strip() == f"gitdir: {snap.git_dir}"
    assert snap.git_dir == repo / "worktrees" / snap._workspace_key


def test_two_workspaces_share_objects_and_refs_but_isolate_working_state(tmp_path):
    ws_a = _make_workspace(tmp_path, "wsA", {"a.txt": "A", "junk.tmp": "x"})
    ws_b = _make_workspace(tmp_path, "wsB", {"b.txt": "B", "keep.tmp": "y"})

    # Different ignore rules per workspace must not leak between them.
    snap_a = GitSnapshotter(working_dir=ws_a, extra_gitignore=["*.tmp"])
    snap_b = GitSnapshotter(working_dir=ws_b, extra_gitignore=["unrelated"])

    _, commit_a = snap_a.snapshot("state-a")
    parent_b, commit_b = snap_b.snapshot("state-b")

    assert commit_a is not None and commit_b is not None
    # B is a fresh worktree: its first snapshot has NO parent (it must not chain
    # onto A's commit, which would overwrite B's working tree).
    assert parent_b is None

    files_a = _git(snap_a.git_dir, "ls-tree", "-r", "--name-only", commit_a).split()
    files_b = _git(snap_b.git_dir, "ls-tree", "-r", "--name-only", commit_b).split()
    assert files_a == ["a.txt"]  # junk.tmp ignored by A's rule
    assert sorted(files_b) == [
        "b.txt",
        "keep.tmp",
    ]  # A's *.tmp rule does not apply to B

    # refs/snapshots/* is one shared namespace, visible from either worktree.
    refs = _git(snap_a.git_dir, "for-each-ref", "--format=%(refname)", "refs/snapshots")
    assert "refs/snapshots/snapshot-state-a" in refs
    assert "refs/snapshots/snapshot-state-b" in refs

    # Objects are shared: B can restore a commit A produced (a cross-workspace hit).
    assert snap_b.has_snapshot(commit_a)
    snap_b.restore(commit_a)
    assert (ws_b / "a.txt").read_text() == "A"
    assert not (ws_b / "b.txt").exists()

    # A is entirely untouched by B's activity.
    assert (ws_a / "a.txt").read_text() == "A"
    assert _git(snap_a.git_dir, "rev-parse", "HEAD") == commit_a


def test_debug_logs_are_auto_excluded_from_snapshots(tmp_path):
    # debug_logs/ is written by the framework (DebugLogger) but must never be
    # snapshotted; the snapshotter ignores it automatically, without the caller
    # having to list it in extra_gitignore.
    ws = _make_workspace(tmp_path, "ws", {"a.txt": "keep"})
    (ws / "debug_logs").mkdir()
    (ws / "debug_logs" / "trace.log").write_text("noisy")

    snap = GitSnapshotter(working_dir=ws)

    _, commit = snap.snapshot("state")
    assert commit is not None
    files = _git(snap.git_dir, "ls-tree", "-r", "--name-only", commit).split()
    assert files == ["a.txt"]  # debug_logs/ absent from the snapshot tree

    # With everything committed, the still-present (ignored) debug_logs/ must not
    # register as a dirty working tree.
    dirty, _ = snap.is_dirty()
    assert not dirty

    # A plain `git status` a user runs by hand (no snapshotter env, so no
    # core.excludesFile) must still ignore debug_logs/, via the shared info/exclude.
    plain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "debug_logs" not in plain.stdout


def test_recreated_workspace_resumes_its_own_snapshot_line(tmp_path):
    ws = _make_workspace(tmp_path, "ws", {"a.txt": "v1"})
    snap = GitSnapshotter(working_dir=ws)
    _, commit = snap.snapshot("v1")
    git_dir = snap.git_dir

    # A second snapshotter over the same workspace path reuses the same worktree
    # and resumes from the same HEAD.
    snap2 = GitSnapshotter(working_dir=ws)
    assert snap2.git_dir == git_dir
    assert _git(git_dir, "rev-parse", "HEAD") == commit
