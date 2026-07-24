import getpass
import hashlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Tuple
from urllib.parse import urlparse

from synnodb import settings
from synnodb.observability.logging.notify import send_notification
from synnodb.utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)

SNAPSHOT_REF_PREFIX = "refs/snapshots"
SNAPSHOT_REF_GLOB = f"{SNAPSHOT_REF_PREFIX}/*"

RESULT_FILE_PATTERNS: tuple[str, ...] = ("result*.arrow", "result*.csv")

# Workspace files the framework writes but must never be part of a snapshot
# (debug_logs/ from DebugLogger, engine result files). Ignored automatically
# for every workspace, in addition to any caller-supplied `extra_gitignore`.
ALWAYS_IGNORED: tuple[str, ...] = ("/debug_logs/", *RESULT_FILE_PATTERNS)


def resolve_snapshot_repo_dir() -> Path | None:
    """The single bare repo (shared object store + ``refs/snapshots/*``) all
    workspaces snapshot into. ``None`` when no data dir is configured."""
    return settings.get_snapshotter_dir()


def _workspace_key(working_dir: Path) -> str:
    """Stable, filesystem-safe worktree id derived from the workspace path."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", working_dir.name).strip("-.") or "workspace"
    digest = hashlib.sha1(str(working_dir).encode("utf-8")).hexdigest()[:12]
    return f"{name}-{digest}"


def resolve_git_dir(working_dir: Path, git_dir: Path | None = None) -> Path:
    """The ``GIT_DIR`` a workspace's git commands use: its linked-worktree admin
    dir ``<repo>/worktrees/<key>`` (own HEAD/index, shared objects/refs via
    ``commondir``). An explicit ``git_dir`` wins; with no shared repo it falls
    back to a workspace-local ``.git``. Single source of truth for the location.
    """
    if git_dir is not None:
        return git_dir.resolve()
    working_dir = working_dir.resolve()
    snapshotter_dir = resolve_snapshot_repo_dir()
    if snapshotter_dir is None:
        return working_dir / ".git"
    return (snapshotter_dir / "worktrees" / _workspace_key(working_dir)).resolve()


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
        git_dir: Path | None = None,
    ):
        self.working_dir = working_dir.resolve()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        # This workspace is a linked worktree of the shared repo: `git_dir` is
        # its worktree admin dir (own HEAD/index), `_common_dir` the shared repo.
        self.git_dir = resolve_git_dir(self.working_dir, git_dir)
        repo = resolve_snapshot_repo_dir()
        if git_dir is None and repo is not None:
            self._shared = True
            self._common_dir = repo
        else:
            self._shared = False
            self._common_dir = self.git_dir
        self._workspace_key = _workspace_key(self.working_dir)
        self.current_hash: str | None = None
        self.do_not_snapshot = do_not_snapshot
        # Number of new commits created via snapshot() since the last push.
        # Used by maybe_push_snapshots() to throttle network round-trips.
        self._unpushed_new_snapshots: int = 0

        self.exclude_files: set[str] = exclude_files

        # Pin git to this worktree (git dir + work tree) so a parent repo is
        # never discovered, then create the shared repo + this worktree on first use.
        self._pin_repo_env()
        self._ensure_repo()

        self._write_common_excludes()
        self._write_extra_gitignore(extra_gitignore)

        # ``cache_repo`` is the git remote *name* used for fetch/push;
        # ``cache_repo_url`` is the resolved location it points at (kept for
        # logging, since the remote name alone is opaque).
        self.cache_repo: str | None = None
        self.cache_repo_url: str | None = None
        if cache_repo is not None:
            self.cache_repo = self._configure_root_remote(cache_repo, "cache_repo")
            self.cache_repo_url = self._remote_url(self.cache_repo)
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

        if parent is None:
            # The first commit landed on the unborn bootstrap branch
            # (`_wt_branch`), which Git created as a side effect of committing.
            # Detach HEAD onto the commit and drop that branch so snapshots stay
            # anchored solely by refs/snapshots/* (the design invariant);
            # otherwise it pins the first commit outside the snapshot namespace
            # and survives snapshot-ref cleanup and GC. Detach before deleting -
            # Git refuses to delete a branch that is still checked out.
            self._git(["switch", "--detach", new])
            self._git_run(["update-ref", "-d", self._wt_branch()], check=False)

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
        if self._head_hash(allow_none=True) is None:
            # Unborn branch (fresh worktree, no snapshot committed yet): reset
            # --hard has no commit to target. Match its empty-tree semantics by
            # dropping any staged files from both the index and the working tree
            # (untracked files are left untouched). --ignore-unmatch makes this a
            # no-op when nothing is staged.
            self._git(["rm", "-r", "-f", "--ignore-unmatch", "."])
            return
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

        logger.info(
            f"Fetching snapshots from cache repo '{self.cache_repo}' "
            f"({self.cache_repo_url or 'unknown url'}) "
            "(this can be large on first sync)..."
        )
        # ``--progress`` forces git to emit its native transfer progress bar
        # (object counts, bytes, speed) on stderr even when stderr is not a TTY;
        # ``passthrough`` streams it straight to the console so a large fetch
        # shows live progress instead of a frozen "Fetching..." line.
        self._git_run(
            [
                "fetch",
                "--progress",
                self.cache_repo,
                f"{SNAPSHOT_REF_GLOB}:{SNAPSHOT_REF_GLOB}",
            ],
            passthrough=True,
        )
        logger.info("Finished fetching snapshots from cache repo.")

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

    def _remote_url(self, name: str) -> str | None:
        """Resolved URL/path a configured remote points at, or None if unknown."""
        r = self._git_capture(["remote", "get-url", name], check=False)
        return r.stdout.strip() if r.returncode == 0 else None

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

    @property
    def _excludes_file(self) -> Path:
        """Per-worktree ignore file (via ``core.excludesFile``), so workspaces
        don't clobber each other's patterns in the shared ``info/exclude``."""
        return self.git_dir / "synno-excludes"

    def _wt_branch(self) -> str:
        """Unique unborn branch a fresh worktree's HEAD starts on, so its first
        snapshot has no parent instead of chaining onto another workspace."""
        return f"refs/heads/synno/{self._workspace_key}"

    def _common_env(self) -> dict[str, str]:
        """Env targeting the shared repo directly (bare; no work tree/index)."""
        env = {k: v for k, v in self._env.items() if k != "GIT_WORK_TREE"}
        env["GIT_DIR"] = str(self._common_dir)
        return env

    def _ensure_repo(self) -> None:
        """Create the shared repo and this workspace's worktree as needed."""
        if not self._shared:
            self._ensure_standalone_repo()
            return
        self._ensure_common_repo()
        self._ensure_worktree()

    def _ensure_common_repo(self) -> None:
        """Create the shared bare repo if needed. ``--shared`` makes it
        writable by all users on a shared filesystem."""
        if (self._common_dir / "HEAD").is_file():
            return
        self._common_dir.mkdir(parents=True, exist_ok=True)
        env = {
            k: v for k, v in self._env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")
        }
        self._git_run_env(
            ["init", "--bare", "--shared=0777", str(self._common_dir)],
            env=env,
            check=True,
        )

    def _ensure_worktree(self) -> None:
        """Register this workspace as a linked worktree of the shared repo: an
        admin dir (``<repo>/worktrees/<key>``) with its own HEAD/index and a
        ``commondir`` pointer, plus a ``.git`` gitfile in the workspace. Created
        once; later runs keep HEAD/index so the workspace resumes its own line.
        """
        wt = self.git_dir
        gitfile = self.working_dir / ".git"
        if not (wt / "HEAD").is_file():
            wt.mkdir(parents=True, exist_ok=True)
            # `worktrees/` and this admin dir are shared across users; keep writable.
            for d in (wt.parent, wt):
                try:
                    os.chmod(d, 0o777)
                except OSError:
                    pass  # best effort; ownership may forbid it
            # Drop any stale branch so the fresh HEAD is genuinely unborn
            # (snapshots stay anchored by refs/snapshots/*).
            self._git_run_env(
                ["update-ref", "-d", self._wt_branch()],
                env=self._common_env(),
                check=False,
            )
            self._write_shared(wt / "commondir", "../..\n")
            self._write_shared(wt / "HEAD", f"ref: {self._wt_branch()}\n")
        # (Re)link admin dir and workspace to each other (idempotent).
        self._write_shared(wt / "gitdir", f"{gitfile}\n")
        if gitfile.is_dir():
            raise RuntimeError(
                f"{gitfile} is a git directory, but this workspace's snapshot "
                f"worktree lives at {wt}. Remove one of the two."
            )
        self._write_shared(gitfile, f"gitdir: {wt}\n")

    def _ensure_standalone_repo(self) -> None:
        """Fallback (no shared repo / explicit ``git_dir``): a plain repo at
        ``self.git_dir``, colocated or via ``--separate-git-dir``."""
        if (self.git_dir / "HEAD").is_file():
            return
        gitpath = self.working_dir / ".git"
        if gitpath.is_file():
            gitpath.unlink()  # dangling gitfile; git init refuses to follow it
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.git_dir == gitpath:
            self._git(["init"])
            return
        env = {
            k: v for k, v in self._env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")
        }
        self._git_run_env(
            ["init", f"--separate-git-dir={self.git_dir}"], env=env, check=True
        )

    @staticmethod
    def _write_shared(path: Path, content: str) -> None:
        """Write a worktree metadata file, best-effort world-writable so other
        users can re-register the same workspace's worktree."""
        path.write_text(content, encoding="utf-8")
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass  # best effort; ownership may forbid it

    def _pin_repo_env(self) -> None:
        """
        Force git to use ONLY this worktree (its git dir + work tree), never a
        repo discovered by walking up from the working tree. The git dir is
        decoupled from the work tree so it can live in the shared repo. Commit
        identity and this worktree's ignore file are injected here, so no
        (shared) per-repo config writes are needed.
        """
        username = getpass.getuser()
        self._env = os.environ.copy()
        self._env["GIT_DIR"] = str(self.git_dir)
        self._env["GIT_WORK_TREE"] = str(self.working_dir)
        # Also prevent upward discovery if something goes odd
        self._env["GIT_CEILING_DIRECTORIES"] = str(self.working_dir)
        self._env["GIT_AUTHOR_NAME"] = username
        self._env["GIT_AUTHOR_EMAIL"] = "llm@local"
        self._env["GIT_COMMITTER_NAME"] = username
        self._env["GIT_COMMITTER_EMAIL"] = "llm@local"
        # Per-worktree ignore patterns without touching the shared info/exclude.
        self._env["GIT_CONFIG_COUNT"] = "1"
        self._env["GIT_CONFIG_KEY_0"] = "core.excludesFile"
        self._env["GIT_CONFIG_VALUE_0"] = str(self._excludes_file)

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

    def _write_common_excludes(self) -> None:
        """Persist the always-ignored framework paths in the (shared) repo's
        ``info/exclude``. Unlike the env-injected ``core.excludesFile`` - which
        only applies to git commands the snapshotter itself runs - ``info/exclude``
        is consulted by *every* git invocation against this workspace, including a
        plain ``git status`` a user runs in the workspace by hand.

        These patterns are universal (identical for all workspaces), so sharing
        them in the common dir is safe; per-workspace ``extra_gitignore`` stays in
        this worktree's private excludes file to avoid cross-workspace clobbering.
        Appends idempotently and keeps the file world-writable for parallel runs.
        """
        exclude_file = self._common_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        missing = [p for p in ALWAYS_IGNORED if p not in existing.split()]
        if not missing:
            return
        with exclude_file.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# synno: always-ignored framework paths (auto-added)\n")
            f.writelines(f"{p}\n" for p in missing)
        try:
            os.chmod(exclude_file, 0o666)
        except OSError:
            pass  # best effort; ownership may forbid it

    def _write_extra_gitignore(self, patterns: Iterable[str] | None) -> None:
        """
        Writes ignore patterns and excluded file paths to this worktree's private
        excludes file (via ``core.excludesFile``; applies in addition to .gitignore).
        Always-ignored framework paths (``ALWAYS_IGNORED``) are prepended so they
        are excluded for every workspace regardless of caller-supplied patterns.
        Overwrites the file each time so stale patterns from previous runs don't accumulate.
        """
        exclude = self._excludes_file
        exclude.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = list(ALWAYS_IGNORED)
        if patterns:
            lines += [p.strip() for p in patterns if p.strip()]
        lines += self.exclude_files

        self._write_shared(exclude, "".join(f"{line}\n" for line in lines))
