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


def _plain_status(ws: Path) -> str:
    """``git status --porcelain`` as a user would run it by hand in the
    workspace: no snapshotter env, so no core.excludesFile - what it ignores,
    it ignores via the shared info/exclude."""
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


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

    # A hand-run `git status` must still ignore debug_logs/ (via info/exclude).
    assert "debug_logs" not in _plain_status(ws)


def test_result_files_are_auto_excluded_from_snapshots(tmp_path):
    # result*.arrow / result*.csv are per-execution engine outputs; see the
    # RESULT_FILE_PATTERNS rationale in git_snapshotter.py.
    ws = _make_workspace(tmp_path, "ws", {"a.txt": "keep"})
    results = ws / "results"
    results.mkdir()
    (results / "result_req_1_abc123.arrow").write_text("binary-ish")
    (ws / "result_req_2_def456.csv").write_text("1,2,3")

    snap = GitSnapshotter(working_dir=ws)

    _, commit = snap.snapshot("state")
    assert commit is not None
    files = _git(snap.git_dir, "ls-tree", "-r", "--name-only", commit).split()
    assert files == ["a.txt"]  # result files absent from the snapshot tree

    dirty, _ = snap.is_dirty()
    assert not dirty  # ignored result files do not register as dirty

    # An execution that only produced new result files must not mint a new
    # snapshot: the tree is unchanged, so the parent commit is reused and
    # live runs converge with cache replays on the same hash.
    (results / "result_req_1_zzz999.arrow").write_text("other run")
    parent, again = snap.snapshot("after-rerun")
    assert (parent, again) == (commit, commit)

    # A hand-run `git status` must also ignore them (via info/exclude).
    assert "result_req" not in _plain_status(ws)


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


def test_first_snapshot_leaves_no_bootstrap_branch(tmp_path):
    # A fresh worktree starts on an unborn bootstrap branch (refs/heads/synno/*)
    # so its first snapshot has no parent. Committing materializes that branch as
    # a side effect; the snapshotter must drop it so snapshots stay anchored
    # solely by refs/snapshots/* and the first commit does not linger outside the
    # snapshot namespace past ref cleanup / GC.
    ws = _make_workspace(tmp_path, "ws", {"a.txt": "v1"})
    snap = GitSnapshotter(working_dir=ws)
    repo = resolve_snapshot_repo_dir()
    assert repo is not None

    parent, first = snap.snapshot("first")
    assert parent is None  # genuinely the first commit on an unborn HEAD
    assert first is not None
    (ws / "a.txt").write_text("v2")
    snap.snapshot("second")

    # No refs/heads/* survive: the bootstrap branch was cleaned up.
    heads = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/")
    assert heads == ""

    # The first commit is anchored ONLY within the snapshot namespace. (It stays
    # reachable as the ancestor of later snapshots - that is a normal chain, not
    # a leak; what must not exist is a stray branch pinning it outside
    # refs/snapshots/*, which would survive snapshot-ref cleanup and GC.)
    pointing = _git(
        repo, "for-each-ref", "--points-at", first, "--format=%(refname)"
    ).split()
    assert pointing == ["refs/snapshots/snapshot-first"]
