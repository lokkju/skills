"""Find a scope's repos and sort each linked worktree into stale, needs-a-look or leave-alone.

Stale (listed with removal commands):
  merged    its branch's newest PR merged, no PR from it is open, the branch is gone from the
            remote, the checkout's commit is in the PR, and the worktree is clean
  closed    the same with a PR closed without merging
  detached  a detached checkout, clean, on a commit some branch or tag has, unused for N days
  prunable  git already knows its directory is gone (`git worktree prune` clears it)

Needs a look (listed, no commands): merged or closed but dirty, carrying commits the PR
doesn't have, or with its branch still on the remote; detached and dirty or on a commit no ref
has; and directories under .claude/worktrees/ or .wt/ that git doesn't list as worktrees.

Never touched: a repo's main checkout, the worktree the session is in, locked worktrees, and
branches with an open PR or none at all.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from . import forge, gitio
from .gitio import Deadline, OutOfTime

DEFAULT_DAYS = 7
SCRATCH_DIRS = (os.path.join(".claude", "worktrees"), ".wt")


@dataclass
class Item:
    path: str
    kind: str  # merged, closed, detached, prunable, or "look"
    why: str
    branch: Optional[str] = None
    submodules: bool = False


@dataclass
class RepoReport:
    path: str  # the main checkout (or the bare repo)
    name: str
    stale: list[Item] = field(default_factory=list)
    look: list[Item] = field(default_factory=list)
    linked: int = 0  # linked worktrees, the main checkout not counted
    unchecked: list[str] = field(default_factory=list)  # worktree paths left unclassified
    forge_note: Optional[str] = None  # why PRs couldn't be checked, when they couldn't
    error: Optional[str] = None
    partial: bool = False  # the time budget ran out partway


@dataclass
class Scan:
    roots: list[str]
    repos: list[RepoReport]
    out_of_time: bool = False
    not_reached: list[str] = field(default_factory=list)  # repos the budget didn't reach
    elapsed: float = 0.0

    def stale(self) -> list[tuple[RepoReport, Item]]:
        return [(r, i) for r in self.repos for i in r.stale]


def _is_git_dir(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def discover(roots: list[str], deadline: Deadline) -> list[str]:
    """The repos to scan: each root that's in a git repo, else the root's immediate non-hidden
    children that are. One entry per repo (by shared git dir), as the path of any of its
    worktrees."""
    seen: dict[str, str] = {}
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        common = gitio.common_dir(root, deadline)
        if common:
            seen.setdefault(common, root)
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if name.startswith(".") or not os.path.isdir(path) or not _is_git_dir(path):
                continue
            common = gitio.common_dir(path, deadline)
            if common:
                seen.setdefault(common, path)
    return list(seen.values())


def _inside(path: str, parent: str) -> bool:
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _orphans(main: str, registered: set[str]) -> list[Item]:
    found = []
    for scratch in SCRATCH_DIRS:
        base = os.path.join(main, scratch)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if not os.path.isdir(path) or os.path.realpath(path) in registered:
                continue
            try:
                empty = not os.listdir(path)
            except OSError:
                empty = False
            what = "an empty directory" if empty else "a directory"
            found.append(Item(path, "look", f"{what} under {scratch} that git doesn't list"))
    return found


def scan_repo(
    start: str,
    deadline: Deadline,
    cwd: str,
    days: float = DEFAULT_DAYS,
    offline: bool = False,
    refresh: bool = False,
    ttl: float = forge.DEFAULT_TTL,
    gh: str = "gh",
) -> RepoReport:
    trees = gitio.worktrees(start, deadline)
    main = trees[0]
    report = RepoReport(main.path, os.path.basename(main.path.rstrip(os.sep)) or main.path)
    linked = trees[1:]
    report.linked = len(linked)
    registered = {os.path.realpath(t.path) for t in trees}

    candidates = []  # (worktree, kind, why)
    branch_trees = []
    for tree in linked:
        if tree.prunable is not None:
            why = "its directory is gone" + (f" ({tree.prunable})" if tree.prunable else "")
            report.stale.append(Item(tree.path, "prunable", why, tree.branch))
        elif tree.locked is not None or _inside(cwd, tree.path) or tree.bare:
            continue
        elif tree.branch:
            branch_trees.append(tree)
        elif tree.detached and tree.head:
            candidates.append((tree, "detached", ""))

    done: set[str] = set()
    try:
        if branch_trees:
            _classify_branches(
                report, branch_trees, candidates, done, deadline, offline, refresh, ttl, gh
            )
        now = time.time()
        for tree, kind, why in candidates:
            item = _confirm(tree, kind, why, deadline, now, days)
            done.add(tree.path)
            if item is not None:
                (report.stale if item.kind != "look" else report.look).append(item)
    except OutOfTime:
        report.partial = True
        pending = [t.path for t in branch_trees] + [t.path for t, _, _ in candidates]
        report.unchecked.extend(p for p in pending if p not in done and p not in report.unchecked)

    if not main.bare:
        report.look.extend(_orphans(main.path, registered))
    return report


def _classify_branches(report, branch_trees, candidates, done, deadline, offline, refresh, ttl, gh):
    if offline:
        report.forge_note = "PRs not checked (offline)"
        report.unchecked.extend(t.path for t in branch_trees)
        done.update(t.path for t in branch_trees)
        return
    url = gitio.origin_url(report.path, deadline) or ""
    override = gitio.config_get(report.path, "worktree-tidy.repo", deadline)
    slug = forge.parse_slug(url, override)
    if slug is None:
        report.forge_note = (
            "PRs not checked (origin isn't on GitHub)" if url else "PRs not checked (no origin)"
        )
        report.unchecked.extend(t.path for t in branch_trees)
        done.update(t.path for t in branch_trees)
        return
    try:
        infos = forge.branch_info(
            slug, sorted({t.branch for t in branch_trees}), deadline, ttl, refresh, gh
        )
    except forge.ForgeError as error:
        report.forge_note = f"couldn't check PRs: {error}"
        report.unchecked.extend(t.path for t in branch_trees)
        done.update(t.path for t in branch_trees)
        return
    for tree in branch_trees:
        info = infos.get(tree.branch)
        if info is None or not info.prs or any(p.state == "OPEN" for p in info.prs):
            done.add(tree.path)  # no PR yet (work in progress) or one still open
            continue
        pr = info.prs[0]
        kind = "merged" if pr.state == "MERGED" else "closed"
        label = f"PR #{pr.number} {kind}"
        if info.on_remote:
            why = f"{label}; branch still on the remote"
            report.look.append(Item(tree.path, "look", why, tree.branch))
            done.add(tree.path)
            continue
        inside = gitio.contains(tree.path, tree.head, pr.head, deadline)
        if inside is True:
            candidates.append((tree, kind, label))
            continue
        if inside is False:
            why = f"{label}; the checkout has commits the PR doesn't"
        else:
            why = f"{label}; couldn't confirm the checkout's commit is in the PR"
        report.look.append(Item(tree.path, "look", why, tree.branch))
        done.add(tree.path)


def _confirm(tree, kind, why, deadline, now, days) -> Optional[Item]:
    """The last, local checks on a candidate: clean, and for a detached one, old and on a ref."""
    branch = tree.branch
    if kind == "detached":
        used = gitio.last_used(tree.path)
        if used is None or now - used < days * 86400:
            return None  # recent, or its age can't be shown
        if not gitio.on_a_ref(tree.path, tree.head, deadline):
            return Item(tree.path, "look", f"detached at {tree.head[:9]}, which no branch has")
        why = f"detached at {tree.head[:9]}, unused {int((now - used) // 86400)} days"
    try:
        changes = gitio.dirty(tree.path, deadline)
    except RuntimeError as error:
        return Item(tree.path, "look", f"{why}; git status failed: {error}", branch)
    if changes:
        return Item(tree.path, "look", f"{why}; {changes}", branch)
    submodules = os.path.exists(os.path.join(tree.path, ".gitmodules"))
    return Item(tree.path, kind, why, branch, submodules)


def scan(
    roots: list[str],
    cwd: str,
    budget: float,
    days: float = DEFAULT_DAYS,
    offline: bool = False,
    refresh: bool = False,
    ttl: float = forge.DEFAULT_TTL,
    gh: str = "gh",
) -> Scan:
    started = time.monotonic()
    deadline = Deadline(budget)
    result = Scan(roots=[os.path.abspath(r) for r in roots], repos=[])
    try:
        starts = discover(roots, deadline)
    except OutOfTime:
        result.out_of_time = True
        starts = []
    for index, start in enumerate(starts):
        if result.out_of_time:
            result.not_reached = starts[index:]
            break
        try:
            report = scan_repo(start, deadline, cwd, days, offline, refresh, ttl, gh)
        except OutOfTime:
            result.out_of_time = True
            result.not_reached = starts[index:]
            break
        except (RuntimeError, OSError) as error:
            name = os.path.basename(start.rstrip(os.sep))
            result.repos.append(RepoReport(start, name, error=str(error)))
            continue
        result.repos.append(report)
        if report.partial:
            result.out_of_time = True
    result.elapsed = time.monotonic() - started
    return result
