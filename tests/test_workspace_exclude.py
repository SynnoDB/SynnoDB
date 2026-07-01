"""Generated run workspaces are nested git repos; they must be auto-excluded from any
enclosing repo (the SynnoDB checkout) so a `git add -A` never embeds/pushes them."""

from __future__ import annotations

import subprocess

from synnodb.utils.utils import exclude_workspace_from_enclosing_repo


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def test_workspace_excluded_from_enclosing_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    ws = repo / "myrun_workspace"
    ws.mkdir()
    (ws / ".git").mkdir()  # the workspace is itself a (nested) git repo
    (ws / "db_loader.cpp").write_text("// engine")

    # before: the workspace shows up as untracked
    assert "myrun_workspace" in _git(repo, "status", "--short").stdout

    exclude_workspace_from_enclosing_repo(ws)

    # after: excluded locally (not via the tracked .gitignore) and gone from status
    exclude_file = (repo / ".git" / "info" / "exclude").read_text()
    assert "/myrun_workspace/" in exclude_file
    assert not (repo / ".gitignore").exists()  # did not touch the committed ignore file
    assert "myrun_workspace" not in _git(repo, "status", "--short").stdout


def test_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    ws = repo / "run1"
    ws.mkdir()
    exclude_workspace_from_enclosing_repo(ws)
    exclude_workspace_from_enclosing_repo(ws)  # twice
    content = (repo / ".git" / "info" / "exclude").read_text()
    assert content.count("/run1/") == 1


def test_no_enclosing_repo_is_safe(tmp_path):
    # a workspace not inside any git repo: best-effort, no crash, nothing written
    ws = tmp_path / "loose_ws"
    ws.mkdir()
    exclude_workspace_from_enclosing_repo(ws)  # must not raise
