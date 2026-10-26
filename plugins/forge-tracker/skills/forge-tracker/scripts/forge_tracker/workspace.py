"""A workspace: a folder that isn't a git repo but holds several. /catchup reads each repo on its
own (its own config, window and marker) and prints one report.

The repos are `repos:` in the folder's own `.claude/forge-tracker.local.md` when it sets that
(paths relative to the folder; the only key read there), else its immediate children that are git
repos with an `origin` remote or a `tracker.web` key. Hidden children are left out.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Callable, Optional

from . import catchup, config, detect
from .model import ConfigError

SCHEMA = 1


@dataclass(frozen=True)
class Workspace:
    root: str
    repos: tuple[str, ...]  # absolute paths, in report order
    # Children left out, with why: a git repo with no origin remote.
    skipped: tuple[tuple[str, str], ...] = ()
    listed: bool = False  # from `repos:` rather than found


def is_repo(path: str, run: Callable[..., Any] = subprocess.run) -> bool:
    """Whether `path` is inside a git work tree."""
    try:
        result = run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        raise ConfigError("git isn't on PATH") from None
    return result.returncode == 0 and (result.stdout or "").strip() == "true"


def _has_remote(path: str, run: Callable[..., Any]) -> bool:
    if detect.git_config(path, detect.OVERRIDE_WEB, run):
        return True
    try:
        detect.origin_url(path, run)
    except ConfigError:
        return False
    return True


def discover(root: str, run: Callable[..., Any] = subprocess.run) -> Workspace:
    """The workspace at `root`. A `repos:` list is taken as given, so a listed path that isn't a
    readable repo fails in the report instead of vanishing from it."""
    root = os.path.abspath(root)
    listed = config.load(root).repos
    if listed:
        return Workspace(
            root, tuple(os.path.normpath(os.path.join(root, p)) for p in listed), (), True
        )
    repos, skipped = [], []
    try:
        names = sorted(os.listdir(root))
    except OSError as error:
        raise ConfigError(f"can't list {root}: {error.strerror}") from None
    for name in names:
        path = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        if _has_remote(path, run):
            repos.append(path)
        else:
            skipped.append((name, "no origin remote"))
    return Workspace(root, tuple(repos), tuple(skipped))


def chosen(repos: list[str]) -> Workspace:
    """A workspace of the repos named (by --repo more than once), wherever they are; its root is
    the folder they share. Taken as given, like a `repos:` list."""
    paths = tuple(os.path.abspath(p) for p in repos)
    return Workspace(os.path.commonpath(paths), paths, (), True)


@dataclass
class Entry:
    """One repo's part: its report, or why it couldn't be read at all."""

    path: str
    report: Optional[dict[str, Any]] = None
    error: Optional[str] = None


def _name(entry: Entry, root: str) -> str:
    if entry.report:
        return entry.report["project"]
    return os.path.relpath(entry.path, root)


def build(ws: Workspace, entries: list[Entry], marker: str) -> dict[str, Any]:
    """The combined report as data: every repo's own report, plus the notes they share said once
    with the repos they apply to. `marker` is move, keep (--no-mark) or since (--since)."""
    notes: dict[str, list[str]] = {}
    repos = []
    for entry in entries:
        name = _name(entry, ws.root)
        if entry.report:
            for note in entry.report["waiting"]["notes"]:
                notes.setdefault(note, []).append(name)
        repos.append(
            {
                "name": name,
                "dir": os.path.relpath(entry.path, ws.root),
                "report": entry.report,
                "error": entry.error,
            }
        )
    ok = all(e.report is not None and e.report["ok"] for e in entries)
    return {
        "schema": SCHEMA,
        "workspace": ws.root,
        "marker": marker,
        "repos": repos,
        "skipped": [{"dir": d, "reason": r} for d, r in ws.skipped],
        "notes": [{"note": n, "repos": names} for n, names in notes.items()],
        "ok": ok,
    }


# --- rendering ----------------------------------------------------------------------------------


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _quiet(report: dict[str, Any]) -> bool:
    """Nothing to say past Waiting on you: nothing merged, ruled or moved, no failed part, and a
    health section with no failed run, no flaky job and no tags to show."""
    merged, health = report["merged"], report["health"]
    if report["failed"] or merged["groups"] or merged["changelog"]:
        return False
    if report["decisions"]["items"] or report["board"]["tiers"]:
        return False
    if health.get("flaky") or health.get("tags"):
        return False
    # A run still going is said in the one line; a finished one that didn't pass is news.
    for name in ("push_run", "schedule_run"):
        run = health.get(name)
        if run and run["status"] == "completed" and run["conclusion"] != "success":
            return False
    return True


def _push_state(health: dict[str, Any]) -> str:
    run = health.get("push_run")
    if run is None:
        return "none"
    if run["status"] != "completed":
        return run["status"]
    return "passed"


def render(report: dict[str, Any], tz: Optional[tzinfo] = None) -> str:
    entries = report["repos"]
    read = [e for e in entries if e["report"]]
    marker = {
        "move": "each repo's marker moves when all its reads succeeded",
        "keep": "--no-mark, so every marker stays",
        "since": "--since; no marker is read or moved",
    }[report["marker"]]
    held = [e["name"] for e in read if e["report"]["marker"] == "held"]
    if held:
        marker += f"; a read FAILED, so the marker stays for {_join(held)}"
    count = f"{len(entries)} repo{'s' if len(entries) != 1 else ''}"
    lines = [f"Workspace {report['workspace']}: {count} ({marker})", ""]

    # 1. Waiting on you, every repo's items together, newest first, each tagged with its repo.
    lines.append("Waiting on you")
    items = []
    for e in read:
        for item in e["report"]["waiting"]["items"]:
            items.append((e["name"], item))
    items.sort(key=lambda pair: pair[1]["updated"], reverse=True)
    for name, item in items:
        reasons = ", ".join(item["reasons"])
        ref = f"{name}{item['ref']}"
        if item["kind"] == "change" and "conflicts" in item:
            extra = f" (draft {'yes' if item['draft'] else 'no'}, conflicts: {item['conflicts']})"
            lines.append(f"  {ref} {reasons}: {item['title']}{extra}")
        else:
            day = catchup._day(item["updated"], tz)
            lines.append(f"  {ref} {reasons}, updated {day}: {item['title']}")
    standing, unknown, failed = [], [], []
    for e in read:
        waiting = e["report"]["waiting"]
        if waiting["standing"]:
            parts = ", ".join(f"{n} {reason}" for reason, n in waiting["standing"].items())
            standing.append(f"{e['name']} {parts}")
        if not e["report"]["user"] or any(
            f["part"] in ("open_changes", "open_issues") for f in waiting["failed"]
        ):
            unknown.append(e["name"])
        failed += [f"  {e['name']} {f['part']}: FAILED ({f['reason']})" for f in waiting["failed"]]
    if not items and not standing and not failed:
        lines.append("  nothing")
    show_all = any(e["report"]["waiting"].get("all") for e in read)
    if standing:
        lines.append(f"  standing: {'; '.join(standing)} (not updated in the window)")
    elif not show_all and read and len(unknown) < len(read):
        where = f" in the repos that read it (not {_join(unknown)})" if unknown else ""
        lines.append(f"  standing: none{where} (not updated in the window)")
    lines += failed
    for note in report["notes"]:
        where = _join(note["repos"])
        if len(note["repos"]) == len(read) > 2:
            where = f"all {len(read)} repos"
            if len(read) < len(entries):
                where += " that could be read"
        lines.append(f"  ({note['note']}) in {where}")

    # 2. Each repo's own sections, or one line when it has nothing past Waiting on you.
    for e in entries:
        lines.append("")
        if e["error"]:
            lines.append(f"{e['name']}: FAILED ({e['error']})")
            continue
        r = e["report"]
        if _quiet(r):
            branch = r["health"].get("branch") or "default branch"
            since = catchup._when(r["window"]["start"], tz)
            lines.append(
                f"{e['name']}: nothing since {since}; {branch} push: {_push_state(r['health'])}"
            )
            continue
        body = catchup.render(r, tz).splitlines()
        lines.append(body[0])
        lines += _sections(body)
    if report["skipped"]:
        lines.append("")
        lines.append(
            "Not read: "
            + ", ".join(f"{s['dir']} ({s['reason']})" for s in report["skipped"])
            + "; set an origin remote, or `git config tracker.web <url>`, to include one"
        )
    return "\n".join(lines) + "\n"


def _sections(body: list[str]) -> list[str]:
    """The single-repo text past its header, without Waiting on you (said above, for every repo)
    and without the ruling_pattern pointer the shared setup line already covers."""
    out: list[str] = []
    skipping = False
    for line in body[2:]:
        if line == "Waiting on you":
            skipping = True
            continue
        if line and not line.startswith(" "):
            skipping = False
        if skipping or line == "  (ruling_pattern isn't set; see setup above)":
            continue
        out.append(line)
    # Decisions made with nothing under it is the unset ruling_pattern, said in the setup line.
    return [
        line
        for i, line in enumerate(out)
        if not (line == "Decisions made" and (i + 1 == len(out) or not out[i + 1].startswith(" ")))
    ]
