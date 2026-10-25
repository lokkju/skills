"""Read-only git calls under a deadline. Nothing here writes to a repo or a worktree."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


class OutOfTime(Exception):
    """The scan's time budget ran out before this call could start."""


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    def check(self) -> float:
        left = self.left()
        if left <= 0.05:
            raise OutOfTime
        return left


# git never asks for credentials or a passphrase; a call that would need one fails instead.
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def run(
    args: list[str], deadline: Deadline, cwd: Optional[str] = None
) -> subprocess.CompletedProcess:
    left = deadline.check()
    env = dict(os.environ, **_ENV)
    try:
        return subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=left, check=False
        )
    except subprocess.TimeoutExpired:
        raise OutOfTime from None


def git(path: str, *args: str, deadline: Deadline) -> subprocess.CompletedProcess:
    return run(["git", "-C", path, *args], deadline)


@dataclass
class Worktree:
    path: str
    head: str = ""
    branch: Optional[str] = None  # short name, None when detached
    bare: bool = False
    detached: bool = False
    locked: Optional[str] = None  # the reason, "" when locked without one
    prunable: Optional[str] = None
    extra: dict = field(default_factory=dict)


def parse_worktree_list(out: str) -> list[Worktree]:
    """`git worktree list --porcelain -z`: attributes NUL-terminated, records ended by an empty
    attribute."""
    trees: list[Worktree] = []
    current: Optional[Worktree] = None
    for attr in out.split("\0"):
        if not attr:
            current = None
            continue
        key, _, value = attr.partition(" ")
        if key == "worktree":
            current = Worktree(path=value)
            trees.append(current)
        elif current is None:
            continue
        elif key == "HEAD":
            current.head = value
        elif key == "branch":
            current.branch = value.removeprefix("refs/heads/")
        elif key == "bare":
            current.bare = True
        elif key == "detached":
            current.detached = True
        elif key == "locked":
            current.locked = value
        elif key == "prunable":
            current.prunable = value
        else:
            current.extra[key] = value
    return trees


def worktrees(repo: str, deadline: Deadline) -> list[Worktree]:
    result = git(repo, "worktree", "list", "--porcelain", "-z", deadline=deadline)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree list failed")
    return parse_worktree_list(result.stdout)


def common_dir(path: str, deadline: Deadline) -> Optional[str]:
    """The repo's shared git dir when `path` is in one, else None."""
    result = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", deadline=deadline)
    if result.returncode != 0:
        return None
    return os.path.realpath(result.stdout.strip())


def dirty(path: str, deadline: Deadline) -> Optional[str]:
    """What `git status` reports as changed (untracked included, submodules included), or None
    when the worktree is clean. Raises RuntimeError when status fails."""
    result = git(path, "status", "--porcelain", "--ignore-submodules=none", deadline=deadline)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    lines = [line for line in result.stdout.splitlines() if line]
    if not lines:
        return None
    return f"{len(lines)} changed or untracked path{'s' if len(lines) != 1 else ''}"


def contains(path: str, ancestor: str, tip: str, deadline: Deadline) -> Optional[bool]:
    """Whether `ancestor` is reachable from `tip`; None when git can't tell (an object is
    missing)."""
    if ancestor == tip:
        return True
    result = git(path, "merge-base", "--is-ancestor", ancestor, tip, deadline=deadline)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def on_a_ref(path: str, sha: str, deadline: Deadline) -> bool:
    """Whether a branch, remote-tracking branch or tag has `sha`, so a detached checkout's commit
    survives the checkout's removal."""
    result = git(
        path,
        "for-each-ref",
        "--count=1",
        f"--contains={sha}",
        "--format=%(refname)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
        deadline=deadline,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def origin_url(repo: str, deadline: Deadline) -> Optional[str]:
    result = git(repo, "remote", "get-url", "origin", deadline=deadline)
    return result.stdout.strip() if result.returncode == 0 else None


def config_get(repo: str, key: str, deadline: Deadline) -> Optional[str]:
    result = git(repo, "config", "--get", key, deadline=deadline)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def last_used(path: str) -> Optional[float]:
    """When the worktree was last checked out or staged in: the newest mtime among its admin
    dir's HEAD, index and HEAD reflog. None when that dir can't be found."""
    dotgit = os.path.join(path, ".git")
    try:
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    admin = line[len("gitdir:") :].strip()
    if not os.path.isabs(admin):
        admin = os.path.normpath(os.path.join(path, admin))
    times = []
    for name in ("HEAD", "index", os.path.join("logs", "HEAD")):
        try:
            times.append(os.path.getmtime(os.path.join(admin, name)))
        except OSError:
            pass
    return max(times) if times else None
