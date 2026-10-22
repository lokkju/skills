"""worktree-tidy: find stale git worktrees and print the commands that would remove them.

It only reads. Removal is the commands it prints, for a person to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
from collections import Counter
from typing import Optional

from . import forge, scan
from .scan import Item, RepoReport, Scan

HOOK_BUDGET = 4.0
REPORT_BUDGET = 60.0
KINDS = ("merged", "closed", "detached", "prunable")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _roots(given: Optional[list[str]], cwd: str) -> list[str]:
    if given:
        return given
    env = os.environ.get("WORKTREE_TIDY_ROOTS", "")
    listed = [os.path.expanduser(p) for p in env.split(os.pathsep) if p.strip()]
    return listed or [cwd]


def _rel(path: str, base: str) -> str:
    rel = os.path.relpath(path, base)
    return path if rel.startswith("..") else rel


def _counts(result: Scan) -> Counter:
    return Counter(item.kind for _, item in result.stale())


def _breakdown(counts: Counter) -> str:
    return ", ".join(f"{counts[k]} {k}" for k in KINDS if counts[k])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(result: Scan) -> str:
    counts = _counts(result)
    total = sum(counts.values())
    look = sum(len(r.look) for r in result.repos)
    names = [r.name for r in result.repos if r.stale]
    if total:
        text = f"{_plural(total, 'stale worktree')} ({_breakdown(counts)}) in {', '.join(names)}"
    else:
        text = "no stale worktrees"
    if look:
        text += f"; {look} to look at"
    return text


def commands(result: Scan) -> list[str]:
    """Shell lines that remove each stale worktree. Each removal first checks the worktree is
    still clean, so a line run later than the scan can't discard new work."""
    lines = []
    for repo in result.repos:
        if not repo.stale:
            continue
        lines.append(f"# {repo.name}")
        prune = False
        for item in repo.stale:
            if item.kind == "prunable":
                prune = True
                continue
            wt, top = shlex.quote(item.path), shlex.quote(repo.path)
            force = " --force" if item.submodules else ""
            line = (
                f'[ -z "$(git -C {wt} status --porcelain --ignore-submodules=none)" ]'
                f" && git -C {top} worktree remove{force} {wt}"
            )
            if item.branch:
                line += f" && git -C {top} branch -D {shlex.quote(item.branch)}"
            left = shlex.quote(f"worktree-tidy: left {item.path} (not clean, or git refused)")
            lines.append(f"{{ {line}; }} || echo {left} >&2")
            prune = True
        if prune:
            lines.append(f"git -C {shlex.quote(repo.path)} worktree prune")
    return lines


def _item_line(item: Item, repo: RepoReport) -> str:
    where = _rel(item.path, repo.path)
    branch = f" [{item.branch}]" if item.branch else ""
    return f"{where}{branch}: {item.why}"


def render(result: Scan) -> str:
    out = []
    scope = ", ".join(result.roots)
    out.append(f"worktree-tidy: {summary(result)} ({_plural(len(result.repos), 'repo')}, {scope})")
    for repo in result.repos:
        out.append("")
        if repo.error:
            out.append(f"{repo.name} ({repo.path}): couldn't read: {repo.error}")
            continue
        out.append(f"{repo.name} ({repo.path}): {_plural(repo.linked, 'linked worktree')}")
        for item in repo.stale:
            out.append(f"  stale {item.kind}: {_item_line(item, repo)}")
        for item in repo.look:
            out.append(f"  look at: {_item_line(item, repo)}")
        if repo.forge_note and repo.unchecked:
            out.append(f"  {repo.forge_note}")
        if repo.unchecked:
            names = ", ".join(_rel(p, repo.path) for p in repo.unchecked)
            out.append(f"  not classified: {names}")
    if result.out_of_time:
        out.append("")
        reached = ", ".join(os.path.basename(p) for p in result.not_reached) or "none"
        out.append(f"The time budget ran out; repos not reached: {reached}.")
    lines = commands(result)
    out.append("")
    if lines:
        out.append(
            "To remove the stale ones, run these yourself (removal is your call). Each checks the "
            "worktree is still clean first; --force appears only for clean worktrees with "
            "submodules, which git won't remove without it."
        )
        out.append("")
        out.extend(lines)
    else:
        out.append("Nothing to remove.")
    return "\n".join(out) + "\n"


def to_json(result: Scan) -> dict:
    return {
        "roots": result.roots,
        "summary": summary(result),
        "out_of_time": result.out_of_time,
        "not_reached": result.not_reached,
        "elapsed": round(result.elapsed, 3),
        "repos": [
            {
                "name": r.name,
                "path": r.path,
                "linked": r.linked,
                "error": r.error,
                "forge_note": r.forge_note,
                "unchecked": r.unchecked,
                "stale": [i.__dict__ for i in r.stale],
                "look": [i.__dict__ for i in r.look],
            }
            for r in result.repos
        ],
        "commands": commands(result),
    }


def report_path(roots: list[str]) -> str:
    key = "\0".join(os.path.abspath(r) for r in roots)
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    name = os.path.basename(os.path.abspath(roots[0]).rstrip(os.sep)) or "root"
    return os.path.join(forge.cache_dir(), "reports", f"{name}-{digest}.txt")


def _write(path: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except OSError:
        return False


def _hook_note(result: Scan, path: Optional[str]) -> Optional[str]:
    counts = _counts(result)
    failed = [
        r.name
        for r in result.repos
        if r.unchecked and r.forge_note and r.forge_note.startswith("couldn't")
    ]
    if not counts and not failed:
        return None
    parts = []
    if counts:
        parts.append(summary(result))
    if failed:
        parts.append(f"couldn't check PRs in {', '.join(failed)}")
    if result.out_of_time:
        parts.append("the time budget ran out before every repo was checked")
    note = "worktree-tidy: " + "; ".join(parts) + "."
    if counts:
        note += " /worktree-tidy lists them with the commands to remove them"
        note += f" (last list: {path})." if path else "."
    return note


def hook(stdin: str) -> int:
    try:
        event = json.loads(stdin) if stdin.strip() else {}
    except ValueError:
        event = {}
    cwd = event.get("cwd") or os.getcwd()
    roots = _roots(None, cwd)
    result = scan.scan(
        roots,
        cwd,
        _env_float("WORKTREE_TIDY_BUDGET", HOOK_BUDGET),
        days=_env_float("WORKTREE_TIDY_DETACHED_DAYS", scan.DEFAULT_DAYS),
        offline=os.environ.get("WORKTREE_TIDY_OFFLINE", "") not in ("", "0"),
        ttl=_env_float("WORKTREE_TIDY_TTL", forge.DEFAULT_TTL),
        gh=os.environ.get("WORKTREE_TIDY_GH", "gh"),
    )
    path: Optional[str] = None
    if result.stale():
        path = report_path(roots)
        if not _write(path, render(result)):
            path = None
    note = _hook_note(result, path)
    if note is None:
        return 0
    context = (
        note + " Removing worktrees is the user's call: show them the list and commands when "
        "they ask, and don't run the removal commands yourself."
    )
    out = {
        "systemMessage": note,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }
    print(json.dumps(out))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="worktree-tidy",
        description="Find stale git worktrees and print the commands that would remove them. "
        "Reads only; nothing is removed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="SessionStart hook: reads the event on stdin, prints hook JSON")
    rep = sub.add_parser("report", help="the full list and the removal commands")
    rep.add_argument(
        "--root",
        action="append",
        help="a repo, or a folder whose immediate children are repos (repeatable; default "
        "$WORKTREE_TIDY_ROOTS, else the current directory)",
    )
    rep.add_argument("--refresh", action="store_true", help="ask GitHub again, ignoring the cache")
    rep.add_argument("--offline", action="store_true", help="don't ask GitHub at all")
    rep.add_argument("--days", type=float, help="detached checkouts count as stale after this")
    rep.add_argument("--budget", type=float, default=REPORT_BUDGET, help="seconds (default 60)")
    rep.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.command == "hook":
        try:
            return hook(sys.stdin.read())
        except Exception as error:  # noqa: BLE001 (a session start must never fail on this)
            print(f"worktree-tidy: hook failed: {error}", file=sys.stderr)
            return 0

    cwd = os.getcwd()
    days = args.days or _env_float("WORKTREE_TIDY_DETACHED_DAYS", scan.DEFAULT_DAYS)
    started = time.monotonic()
    result = scan.scan(
        _roots(args.root, cwd),
        cwd,
        args.budget,
        days=days,
        offline=args.offline,
        refresh=args.refresh,
        ttl=_env_float("WORKTREE_TIDY_TTL", forge.DEFAULT_TTL),
        gh=os.environ.get("WORKTREE_TIDY_GH", "gh"),
    )
    result.elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps(to_json(result), indent=2))
    else:
        sys.stdout.write(render(result))
    return 0
