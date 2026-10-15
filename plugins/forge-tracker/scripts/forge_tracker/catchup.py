"""/catchup: what changed and what's waiting on you since your last catchup. Read-only.

The window opens at the previous successful catchup's end (less five minutes, so a fast local
clock can't skip events), or seven days back when there's no marker. The marker moves only when
every read succeeded and the report printed; `--since` and `--no-mark` leave it alone.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional

from . import state
from .config import Config
from .detect import Remote
from .model import Change, ForgeError, Issue, Read, attempt, iso, parse_time

OVERLAP = timedelta(minutes=5)
FIRST_WINDOW = timedelta(days=7)
SCHEMA = 1
# Text lines quoting a comment are cut here; --json keeps the whole line.
LINE_LIMIT = 160

_SINCE = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str, now: datetime) -> datetime:
    """`90m`, `12h`, `3d`, `2w`, or an ISO date or time (UTC when it has no zone)."""
    value = value.strip()
    match = _SINCE.match(value)
    if match:
        return now - timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"--since takes 3d, 12h, 2w, 90m or an ISO time, not {value!r}") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    basis: str  # marker | first | since | no-identity
    note: Optional[str] = None


def marker_path(remote: Remote, user: str, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(state.cache_root(environ), "catchup", f"{remote.key}_{user}.json")


def window_from_marker(marker: Any, now: datetime) -> Window:
    end = parse_time(marker.get("end")) if isinstance(marker, dict) else None
    if marker is not None and end is None:
        return Window(now - FIRST_WINDOW, now, "first", "the marker was unreadable")
    if end is None:
        return Window(now - FIRST_WINDOW, now, "first")
    return Window(min(end - OVERLAP, now), now, "marker")


# --- gathering ------------------------------------------------------------------------------


def changelog_added(
    repo_dir: str,
    path: str,
    branch: str,
    since: datetime,
    run: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Bullet lines added to the changelog on origin/<branch> since `since`, from the local
    clone (fetched first by the command)."""
    ref = f"origin/{branch}"

    def git(*args: str) -> str:
        result = run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise ForgeError(f"git {args[0]} failed: {(result.stderr or '').strip()[:200]}")
        return result.stdout

    base = git("rev-list", "-1", f"--before={iso(since)}", ref).strip()
    # With no commit before the window, everything on the branch is new.
    base = base or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # the empty tree
    diff = git("diff", "--unified=0", "--no-color", base, ref, "--", path)
    added = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if text.startswith(("- ", "* ")):
                added.append(text[2:].strip())
    return added


def gather(
    adapter: Any,
    config: Config,
    login: Optional[str],
    window: Window,
    repo_dir: str,
    git_run: Callable[..., Any] = subprocess.run,
    workers: int = 6,
) -> dict[str, Read]:
    """Every read the report needs, each failing on its own."""
    start = window.start
    repo = attempt(adapter.repo)
    branch = repo.data.default_branch if repo.ok else None

    def needs(value: Any, what: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
        def wrap(fn: Callable[[], Any]) -> Callable[[], Any]:
            def run() -> Any:
                if value is None:
                    raise ForgeError(f"needs {what}, which failed")
                return fn()

            return run

        return wrap

    jobs: dict[str, Callable[[], Any]] = {
        "mentions": needs(login, "your identity")(lambda: adapter.mentions_since(login, start)),
        "open_changes": adapter.open_changes,
        "open_issues": adapter.open_issues,
        "merged": lambda: adapter.merged_since(start),
        "comments": lambda: adapter.comments_since(start),
        "issues_since": lambda: adapter.issues_since(start),
        "push_run": needs(branch, "the default branch")(lambda: adapter.latest_run(branch, "push")),
        "schedule_run": needs(branch, "the default branch")(
            lambda: adapter.latest_run(branch, "schedule")
        ),
        "flaky": needs(branch, "the default branch")(
            lambda: adapter.passed_on_retry(branch, start)
        ),
    }
    if config.report_tags:
        jobs["tags"] = lambda: adapter.tags(config.report_tags)
    if config.changelog:
        jobs["changelog"] = needs(branch, "the default branch")(
            lambda: changelog_added(repo_dir, config.changelog, branch, start, git_run)
        )
    reads: dict[str, Read] = {"repo": repo}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(attempt, fn) for name, fn in jobs.items()}
        for name, future in futures.items():
            reads[name] = future.result()
    return reads


# --- building the report ----------------------------------------------------------------------

_HASH_REF = re.compile(r"(?<![\w/])#(\d+)\b")
_DATEISH = re.compile(r"\d{4}[-._]\d{1,2}[-._]\d{1,2}(?:[-._]\d+)?")


def issue_numbers(change: Change) -> list[int]:
    """Issue numbers a merged change names: its closing references, `#N` in its title, and
    all-digit segments of its branch. Date-shaped and version-shaped runs in the branch
    (`2026-10-14`, `2026.10.15.1`) aren't issue numbers."""
    numbers = set(change.closing_issues)
    numbers.update(int(n) for n in _HASH_REF.findall(change.title))
    branch = _DATEISH.sub(" ", change.branch)
    for segment in re.split(r"[/\-_. ]+", branch):
        if segment.isdigit() and len(segment) <= 7:
            numbers.add(int(segment))
    numbers.discard(change.number)
    return sorted(numbers)


def _failed(reads: Mapping[str, Read], *names: str) -> list[dict[str, str]]:
    return [
        {"part": name, "reason": reads[name].error or ""}
        for name in names
        if name in reads and not reads[name].ok
    ]


def build(
    reads: Mapping[str, Read],
    config: Config,
    remote: Remote,
    forge: str,
    login: Optional[str],
    identity_error: Optional[str],
    window: Window,
    show_all: bool = False,
    marker: str = "move",
) -> dict[str, Any]:
    """The report as data. `marker` is move (it moves after printing), keep (--no-mark,
    --since, or no identity) or held (a read failed)."""
    start = window.start
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "project": remote.project,
        "host": remote.host,
        "forge": forge,
        "user": login,
        "window": {
            "start": iso(window.start),
            "end": iso(window.end),
            "basis": window.basis,
            "note": window.note,
        },
        "marker": marker,
    }
    failed: list[dict[str, str]] = []
    if identity_error:
        failed.append({"part": "identity", "reason": identity_error})

    # 1. Waiting on you.
    items: dict[str, dict[str, Any]] = {}
    standing: dict[str, int] = {}

    def add(key: str, entry: dict[str, Any], reason: str, recent: bool) -> None:
        if not recent and not show_all:
            standing[reason] = standing.get(reason, 0) + 1
            return
        existing = items.setdefault(key, dict(entry, reasons=[]))
        if reason not in existing["reasons"]:
            existing["reasons"].append(reason)

    def change_entry(c: Change) -> dict[str, Any]:
        return {
            "ref": f"#{c.number}",
            "kind": "change",
            "title": c.title,
            "updated": iso(c.updated),
            "draft": c.draft,
            "conflicts": c.conflicts,
            "url": c.url,
        }

    def issue_entry(i: Issue) -> dict[str, Any]:
        return {
            "ref": f"#{i.number}",
            "kind": "issue",
            "title": i.title,
            "updated": iso(i.updated),
            "labels": list(i.labels),
            "url": i.url,
        }

    if reads["mentions"].ok:
        for m in reads["mentions"].data:
            entry = {
                "ref": f"#{m.number}",
                "kind": "change" if m.is_change else "issue",
                "title": m.title,
                "updated": iso(m.updated),
                "url": m.url,
            }
            add(f"#{m.number}", entry, "mentioned", True)
    changes_by_number: dict[int, Change] = {}
    if reads["open_changes"].ok and login:
        for c in reads["open_changes"].data:
            changes_by_number[c.number] = c
            recent = c.updated >= start
            if login in c.reviewers:
                add(f"#{c.number}", change_entry(c), "review requested", recent)
            if login in c.assignees:
                add(f"#{c.number}", change_entry(c), "assigned", recent)
    needs_human, decision = config.label("needs_human"), config.label("decision")
    if reads["open_issues"].ok:
        for i in reads["open_issues"].data:
            recent = i.updated >= start
            if login and login in i.assignees:
                add(f"#{i.number}", issue_entry(i), "assigned", recent)
            if needs_human and needs_human in i.labels:
                add(f"#{i.number}", issue_entry(i), needs_human, recent)
            if decision and decision in i.labels:
                add(f"#{i.number}", issue_entry(i), decision, recent)
    # A mentioned change is shown with its draft and conflict state when the open list has it.
    for key, entry in items.items():
        number = int(key[1:])
        if entry["kind"] == "change" and "conflicts" not in entry and number in changes_by_number:
            entry.update(
                {k: v for k, v in change_entry(changes_by_number[number]).items() if k != "ref"}
            )
    waiting_failed = _failed(reads, "mentions", "open_changes", "open_issues")
    if not login and not identity_error:
        waiting_failed.append({"part": "identity", "reason": "no login"})
    notes = []
    if not needs_human:
        notes.append("labels.needs_human isn't set, so needs-human issues aren't listed")
    if not decision:
        notes.append("labels.decision isn't set, so decision issues aren't listed")
    report["waiting"] = {
        "items": sorted(items.values(), key=lambda e: e["updated"], reverse=True),
        "standing": standing,
        "failed": waiting_failed,
        "notes": notes,
    }
    failed.extend(waiting_failed)

    # 2. Merged since.
    merged: dict[str, Any] = {"groups": [], "changelog": None, "failed": []}
    if reads["merged"].ok:
        groups: dict[Optional[int], list[dict[str, Any]]] = {}
        for c in sorted(reads["merged"].data, key=lambda c: c.merged_at or c.updated):
            entry = {
                "ref": f"#{c.number}",
                "title": c.title,
                "branch": c.branch,
                "merged_at": iso(c.merged_at) if c.merged_at else None,
                "merge_sha": c.merge_sha,
                "closing_issues": list(c.closing_issues),
                "url": c.url,
            }
            for number in issue_numbers(c) or [None]:
                groups.setdefault(number, []).append(entry)
        ordered = sorted(n for n in groups if n is not None) + ([None] if None in groups else [])
        merged["groups"] = [
            {"issue": f"#{n}" if n is not None else None, "changes": groups[n]} for n in ordered
        ]
    merged["failed"] = _failed(reads, "merged")
    if "changelog" in reads:
        merged["changelog"] = reads["changelog"].data if reads["changelog"].ok else None
        merged["failed"] += _failed(reads, "changelog")
    report["merged"] = merged
    failed.extend(merged["failed"])

    # 3. Decisions made.
    decisions: dict[str, Any] = {"items": [], "failed": [], "note": None}
    if config.ruling_pattern is None:
        decisions["note"] = "ruling_pattern isn't set, so rulings aren't picked out"
    elif reads["comments"].ok:
        for c in reads["comments"].data:
            match = config.ruling_pattern.search(c.body)
            if match:
                line_start = c.body.rfind("\n", 0, match.start()) + 1
                line_end = c.body.find("\n", match.start())
                line = c.body[line_start : line_end if line_end >= 0 else None].strip()
                decisions["items"].append(
                    {
                        "ref": f"#{c.issue}",
                        "line": line,
                        "author": c.author,
                        "created": iso(c.created),
                        "url": c.url,
                    }
                )
        decisions["failed"] = []
    if config.ruling_pattern is not None:
        decisions["failed"] = _failed(reads, "comments")
    report["decisions"] = decisions
    failed.extend(decisions["failed"])

    # 4. Board movement.
    board: dict[str, Any] = {"tiers": [], "failed": _failed(reads, "issues_since")}
    if reads["issues_since"].ok:
        counts: dict[str, list[int]] = {}
        for i in reads["issues_since"].data:
            opened = i.created >= start
            closed = i.state == "closed" and i.closed is not None and i.closed >= start
            if not opened and not closed:
                continue
            tier = config.tier(i.labels, i.milestone.title if i.milestone else None) or "untiered"
            pair = counts.setdefault(tier, [0, 0])
            pair[0] += int(opened)
            pair[1] += int(closed)
        order = list(config.tier_labels) + list(config.milestone_prefixes) + ["untiered"]
        board["tiers"] = [
            {"tier": t, "opened": counts[t][0], "closed": counts[t][1]}
            for t in order
            if t in counts
        ]
    report["board"] = board
    failed.extend(board["failed"])

    # 5. Health.
    health: dict[str, Any] = {"failed": _failed(reads, "repo")}
    branch = reads["repo"].data.default_branch if reads["repo"].ok else None
    health["branch"] = branch
    for name in ("push_run", "schedule_run"):
        read = reads[name]
        if read.ok:
            run = read.data
            health[name] = (
                None
                if run is None
                else {
                    "id": run.id,
                    "name": run.name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "created": iso(run.created),
                    "failed_jobs": list(run.failed_jobs),
                    "url": run.url,
                }
            )
    if reads["flaky"].ok:
        health["flaky"] = [
            {"job": job, "times": times}
            for job, times in sorted(reads["flaky"].data.items(), key=lambda kv: (-kv[1], kv[0]))
            if times > 1
        ]
    if "tags" in reads and reads["tags"].ok:
        health["tags"] = [
            {
                "pattern": t.pattern,
                "name": t.name,
                "date": iso(t.date) if t.date else None,
                "note": t.note,
            }
            for t in reads["tags"].data
        ]
    health["failed"] += _failed(reads, "push_run", "schedule_run", "flaky", "tags")
    report["health"] = health
    failed.extend(f for f in health["failed"] if f not in failed)

    report["failed"] = failed
    report["ok"] = not failed
    return report


# --- rendering ------------------------------------------------------------------------------


def _when(value: Optional[str], tz: Optional[tzinfo]) -> str:
    when = parse_time(value)
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M") if when else "?"


def _day(value: Optional[str], tz: Optional[tzinfo]) -> str:
    when = parse_time(value)
    return when.astimezone(tz).strftime("%Y-%m-%d") if when else "?"


def _failed_lines(failed: list[dict[str, str]]) -> list[str]:
    return [f"  {f['part']}: FAILED ({f['reason']})" for f in failed]


def _run_line(label: str, run: Optional[dict[str, Any]]) -> str:
    if run is None:
        return f"  {label}: none"
    if run["status"] != "completed":
        state_text = run["status"]
    elif run["conclusion"] == "success":
        state_text = "passed"
    else:
        state_text = f"FAILED ({run['conclusion']})"
        if run["failed_jobs"]:
            state_text = f"FAILED (jobs: {', '.join(run['failed_jobs'])})"
    return f"  {label} run {run['id']} ({run['name']}): {state_text}"


def render(report: dict[str, Any], tz: Optional[tzinfo] = None) -> str:
    window = report["window"]
    basis = {
        "marker": "since your last catchup",
        "first": "first catchup here: the last 7 days",
        "since": "--since; the marker isn't read or moved",
        "no-identity": "identity FAILED, so the marker wasn't read: the last 7 days",
    }[window["basis"]]
    if window.get("note"):
        basis += f"; {window['note']}"
    if report["marker"] == "keep" and window["basis"] in ("marker", "first"):
        basis += "; --no-mark, so the marker stays"
    elif report["marker"] == "held":
        basis += "; a read FAILED, so the marker stays"
    start, end = _when(window["start"], tz), _when(window["end"], tz)
    lines = [f"{report['project']} on {report['host']}, {start} to {end} ({basis})", ""]

    waiting = report["waiting"]
    lines.append("Waiting on you")
    for item in waiting["items"]:
        reasons = ", ".join(item["reasons"])
        if item["kind"] == "change" and "conflicts" in item:
            extra = f" (draft {'yes' if item['draft'] else 'no'}, conflicts: {item['conflicts']})"
            lines.append(f"  {item['ref']} {reasons}: {item['title']}{extra}")
        else:
            lines.append(
                f"  {item['ref']} {reasons}, updated {_day(item['updated'], tz)}: {item['title']}"
            )
    if waiting["standing"]:
        parts = [f"{n} {reason}" for reason, n in waiting["standing"].items()]
        lines.append(f"  standing: {', '.join(parts)} (not updated in the window)")
    if not waiting["items"] and not waiting["standing"] and not waiting["failed"]:
        lines.append("  nothing")
    lines += _failed_lines(waiting["failed"])
    lines += [f"  ({note})" for note in waiting["notes"]]

    merged = report["merged"]
    lines.append("Merged since")
    for group in merged["groups"]:
        changes = group["changes"]
        listed = ", ".join(f"{c['ref']} {c['title']}" for c in changes)
        if group["issue"] is None:
            for c in changes:
                lines.append(f"  (no issue) {c['ref']} {c['title']}")
        else:
            count = f"{len(changes)} PR{'s' if len(changes) != 1 else ''}"
            lines.append(f"  {group['issue']}  {count}: {listed}")
    if merged["changelog"]:
        n = len(merged["changelog"])
        lines.append(f"  changelog: {n} {'entry' if n == 1 else 'entries'} added")
        lines += [f"    {entry}" for entry in merged["changelog"]]
    elif merged["changelog"] == []:
        lines.append("  changelog: no entries added")
    if not merged["groups"] and not merged["failed"]:
        lines.append("  nothing merged")
    lines += _failed_lines(merged["failed"])

    decisions = report["decisions"]
    lines.append("Decisions made")
    for d in decisions["items"]:
        line = d["line"] if len(d["line"]) <= LINE_LIMIT else d["line"][: LINE_LIMIT - 3] + "..."
        lines.append(f"  {d['ref']}  {line}")
    if decisions["note"]:
        lines.append(f"  ({decisions['note']})")
    elif not decisions["items"] and not decisions["failed"]:
        lines.append("  none")
    lines += _failed_lines(decisions["failed"])

    board = report["board"]
    lines.append("Board movement")
    if board["tiers"]:
        lines.append(
            "  " + "   ".join(f"{t['tier']} +{t['opened']} -{t['closed']}" for t in board["tiers"])
        )
    elif not board["failed"]:
        lines.append("  no issues opened or closed")
    lines += _failed_lines(board["failed"])

    health = report["health"]
    lines.append("Health")
    branch = health.get("branch") or "default branch"
    if "push_run" in health:
        lines.append(_run_line(f"{branch} push", health["push_run"]))
    if health.get("schedule_run") is not None:
        lines.append(_run_line("scheduled", health["schedule_run"]))
    if health.get("flaky"):
        for f in health["flaky"]:
            lines.append(f"  flaky: {f['job']} (passed on retry {f['times']} times)")
    elif "flaky" in health:
        lines.append("  flaky: none")
    if health.get("tags") is not None:
        for t in health["tags"]:
            if t["note"]:
                lines.append(f"  tags {t['pattern']}: {t['note']}")
            elif t["name"]:
                date = f" ({_day(t['date'], tz)})" if t["date"] else ""
                lines.append(f"  tags {t['pattern']}: {t['name']}{date}")
            else:
                lines.append(f"  tags {t['pattern']}: none match")
    lines += _failed_lines(health["failed"])

    identity = [f for f in report["failed"] if f["part"] == "identity"]
    if identity:
        lines.append("")
        lines += [line.strip() for line in _failed_lines(identity)]
    return "\n".join(lines) + "\n"
