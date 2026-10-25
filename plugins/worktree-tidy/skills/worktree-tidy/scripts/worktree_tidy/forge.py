"""What GitHub says about a worktree's branch: its pull requests and whether the branch is still
on the remote. One read-only `gh api graphql` call per repo, cached so a session start usually
makes none.

A failed or partial answer is never read as "gone": a branch with no answer is unchecked, and an
unchecked branch is never stale.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from .gitio import Deadline, OutOfTime, run

# How long a branch's answer is reused. PR state changes slowly next to a session start.
DEFAULT_TTL = 600

_URL_PATTERNS = [
    # https://host/owner/repo(.git), ssh://git@host(:port)/owner/repo(.git)
    re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)/?$"),
    # git@host:owner/repo(.git)
    re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>[^/].+?)/?$"),
]


@dataclass(frozen=True)
class Slug:
    host: str
    owner: str
    name: str

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_slug(url: str, override: Optional[str] = None) -> Optional[Slug]:
    """The GitHub repo an origin URL points at. `override` (git config `worktree-tidy.repo`,
    `owner/name` or `host/owner/name`) wins, for remotes behind an SSH host alias."""
    if override:
        parts = override.strip("/").split("/")
        if len(parts) == 2:
            return Slug("github.com", parts[0], parts[1])
        if len(parts) == 3:
            return Slug(parts[0], parts[1], parts[2])
        return None
    for pattern in _URL_PATTERNS:
        m = pattern.match(url.strip())
        if not m:
            continue
        host = m.group("host").lower()
        path = m.group("path")
        path = path.removesuffix(".git")
        parts = path.split("/")
        if len(parts) != 2 or not all(parts):
            return None
        if "github" not in host:
            return None
        if host in ("ssh.github.com", "www.github.com"):
            host = "github.com"
        return Slug(host, parts[0], parts[1])
    return None


@dataclass(frozen=True)
class PR:
    number: int
    state: str  # OPEN, CLOSED or MERGED
    head: str  # the PR's head commit
    url: str


@dataclass(frozen=True)
class BranchInfo:
    prs: tuple[PR, ...]  # same-repo PRs from this branch, newest first
    on_remote: bool


class ForgeError(Exception):
    pass


_QUERY_LIMIT = 40  # branches per GraphQL call


def _query(branches: list[str]) -> str:
    params = ", ".join(f"$b{i}: String!, $q{i}: String!" for i in range(len(branches)))
    fields = []
    for i in range(len(branches)):
        fields.append(
            f"p{i}: pullRequests(headRefName: $b{i}, first: 10, "
            "orderBy: {field: CREATED_AT, direction: DESC}) "
            "{ nodes { number state url headRefOid isCrossRepository } }"
        )
        fields.append(f"r{i}: ref(qualifiedName: $q{i}) {{ name }}")
    return (
        f"query($owner: String!, $name: String!, {params}) "
        f"{{ repository(owner: $owner, name: $name) {{ {' '.join(fields)} }} }}"
    )


def _ask(slug: Slug, branches: list[str], deadline: Deadline, gh: str) -> dict[str, BranchInfo]:
    args = [gh, "api", "graphql"]
    if slug.host != "github.com":
        args += ["--hostname", slug.host]
    args += ["-f", f"query={_query(branches)}", "-f", f"owner={slug.owner}"]
    args += ["-f", f"name={slug.name}"]
    for i, branch in enumerate(branches):
        args += ["-f", f"b{i}={branch}", "-f", f"q{i}=refs/heads/{branch}"]
    try:
        result = run(args, deadline)
    except FileNotFoundError:
        raise ForgeError("gh isn't on PATH") from None
    except OutOfTime:
        raise ForgeError("ran out of time") from None
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise ForgeError(f"gh failed: {message[-1] if message else f'exit {result.returncode}'}")
    try:
        data = json.loads(result.stdout)
        repo = data["data"]["repository"]
    except (ValueError, KeyError, TypeError):
        raise ForgeError("gh returned something that isn't a GraphQL answer") from None
    if repo is None:
        raise ForgeError(f"{slug} isn't visible to gh")
    if data.get("errors"):
        raise ForgeError(f"GraphQL error: {data['errors'][0].get('message', '?')}")
    answers = {}
    for i, branch in enumerate(branches):
        conn = repo.get(f"p{i}")
        if conn is None or f"r{i}" not in repo:
            raise ForgeError("incomplete GraphQL answer")
        prs = tuple(
            PR(n["number"], n["state"], n["headRefOid"], n["url"])
            for n in conn["nodes"]
            if not n.get("isCrossRepository")
        )
        answers[branch] = BranchInfo(prs, repo[f"r{i}"] is not None)
    return answers


def cache_dir() -> str:
    base = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not base:
        base = os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "worktree-tidy"
        )
    return base


def _cache_path(slug: Slug) -> str:
    key = hashlib.sha256(f"{slug.host}/{slug}".encode()).hexdigest()[:16]
    return os.path.join(cache_dir(), "forge", f"{slug.owner}-{slug.name}-{key}.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that can't be written costs a call next time, nothing more


def branch_info(
    slug: Slug,
    branches: list[str],
    deadline: Deadline,
    ttl: float = DEFAULT_TTL,
    refresh: bool = False,
    gh: str = "gh",
) -> dict[str, BranchInfo]:
    """Each branch's PRs and remote presence, from the cache when younger than `ttl`. Raises
    ForgeError when gh can't answer for a branch the cache doesn't cover."""
    path = _cache_path(slug)
    cache = _load(path)
    now = time.time()
    answers: dict[str, BranchInfo] = {}
    todo = []
    for branch in branches:
        hit = cache.get(branch)
        if not refresh and isinstance(hit, dict) and now - hit.get("at", 0) < ttl:
            try:
                answers[branch] = BranchInfo(
                    tuple(PR(**p) for p in hit["prs"]), bool(hit["on_remote"])
                )
                continue
            except (KeyError, TypeError):
                pass
        todo.append(branch)
    for start in range(0, len(todo), _QUERY_LIMIT):
        chunk = todo[start : start + _QUERY_LIMIT]
        fresh = _ask(slug, chunk, deadline, gh)
        for branch, info in fresh.items():
            answers[branch] = info
            cache[branch] = {
                "at": now,
                "prs": [p.__dict__ for p in info.prs],
                "on_remote": info.on_remote,
            }
    if todo:
        # Forget branches no longer asked about once they're stale, so the file stays small.
        cache = {
            k: v
            for k, v in cache.items()
            if k in branches or (isinstance(v, dict) and now - v.get("at", 0) < ttl)
        }
        _save(path, cache)
    return answers
