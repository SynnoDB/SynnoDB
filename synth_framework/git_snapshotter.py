import getpass
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Tuple
from urllib.parse import urlparse

from observability.logging.notify import send_notification
from utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)

SNAPSHOT_REF_PREFIX = "refs/snapshots"
SNAPSHOT_REF_GLOB = f"{SNAPSHOT_REF_PREFIX}/*"


class GitSnapshotter:
    def __init__(
        self,
        working_dir: Path,
        cache_repo: str | None = None,
        extra_gitignore: Iterable[str] | None = None,
        do_not_snapshot: bool = False,
        exclude_files: set[
            str
        ] = set(),  # files to exclude from snapshots, filename (relative to working_dir)
    ):
        self.working_dir = working_dir.resolve()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.current_hash: str | None = None
        self.do_not_snapshot = do_not_snapshot
        # Number of new commits created via snapshot() since the last push.
        # Used by maybe_push_snapshots() to throttle network round-trips.
        self._unpushed_new_snapshots: int = 0

        self.exclude_files: set[str] = exclude_files

        # If there's already a repo here, verify it's rooted HERE (not a parent).
        # If not, initialize a new repo here.
        if self._has_git_dir_here():
            self._pin_repo_env()
            self._assert_repo_root_is_working_dir()
        else:
            # Create an independent repo here.
            # Ensure we don't accidentally "discover" a parent repo when calling git.
            self._git_raw(["init"])
            self._pin_repo_env()
            self._assert_repo_root_is_working_dir()

        # Minimal identity for commits
        username = getpass.getuser()
        self._git(["config", "user.name", username])
        self._git(["config", "user.email", "llm@local"])

        self._write_extra_gitignore(extra_gitignore)

        self.cache_repo: str | None = None
        if cache_repo is not None:
            self.cache_repo = self._configure_root_remote(cache_repo, "cache_repo")
            self.fetch_snapshots()

    def snapshot(
        self, name: str, reuse_parent_if_unchanged: bool = True
    ) -> Tuple[str | None, str | None]:
        """
        Creates a snapshot commit.
        If `reuse_parent_if_unchanged` is True (default) and the staged tree
        is identical to the parent commit, no new commit is created; the new
        snapshot ref is pointed at the parent and (parent, parent) is returned.
        Returns (parent_hash, new_hash).
        """

        assert not self.do_not_snapshot, (
            "Snapshotting is disabled (do_not_snapshot=True) - this is a sanity check. somewhere snapshot was called, although it shouldnt!"
        )

        safe = self._sanitize_ref_component(f"snapshot-{name}")
        assert self.is_snapshot_name_unique(safe), (
            f'Snapshot with name "{name}" already exists'
        )
        parent = self._head_hash(allow_none=True)

        if parent is not None:
            self._git(["switch", "--detach", parent])

        # untrack excluded files (if any) so they don't affect the snapshot hash or cause dirty state
        # only remove from index, keep on disk
        ex = self.exclude_files
        if len(ex) > 0:
            self._git(["rm", "-r", "--cached", "--ignore-unmatch", "--"] + list(ex))

        self._git(["add", "-A"])

        # If the staged tree matches the parent, reuse the parent commit
        # instead of creating an empty commit with a new hash.
        if reuse_parent_if_unchanged and parent is not None:
            diff = self._git_quiet(["diff", "--cached", "--quiet", "HEAD"], check=False)
            if diff.returncode == 0:
                self._git(["update-ref", self._snapshot_ref(safe), parent])
                self.current_hash = parent
                logger.debug(f"No changes; reusing parent snapshot: {parent}")
                return parent, parent

        self._git(
            [
                "commit",
                "--allow-empty",
                "-m",
                f"Snapshot {name.strip()}",
            ]
        )

        new = self._head_hash()
        self._git(["update-ref", self._snapshot_ref(safe), new])

        self.current_hash = new

        logger.debug(f"Created snapshot: {new}")

        # A new commit was created; opportunistically push if we've
        # accumulated enough unpushed snapshots since the last push.
        self._unpushed_new_snapshots += 1
        self.maybe_push_snapshots()

        return parent, new

    def restore(
        self,
        commit_hash: str,
    ) -> None:
        """
        Restores the working directory to the given commit.
        """
        try:
            self._git(["switch", "--detach", "--quiet", commit_hash])
        except RuntimeError as exc:
            send_notification(
                f"Awaiting user confirmation to restore snapshot. Git checkout failed for snapshot {commit_hash}. This may be due to uncommitted changes. Waiting for user confirmation to discard local changes and continue...",
            )
            if not await_user_confirmation(
                "Git checkout failed. This may be due to uncommitted changes. Do you want to discard local changes and continue?"
            ):
                raise exc

            self.clear_untracked()
            self.reset_changes()

            # try again
            self._git(["switch", "--detach", "--quiet", commit_hash])

        self._git(["reset", "--hard", "--quiet", commit_hash])

        self.current_hash = commit_hash

        logger.debug(f"Restored snapshot: {commit_hash}")

    def is_dirty(self) -> tuple[bool, str]:
        """
        Returns True if there are uncommitted changes in the working directory.
        Includes staged, unstaged, and untracked files (except ignored ones).
        Read-only files are excluded from the check.
        """
        result = self._git_capture(["status", "--porcelain"], check=False)
        if len(self.exclude_files) == 0:
            return bool(result.stdout.strip()), result.stdout.strip()

        filtered = []
        for line in result.stdout.splitlines():
            assert line.startswith((" M ", " D ", " A ", "?? ")), (
                f"Unexpected git status line: '{line}'"
            )
            # skip status code (first 3 chars) to get file path, and filter out excluded files
            line = line[3:].strip()
            if line != "" and line not in self.exclude_files:
                filtered.append(line)

        filtered_str = "\n".join(filtered)
        return bool(filtered_str), filtered_str

    def clear_untracked(self, include_ignored: bool = False) -> None:
        """
        Delete files/dirs that are not tracked by git in this repo.

        - include_ignored=False: removes untracked files/dirs, keeps ignored files.
        - include_ignored=True: also removes ignored files (like build artifacts, caches).

        Files in exclude_files are never removed regardless of include_ignored.
        """
        args = ["clean", "-fd"]
        if include_ignored:
            args.append("-x")  # remove ignored files too
        for f in self.exclude_files:
            args += ["-e", f]
        self._git(args)

    def reset_changes(self) -> None:
        """
        Discard all local modifications to tracked files.
        Does NOT remove untracked or ignored files.
        Equivalent to: git reset --hard HEAD
        """
        self._git(["reset", "--hard", "HEAD"])

    def matches_snapshot(self, commit_hash: str) -> bool:
        """
        Returns True iff the current working directory exactly matches
        the given snapshot commit:
        - files tracked in commit_hash match filesystem contents
        - no extra untracked (non-ignored) files exist
        """

        # 1) Get files tracked in the snapshot commit (excluding excluded)
        tree = self._git_capture(["ls-tree", "-r", "--name-only", commit_hash])
        tracked_files = [
            p for p in tree.stdout.splitlines() if p and p not in self.exclude_files
        ]

        try:
            # 2) Intent-to-add files that exist on disk but are untracked in HEAD
            for path in tracked_files:
                self._git_quiet(["add", "-N", "--", path], check=False)

            # 3) Compare snapshot commit against working tree
            diff = self._git_quiet(
                ["diff", "--quiet", commit_hash, "--", "."], check=False
            )
            if diff.returncode != 0:
                return False

            # 4) Ensure no extra untracked (non-ignored) files exist (excluding read-only)
            untracked = self._git_capture(
                ["ls-files", "--others", "--exclude-standard"]
            )
            untracked_files = [
                p
                for p in untracked.stdout.splitlines()
                if p and p not in self.exclude_files
            ]
            return not bool(untracked_files)

        finally:
            # 5) Clean index side-effects (remove intent-to-add entries)
            self._git_quiet(["reset", "--quiet"], check=False)

    def has_snapshot(self, commit_hash: str) -> bool:
        """
        Returns True iff the given commit hash exists in this repository.
        This checks object existence, not whether a ref points to it.
        """
        result = self._git_quiet(
            ["cat-file", "-e", f"{commit_hash}^{{commit}}"], check=False
        )
        return result.returncode == 0

    def is_snapshot_name_unique(self, name: str) -> bool:
        """
        Returns True iff refs/snapshots/<name> does not already exist.
        """
        ref = f"refs/snapshots/{name}"
        ref = self._snapshot_ref(name)

        result = self._git_quiet(["show-ref", "--verify", "--quiet", ref], check=False)
        return result.returncode != 0

    def create_empty_snapshot(self, name: str) -> str:
        """
        Creates an empty commit (no files at all), anchors it at refs/snapshots/<name>,
        records a reflog message, and CHECKS IT OUT so the repo ends in empty state.

        This will:
        - remove untracked files (via clear_untracked)
        - reset tracked files away (via reset --hard) when checking out the empty commit
        - move HEAD (so it appears in `git log --reflog --oneline`)
        """
        safe = self._sanitize_ref_component(f"empty-{name}")

        # Remove untracked stuff first (keeps ignored by default)
        self.clear_untracked(include_ignored=True)

        # Reuse an existing empty snapshot of the same name so caches keyed on
        # the commit hash stay valid across runs.
        if not self.is_snapshot_name_unique(safe):
            existing = self._git_capture(
                ["rev-parse", "--verify", f"{self._snapshot_ref(safe)}^{{commit}}"]
            ).stdout.strip()
            self.restore(existing)
            return existing

        # Ensure reflogs are recorded for ref updates
        self._git(["config", "core.logAllRefUpdates", "true"])

        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        # Create commit directly from the empty tree
        result = self._git_capture(
            ["commit-tree", EMPTY_TREE, "-m", f"Empty Snapshot {name}".strip()]
        )
        commit_hash = result.stdout.strip()

        # Anchor snapshot ref + reflog message for that ref
        self._git(
            [
                "update-ref",
                "-m",
                f"create empty snapshot {name}",
                self._snapshot_ref(safe),
                commit_hash,
            ]
        )

        # CHECK IT OUT (detach HEAD) and enforce empty working tree state
        self._git(["switch", "--detach", commit_hash])
        self._git(["reset", "--hard", commit_hash])

        # Record current hash
        self.current_hash = commit_hash

        return commit_hash

    def print_tree(self) -> None:
        size = shutil.get_terminal_size(fallback=(120, 40))
        use_compact = size.columns < 120 or size.lines < 20
        args = ["log", "--oneline", "--decorate", "--all", "--date-order"]
        if not use_compact:
            args.insert(1, "--graph")
        if use_compact:
            args.insert(0, "--no-pager")
        self._git_run(args, check=True, passthrough=True)

    def push_snapshots(self) -> None:
        """
        Push all snapshot refs to the root repo (same namespace).
        If `root` is None, uses self.root (set via __init__).
        """
        if self.cache_repo is None:
            return

        try:
            self._git_run(
                ["push", self.cache_repo, f"{SNAPSHOT_REF_GLOB}:{SNAPSHOT_REF_GLOB}"]
            )
        except Exception as exc:
            logger.error(
                f"Failed to push snapshots to cache repo '{self.cache_repo}': {exc}"
            )
            return

    def maybe_push_snapshots(
        self, every_n_new_snapshots: int = 100, force: bool = False
    ) -> None:
        """
        Throttled wrapper around push_snapshots. Pushes when at least
        `every_n_new_snapshots` new commits have been created via snapshot()
        since the last push, or when `force` is set. No-op if no cache_repo
        is configured.
        """
        if self.cache_repo is None:
            return
        if force or self._unpushed_new_snapshots >= every_n_new_snapshots:
            self.push_snapshots()
            self._unpushed_new_snapshots = 0

    def fetch_snapshots(self) -> None:
        """
        Fetch all snapshot refs from the root repo (same namespace).
        If `root` is None, uses self.root (set via __init__).
        """
        if self.cache_repo is None:
            return

        logger.debug(f"Fetching snapshots from cache repo '{self.cache_repo}'...")
        self._git_run(
            ["fetch", self.cache_repo, f"{SNAPSHOT_REF_GLOB}:{SNAPSHOT_REF_GLOB}"]
        )

    # ---------- git + safety helpers ----------

    def _configure_root_remote(self, root_repo: str, remote_name: str) -> str:
        resolved = None

        path = Path(root_repo).expanduser()
        if path.exists():
            resolved = path.resolve().as_uri()
        else:
            parsed = urlparse(root_repo)
            if parsed.scheme:
                resolved = root_repo
            elif self._remote_exists(root_repo):
                return root_repo
            else:
                raise ValueError(
                    f"root_repo '{root_repo}' is neither a filesystem path, "
                    f"a valid URL, nor an existing remote name"
                )

        if self._remote_exists(remote_name):
            self._git(["remote", "set-url", remote_name, resolved])
        else:
            self._git(["remote", "add", remote_name, resolved])

        return remote_name

    def _remote_exists(self, name: str) -> bool:
        r = self._git_quiet(["remote", "get-url", name], check=False)
        return r.returncode == 0

    def _sanitize_ref_component(self, name: str) -> str:
        """
        Convert an arbitrary snapshot name into a valid single ref path component.

        - replaces whitespace with '-'
        - removes disallowed characters
        - avoids forbidden sequences
        """
        name = name.strip()
        name = re.sub(r"\s+", "-", name)  # spaces -> -
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)  # keep safe chars
        name = re.sub(r"-{2,}", "-", name).strip("-.")  # tidy

        if not name:
            name = "snapshot"

        # Disallow some ref edge cases
        forbidden = (
            name.startswith(".")
            or name.endswith(".")
            or name.endswith(".lock")
            or ".." in name
            or "@{" in name
        )
        if forbidden:
            name = re.sub(r"\.+", ".", name).strip(".")
            name = name.replace("@{", "-")
            if not name or name.endswith(".lock"):
                name = "snapshot"

        return name

    def _has_git_dir_here(self) -> bool:
        """
        True if working_dir has its own .git (directory or gitfile).
        This is the key signal that a repo is rooted here, not only in a parent.
        """
        git_path = self.working_dir / ".git"
        return git_path.exists()

    def _pin_repo_env(self) -> None:
        """
        Force git to use ONLY this repo (no parent discovery).
        Supports normal repos and gitdir 'gitfile' (e.g., worktrees/submodules).
        """
        git_path = self.working_dir / ".git"
        self._env = os.environ.copy()
        self._env["GIT_WORK_TREE"] = str(self.working_dir)

        if git_path.is_file():
            # .git is a "gitfile": contains "gitdir: /actual/path"
            content = git_path.read_text(encoding="utf-8", errors="replace").strip()
            prefix = "gitdir:"
            if not content.lower().startswith(prefix):
                raise RuntimeError(f"Invalid .git file at {git_path}")
            gitdir = content[len(prefix) :].strip()
            gitdir_path = (self.working_dir / gitdir).resolve()
            self._env["GIT_DIR"] = str(gitdir_path)
        else:
            # normal .git directory
            self._env["GIT_DIR"] = str(git_path)

        # Also prevent upward discovery if something goes odd
        self._env["GIT_CEILING_DIRECTORIES"] = str(self.working_dir)

    def _assert_repo_root_is_working_dir(self) -> None:
        """
        Verify that git sees the repo root as working_dir.
        This catches the case where we accidentally target a parent repo.
        """
        # With pinned env, this should always match working_dir
        result = self._git_capture(["rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Git repo check failed: {result.stderr.strip()}")

        toplevel = Path(result.stdout.strip()).resolve()
        if toplevel != self.working_dir:
            raise RuntimeError(
                f"Refusing to use repo rooted at {toplevel}; expected {self.working_dir}. "
                f"This usually means a parent repo is being picked up."
            )

    def _git_raw(self, args):
        """
        Git calls before repo pinning. We still prevent parent discovery by ceiling.
        """
        env = os.environ.copy()
        env["GIT_CEILING_DIRECTORIES"] = str(self.working_dir)
        self._git_run_env(args, env=env, check=True)

    def _git(self, args):
        """
        Git calls pinned to this repo (no parent discovery possible).
        """
        self._git_run(args, check=True)

    def _head_hash(self, allow_none: bool = False) -> str | None:
        result = self._git_capture(["rev-parse", "HEAD"], check=False)
        if result.returncode != 0:
            if allow_none:
                return None
            raise RuntimeError("HEAD does not exist")
        return result.stdout.strip()

    def _snapshot_ref(self, name: str) -> str:
        return f"{SNAPSHOT_REF_PREFIX}/{name}"

    def _git_capture(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self._git_run(args, check=check, capture=True)

    def _git_quiet(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self._git_run(args, check=check)

    def _git_run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if capture:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
        elif passthrough:
            stdout = None
            stderr = None
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        return self._git_run_env(
            args,
            env=self._env,
            check=check,
            stdout=stdout,
            stderr=stderr,
        )

    def _git_run_env(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        check: bool,
        stdout: int | None = subprocess.DEVNULL,
        stderr: int | None = subprocess.DEVNULL,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=self.working_dir,
                env=env,
                check=check,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            cmd = " ".join(exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
            parts = [f"git failed: {cmd}"]
            if exc.stdout:
                parts.append(str(exc.stdout).strip())
            if exc.stderr:
                parts.append(str(exc.stderr).strip())
            raise RuntimeError("\n".join(parts)) from exc

    def _write_extra_gitignore(self, patterns: Iterable[str] | None) -> None:
        """
        Writes ignore patterns and excluded file paths to .git/info/exclude
        (applies in addition to .gitignore).
        Overwrites the file each time so stale patterns from previous runs don't accumulate.
        """
        exclude = self.working_dir / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        if patterns:
            lines += [p.strip() for p in patterns if p.strip()]
        lines += self.exclude_files

        with exclude.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
