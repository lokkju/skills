"""/next: what to work on. Your queue, the lanes agents can start now, and what's blocked.

Two layers, both cached under `${XDG_CACHE_HOME:-~/.cache}/forge-tracker/next/<host>_<project>/`:

- **Facts** (this module, deterministic): open issues with tier, due date, labels, owners and
  blocking links; open changes with head pipeline and conflict state; branches with no change;
  and the closing references that say which change will close which issue. They're kept in
  `facts.json` with a fingerprint of cheap reads, and reused while the fingerprint holds. A run
  with a failed read is written for inspection but never reused.
- **The slate** (Claude's ranking): the queue order and the agent lanes, saved with
  `forge-tracker next save-slate` into `slate.json` beside the facts, with their fingerprint. It's
  shown while the fingerprint holds; when it moves, the report asks for a new one.

Classification is the spec's: person-only is the needs-human or decision label (firm) or a
`person_only_markers` word in the title or labels (a hint Claude may overrule with a reason);
blocked is an open blocker by link, or the blocked label; agent-ready is open, not person-only
and not blocked. An assignee is never a reason for anything; it's shown as owner.

Agent-ready needs a label that could have said otherwise. With neither the needs-human nor the
decision label configured, an item that's neither blocked nor marked is `unclassified`, not
agent-ready: the CLI can't judge prose, so the slate places each one, in the queue with a stated
reason (`person_only`) or in a lane. In a labelled repo an agent-ready item without the agent
label is `untagged`: agent-ready by default, not by a label, and the slate may still judge it
person-only with a reason.

It recommends only: nothing here claims, assigns, comments or branches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, tzinfo
from typing import Any, Callable, Optional

from . import refs, state
from .config import PATH as CONFIG_PATH
from .config import SETUP_PREFIX, Config
from .detect import Remote
from .model import Branch, Change, ClosingRef, Issue, Link, Read, attempt, iso, parse_time

SCHEMA = 1
# Issue bodies are kept in the facts for Claude's lane grouping, cut to this many characters.
BODY_LIMIT = 4000

# --- the fingerprint ----------------------------------------------------------------------------


def source_tag() -> str:
    """A hash of the package's own Python source, so facts cached by older code aren't reused."""
    root = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                digest.update(os.path.relpath(path, root).encode() + b"\0")
                with open(path, "rb") as handle:
                    digest.update(handle.read())
                digest.update(b"\0")
    return digest.hexdigest()[:16]


def config_digest(repo_dir: str) -> str:
    """A hash of `.claude/forge-tracker.local.md`, since classification and the slate depend on
    its labels, tiers and markers. `none` when there's no file."""
    try:
        with open(os.path.join(repo_dir, CONFIG_PATH), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "none"


def fingerprint(inputs: Mapping[str, Any], source: str, config: str) -> str:
    blob = json.dumps(
        {"inputs": inputs, "source": source, "config": config, "schema": SCHEMA}, sort_keys=True
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def cache_dir(remote: Remote, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(state.cache_root(environ), "next", remote.key)


# --- phase 2 seam -----------------------------------------------------------------------------


def recorded_pickups(remote: Remote, environ: Mapping[str, str] = os.environ) -> dict[str, int]:
    """PHASE 2 SEAM: branch name -> issue number, from `forge-tracker issue <N> --pickup`.

    The pickup record (`.../forge-tracker/pickup/<host>_<project>.json`) is written by phase 2,
    which hasn't shipped, so there's nothing to read yet and this returns no pickups. Until then
    an issue is in flight only through what the forge itself shows: a change with a closing
    reference to it, a change or branch whose name has the issue number as a segment, or a
    change whose title says `#N`. When phase 2 lands, read its file here; `in_flight` already
    accepts `via: "pickup"`.
    """
    return {}


# --- gathering ----------------------------------------------------------------------------------


def _issue(i: Issue) -> dict[str, Any]:
    return {
        "number": i.number,
        "title": i.title,
        "labels": list(i.labels),
        "assignees": list(i.assignees),
        "milestone": {"title": i.milestone.title, "due": i.milestone.due} if i.milestone else None,
        "created": iso(i.created),
        "updated": iso(i.updated),
        "url": i.url,
        "body": i.body[:BODY_LIMIT],
        "body_cut": len(i.body) > BODY_LIMIT,
    }


def _change(c: Change) -> dict[str, Any]:
    return {
        "number": c.number,
        "title": c.title,
        "branch": c.branch,
        "draft": c.draft,
        "conflicts": c.conflicts,
        "pipeline": c.pipeline,
        "review": c.review,
        "author": c.author,
        "assignees": list(c.assignees),
        "reviewers": list(c.reviewers),
        "updated": iso(c.updated),
        "url": c.url,
    }


def _link(link: Link) -> dict[str, Any]:
    return {
        "blocked": link.blocked,
        "blocker": link.blocker,
        "blocker_open": link.blocker_open,
        "kind": link.kind,
        "blocker_project": link.blocker_project,
    }


def _closing(ref: ClosingRef) -> dict[str, Any]:
    return {
        "issue": ref.issue,
        "change": ref.change,
        "state": ref.state,
        "branch": ref.branch,
        "base": ref.base,
        "merged_at": iso(ref.merged_at) if ref.merged_at else None,
        "change_project": ref.change_project,
    }


def _branch(b: Branch) -> dict[str, Any]:
    return {"name": b.name, "sha": b.sha, "has_change": b.has_change, "ahead_by": b.ahead_by}


def gather(adapter: Any, default_branch: Optional[str], workers: int = 4) -> dict[str, Read]:
    """Every facts read, each failing on its own. Branches need the open changes (to tell which
    branches have one) and the default branch, so they're read after both."""
    reads: dict[str, Read] = {}
    if default_branch is None:
        repo = attempt(adapter.repo)
        reads["repo"] = repo
        default_branch = repo.data.default_branch if repo.ok else None
    jobs: dict[str, Callable[[], Any]] = {
        "issues": adapter.open_issues,
        "links": adapter.blocking_links,
        "changes": adapter.open_changes,
        "closing": adapter.closing_refs,
    }
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(attempt, fn) for name, fn in jobs.items()}
        for name, future in futures.items():
            reads[name] = future.result()
    changes = reads["changes"]
    if not changes.ok:
        reads["branches"] = Read(error="needs the open changes, which failed")
    elif default_branch is None:
        reads["branches"] = Read(error="needs the default branch, which failed")
    else:
        reads["branches"] = attempt(lambda: adapter.branches(default_branch, changes.data))
    return reads


_SERIALIZE: dict[str, Callable[[Any], Any]] = {
    "issues": _issue,
    "links": _link,
    "changes": _change,
    "closing": _closing,
    "branches": _branch,
}


def facts_from(
    reads: Mapping[str, Read],
    fp_read: Read,
    fp: Optional[str],
    remote: Remote,
    forge: str,
    now: datetime,
) -> dict[str, Any]:
    parts: dict[str, Any] = {}
    for name, serialize in _SERIALIZE.items():
        read = reads[name]
        parts[name] = (
            {"data": [serialize(x) for x in read.data]} if read.ok else {"error": read.error}
        )
    failed = []
    if not fp_read.ok:
        failed.append({"part": "fingerprint", "reason": fp_read.error})
    if "repo" in reads and not reads["repo"].ok:
        failed.append({"part": "repo", "reason": reads["repo"].error})
    failed += [{"part": n, "reason": p["error"]} for n, p in parts.items() if "error" in p]
    default_branch = None
    if fp_read.ok:
        default_branch = fp_read.data["default_branch"]
    elif "repo" in reads and reads["repo"].ok:
        default_branch = reads["repo"].data.default_branch
    return {
        "schema": SCHEMA,
        "project": remote.project,
        "host": remote.host,
        "forge": forge,
        "read_at": iso(now),
        "default_branch": default_branch,
        "fingerprint": fp,
        "inputs": fp_read.data if fp_read.ok else None,
        "parts": parts,
        "failed": failed,
        "ok": not failed,
    }


def reusable(cached: Any, fp: Optional[str]) -> bool:
    return (
        fp is not None
        and isinstance(cached, dict)
        and cached.get("schema") == SCHEMA
        and cached.get("ok") is True
        and cached.get("fingerprint") == fp
        and isinstance(cached.get("parts"), dict)
    )


def load_facts(
    adapter: Any,
    remote: Remote,
    forge: str,
    repo_dir: str,
    directory: str,
    now: datetime,
    refresh: bool = False,
) -> tuple[dict[str, Any], str]:
    """The facts and where they came from (`cache` or `read`). The fingerprint is always read;
    the facts are read only when it moved, when it failed, or with `refresh`."""
    fp_read = attempt(adapter.fingerprint_inputs)
    fp = fingerprint(fp_read.data, source_tag(), config_digest(repo_dir)) if fp_read.ok else None
    path = os.path.join(directory, "facts.json")
    cached = state.read_json(path)
    if not refresh and reusable(cached, fp):
        return cached, "cache"
    reads = gather(adapter, fp_read.data["default_branch"] if fp_read.ok else None)
    facts = facts_from(reads, fp_read, fp, remote, forge, now)
    state.write_json(path, facts)
    return facts, "read"


# --- classification -----------------------------------------------------------------------------


def _word(marker: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(marker) + r"(?![\w])", re.IGNORECASE)


def _strongly_connected(nodes: list[int], edges: Mapping[int, set[int]]) -> list[list[int]]:
    """Tarjan's algorithm, iterative so a long chain can't hit the recursion limit."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    out: list[list[int]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                out.append(sorted(component))
    return out


def _part(facts: Mapping[str, Any], name: str) -> Optional[list[Any]]:
    return (facts.get("parts") or {}).get(name, {}).get("data")


def classify(
    facts: Mapping[str, Any], config: Config, pickups: Optional[Mapping[str, int]] = None
) -> dict[str, Any]:
    """Every open issue with its class, tier, due date, owners, blockers, unblock count, what
    has it in flight, and flags; plus the cycles and the branches no issue claims."""
    issues = _part(facts, "issues")
    links = _part(facts, "links")
    changes = _part(facts, "changes")
    closing = _part(facts, "closing")
    branches = _part(facts, "branches")
    pickups = pickups or {}
    result: dict[str, Any] = {
        "items": [],
        "cycles": [],
        "other_branches": [],
        "other_changes": [],
        "open_changes": [],
        "notes": [],
        "labelled": False,
        "blocked_label": False,
    }
    needs_human, decision = config.label("needs_human"), config.label("decision")
    # Without a person-only label, "no label says a person must act" says nothing.
    labelled = bool(needs_human or decision)
    result["labelled"] = labelled
    blocked_label, agent_label = config.label("blocked"), config.label("agent")
    result["blocked_label"] = bool(blocked_label)
    setup = config.setup_note(("labels.needs_human", "labels.decision"))
    if setup:
        result["notes"].append(setup)
    if issues is None:
        return result

    open_numbers = {i["number"] for i in issues}
    markers = [(m, _word(m)) for m in config.person_only_markers]
    order = list(config.tier_labels) + list(config.milestone_prefixes)

    # Blocking links: open blockers block; a closed blocker is a stale link for /groom.
    blockers: dict[int, list[dict[str, Any]]] = {}
    stale: dict[int, list[dict[str, Any]]] = {}
    edges: dict[int, set[int]] = {}
    for link in links or []:
        if link["blocked"] not in open_numbers:
            continue
        project = link.get("blocker_project")
        ref = f"{project}#{link['blocker']}" if project else f"#{link['blocker']}"
        entry = {"ref": ref, "kind": link["kind"]}
        if link["blocker_open"]:
            blockers.setdefault(link["blocked"], []).append(entry)
            if not project and link["blocker"] in open_numbers:
                edges.setdefault(link["blocker"], set()).add(link["blocked"])
        else:
            stale.setdefault(link["blocked"], []).append(entry)

    # What has each issue in flight, and which merged changes close an issue that's still open.
    in_flight: dict[int, list[dict[str, Any]]] = {}
    merged_by: dict[int, list[dict[str, Any]]] = {}
    attached: set[str] = set()  # refs of open changes that have an open issue in flight
    named: dict[str, set[int]] = {}  # every issue number each open change names, open or not

    def fly(number: int, entry: dict[str, Any]) -> None:
        if entry["kind"] == "change":
            named.setdefault(entry["ref"], set()).add(number)
        if number not in open_numbers:
            return
        if entry["kind"] == "change":
            attached.add(entry["ref"])
        listed = in_flight.setdefault(number, [])
        same = [e for e in listed if e["kind"] == entry["kind"] and e["ref"] == entry["ref"]]
        if same:
            same[0]["via"].extend(v for v in entry["via"] if v not in same[0]["via"])
        else:
            listed.append(entry)

    closes: dict[int, set[int]] = {}
    for ref in closing or []:
        if ref["change_project"]:
            continue
        if ref["state"] == "open":
            closes.setdefault(ref["change"], set()).add(ref["issue"])
        elif ref["state"] == "merged":
            merged_by.setdefault(ref["issue"], []).append(
                {"ref": f"#{ref['change']}", "base": ref["base"], "merged_at": ref["merged_at"]}
            )

    def change_entry(c: dict[str, Any], via: str) -> dict[str, Any]:
        return {
            "kind": "change",
            "ref": f"#{c['number']}",
            "branch": c["branch"],
            "draft": c["draft"],
            "conflicts": c["conflicts"],
            "pipeline": c["pipeline"],
            "review": c.get("review"),
            "via": [via],
        }

    for c in changes or []:
        for number in sorted(closes.get(c["number"], ())):
            fly(number, change_entry(c, "closing reference"))
        for number in sorted(refs.title_numbers(c["title"]) - {c["number"]}):
            fly(number, change_entry(c, "title"))
        for number in sorted(refs.branch_numbers(c["branch"]) - {c["number"]}):
            fly(number, change_entry(c, "branch name"))
    # An open change the changes read missed (it failed) still counts through its closing ref.
    if changes is None:
        for number, issue_numbers in closes.items():
            for issue_number in sorted(issue_numbers):
                fly(
                    issue_number,
                    {"kind": "change", "ref": f"#{number}", "via": ["closing reference"]},
                )
    claimed_branches: set[str] = set()
    for b in branches or []:
        if b["has_change"]:
            continue
        numbers = refs.branch_numbers(b["name"])
        if b["name"] in pickups:
            numbers.add(pickups[b["name"]])
        matched = False
        for number in sorted(numbers & open_numbers):
            matched = True
            if b["ahead_by"] == 0:
                continue  # nothing on it past the default branch: not work in flight
            via = "pickup" if pickups.get(b["name"]) == number else "branch name"
            fly(
                number,
                {"kind": "branch", "ref": b["name"], "ahead_by": b["ahead_by"], "via": [via]},
            )
        if matched:
            claimed_branches.add(b["name"])
        elif b["ahead_by"] != 0:
            result["other_branches"].append({"name": b["name"], "ahead_by": b["ahead_by"]})
    # Changes whose branch is in the pickup record but whose name doesn't say the number.
    for branch, number in pickups.items():
        for c in changes or []:
            if c["branch"] == branch:
                fly(number, change_entry(c, "pickup"))

    # Every open change, for the slate's recommendation (what's ready to merge).
    result["open_changes"] = [
        {
            "ref": f"#{c['number']}",
            "title": c["title"],
            "draft": c["draft"],
            "conflicts": c["conflicts"],
            "pipeline": c["pipeline"],
            "review": c.get("review"),
        }
        for c in changes or []
    ]

    # Open changes with no open issue behind them: the mirror of a branch with no change.
    for c in changes or []:
        ref = f"#{c['number']}"
        if ref in attached:
            continue
        result["other_changes"].append(
            {
                "ref": ref,
                "title": c["title"],
                "branch": c["branch"],
                "draft": c["draft"],
                "conflicts": c["conflicts"],
                "pipeline": c["pipeline"],
                "review": c.get("review"),
                "author": c.get("author"),
                "updated": c["updated"],
                "url": c["url"],
                "names": [f"#{n}" for n in sorted(named.get(ref, ()))],
            }
        )

    items: dict[int, dict[str, Any]] = {}
    for i in issues:
        number = i["number"]
        labels = i["labels"]
        milestone = i.get("milestone") or {}
        firm = [label for label in (needs_human, decision) if label and label in labels]
        hints = sorted(
            {
                marker
                for marker, pattern in markers
                if pattern.search(i["title"]) or any(pattern.search(label) for label in labels)
            }
        )
        open_blockers = blockers.get(number, [])
        labelled_blocked = bool(blocked_label and blocked_label in labels)
        if open_blockers or labelled_blocked:
            blocked: Optional[bool] = True
        elif links is None:
            blocked = None  # the links read failed: can't tell
        else:
            blocked = False
        person_only = bool(firm or hints)
        if blocked:
            kind = "blocked"
        elif person_only:
            kind = "person-only"
        elif blocked is None:
            kind = "unknown"
        elif not labelled:
            kind = "unclassified"
        else:
            kind = "agent-ready"
        tier = config.tier(tuple(labels), milestone.get("title"))
        flags = []
        if labelled_blocked and not open_blockers and links is not None:
            flags.append("blocked label with no open blocker linked")
        items[number] = {
            "ref": f"#{number}",
            "number": number,
            "title": i["title"],
            "url": i["url"],
            "class": kind,
            "tier": tier,
            "tier_rank": order.index(tier) if tier in order else len(order),
            "due": (milestone.get("due") or "")[:10] or None,
            "milestone": milestone.get("title"),
            "labels": labels,
            "owners": i["assignees"],
            "agent_label": bool(agent_label and agent_label in labels),
            # Agent-ready only because no label said otherwise: the default, not a judgement.
            "untagged": kind == "agent-ready" and not (agent_label and agent_label in labels),
            "person_only": {"labels": firm, "markers": hints},
            "blocked_by": open_blockers,
            "blocked_label": labelled_blocked,
            "stale_links": stale.get(number, []),
            "in_flight": in_flight.get(number, []),
            "merged_by": merged_by.get(number, []) if closing is not None else None,
            "unblocks": None,
            "cycle": False,
            "flags": flags,
            "updated": i["updated"],
            "body": i.get("body", ""),
            "body_cut": i.get("body_cut", False),
        }

    # Unblock counts: everything reachable through open links, each item once (a visited set),
    # counting the items that would be agent work once unblocked (not person-only).
    if links is not None:
        components = _strongly_connected(sorted(items), edges)
        in_cycle: set[int] = set()
        for component in components:
            if len(component) > 1 or component[0] in edges.get(component[0], ()):
                in_cycle.update(component)
                result["cycles"].append([f"#{n}" for n in component])
        for number, item in items.items():
            seen: set[int] = set()
            todo = list(edges.get(number, ()))
            while todo:
                current = todo.pop()
                if current in seen or current == number:
                    continue
                seen.add(current)
                todo.extend(edges.get(current, ()))
            counted = sorted(
                n
                for n in seen
                if not items[n]["person_only"]["labels"] and not items[n]["person_only"]["markers"]
            )
            item["unblocks"] = [f"#{n}" for n in counted]
            item["cycle"] = number in in_cycle or bool(seen & in_cycle)
    result["items"] = [items[n] for n in sorted(items)]
    return result


def _due_key(item: Mapping[str, Any]) -> str:
    return item["due"] or "9999-99-99"


def sections(classified: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    items = classified["items"]
    person = [i for i in items if i["class"] == "person-only"]
    person.sort(key=lambda i: (-len(i["unblocks"] or []), i["tier_rank"], _due_key(i), i["number"]))
    ready = [i for i in items if i["class"] == "agent-ready"]
    ready.sort(
        key=lambda i: (i["tier_rank"], 0 if i["agent_label"] else 1, _due_key(i), i["number"])
    )
    blocked = [i for i in items if i["class"] == "blocked"]
    blocked.sort(key=lambda i: (i["tier_rank"], _due_key(i), i["number"]))
    unknown = [i for i in items if i["class"] == "unknown"]
    unknown.sort(key=lambda i: (i["tier_rank"], _due_key(i), i["number"]))
    unclassified = [i for i in items if i["class"] == "unclassified"]
    unclassified.sort(key=lambda i: (i["tier_rank"], _due_key(i), i["number"]))
    return {
        "queue": person,
        "agent_ready": ready,
        "blocked": blocked,
        "unknown": unknown,
        "unclassified": unclassified,
    }


# Every section an item can be listed in, in report order.
SECTIONS = ("queue", "agent_ready", "blocked", "unknown", "unclassified")


# --- the slate ----------------------------------------------------------------------------------


class SlateError(ValueError):
    """A slate that doesn't fit the facts; the message lists every problem."""


def _ref_number(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"#?\d+", value.strip()):
        return int(value.strip().lstrip("#"))
    return None


def validate_slate(payload: Any, classified: Mapping[str, Any]) -> dict[str, Any]:
    """Check Claude's slate against the facts and return it normalized. The rules: every ref is
    an open issue; an item appears once; the queue holds person-only items, and items no label
    decided (unclassified, or untagged agent-ready) judged person-only with a reason in
    `person_only`; a lane holds agent-ready items, unclassified items, or items person-only only
    by a marker hint, with an overrule reason. `recommend`, when present, is checked by
    `validate_recommend` against the slate it closes."""
    if not isinstance(payload, dict):
        raise SlateError("the slate must be a JSON object with queue and lanes")
    unknown_keys = sorted(
        set(payload) - {"queue", "lanes", "overrules", "person_only", "note", "recommend"}
    )
    errors = [f"unknown key {k}" for k in unknown_keys]
    items = {i["number"]: i for i in classified["items"]}
    seen: set[int] = set()

    def resolve(value: Any, where: str) -> Optional[int]:
        number = _ref_number(value)
        if number is None:
            errors.append(f"{where}: {value!r} isn't an issue reference like #12")
            return None
        if number not in items:
            errors.append(f"{where}: #{number} isn't an open issue in the facts")
            return None
        if number in seen:
            errors.append(f"{where}: #{number} appears more than once in the slate")
            return None
        seen.add(number)
        return number

    overrules: dict[int, str] = {}
    raw_overrules = payload.get("overrules") or {}
    if not isinstance(raw_overrules, dict):
        errors.append("overrules must be an object of {ref: reason}")
        raw_overrules = {}
    for key, reason in raw_overrules.items():
        number = _ref_number(key)
        if number is None or number not in items:
            errors.append(f"overrules: {key!r} isn't an open issue in the facts")
            continue
        item = items[number]
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"overrules: #{number} needs a stated reason")
        elif item["person_only"]["labels"]:
            errors.append(
                f"overrules: #{number} is person-only by label "
                f"({', '.join(item['person_only']['labels'])}); only marker hints can be overruled"
            )
        elif not item["person_only"]["markers"]:
            errors.append(f"overrules: #{number} isn't person-only, so there's nothing to overrule")
        else:
            overrules[number] = reason.strip()

    # Items the slate judged person-only, each with the reason that put it in the queue: any
    # item no label decided (no person-only or agent label) that isn't blocked. A labelled item
    # stays as its label says.
    judged: dict[int, str] = {}
    raw_judged = payload.get("person_only") or {}
    if not isinstance(raw_judged, dict):
        errors.append("person_only must be an object of {ref: reason}")
        raw_judged = {}
    for key, reason in raw_judged.items():
        number = _ref_number(key)
        if number is None or number not in items:
            errors.append(f"person_only: {key!r} isn't an open issue in the facts")
            continue
        item = items[number]
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"person_only: #{number} needs a stated reason")
        elif item["person_only"]["labels"]:
            errors.append(
                f"person_only: #{number} is person-only by label "
                f"({', '.join(item['person_only']['labels'])}); it's in the queue already"
            )
        elif item["agent_label"]:
            errors.append(
                f"person_only: #{number} has the agent label; a labelled item stays as its "
                "label says"
            )
        elif item["class"] not in ("agent-ready", "unclassified", "person-only"):
            errors.append(
                f"person_only: #{number} is {item['class']}; only an item that isn't blocked "
                "and has no person-only or agent label goes in the queue by judgement"
            )
        else:
            judged[number] = reason.strip()

    queue: list[str] = []
    raw_queue = payload.get("queue")
    if not isinstance(raw_queue, list):
        errors.append("queue must be a list of issue references")
        raw_queue = []
    for position, value in enumerate(raw_queue, 1):
        number = resolve(value, f"queue item {position}")
        if number is None:
            continue
        item = items[number]
        if number in judged:
            queue.append(f"#{number}")
            continue
        if item["class"] == "unclassified" or (
            item["class"] == "agent-ready" and not item["agent_label"]
        ):
            errors.append(
                f"queue item {position}: #{number} is {item['class']}"
                + (" (untagged)" if item["untagged"] else "")
                + "; add it to person_only with the reason it needs a person"
            )
            continue
        if item["class"] != "person-only" or number in overrules:
            errors.append(
                f"queue item {position}: #{number} is {item['class']}"
                + (" (overruled)" if number in overrules else "")
                + ", not person-only"
            )
            continue
        queue.append(f"#{number}")

    lanes: list[dict[str, Any]] = []
    raw_lanes = payload.get("lanes")
    if not isinstance(raw_lanes, list):
        errors.append("lanes must be a list of {area, items, why}")
        raw_lanes = []
    for position, lane in enumerate(raw_lanes, 1):
        where = f"lane {position}"
        if not isinstance(lane, dict):
            errors.append(f"{where} must be an object with area and items")
            continue
        extra = sorted(set(lane) - {"area", "items", "why"})
        if extra:
            errors.append(f"{where}: unknown key {', '.join(extra)}")
        area = lane.get("area")
        if not isinstance(area, str) or not area.strip():
            errors.append(f"{where} needs an area (the files or part of the repo it touches)")
            area = ""
        refs = lane.get("items")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{where} needs at least one item")
            refs = []
        kept = []
        for value in refs:
            number = resolve(value, where)
            if number is None:
                continue
            item = items[number]
            if item["class"] in ("agent-ready", "unclassified") or (
                item["class"] == "person-only" and number in overrules
            ):
                kept.append(f"#{number}")
            elif item["class"] == "person-only" and not item["person_only"]["labels"]:
                errors.append(
                    f"{where}: #{number} is person-only by marker "
                    f"({', '.join(item['person_only']['markers'])}); "
                    "add it to overrules with a reason to put it in a lane"
                )
            else:
                errors.append(f"{where}: #{number} is {item['class']}, not agent-ready")
        why = lane.get("why")
        if why is not None and not isinstance(why, str):
            errors.append(f"{where}: why must be a string")
            why = None
        lanes.append({"area": area.strip(), "items": kept, "why": (why or "").strip() or None})
    for number in overrules:
        if f"#{number}" not in {ref for lane in lanes for ref in lane["items"]}:
            errors.append(f"overrules: #{number} is overruled but in no lane")
    for number in judged:
        if f"#{number}" not in queue:
            errors.append(f"person_only: #{number} is judged person-only but isn't in the queue")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        errors.append("note must be a string")
    recommend = None
    if payload.get("recommend") is not None:
        recommend = validate_recommend(payload["recommend"], queue, lanes, classified, errors)
    if errors:
        raise SlateError("; ".join(errors))
    return {
        "queue": queue,
        "lanes": lanes,
        "overrules": {f"#{n}": reason for n, reason in overrules.items()},
        "person_only": {f"#{n}": reason for n, reason in judged.items()},
        "note": note,
        "recommend": recommend,
    }


def lane_label(index: int) -> str:
    """A lane's name in the report: A to Z, then its 1-based number."""
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def merge_ready(change: Mapping[str, Any]) -> list[str]:
    """Why an open change isn't ready to merge; empty when it is. Ready means not a draft, no
    conflicts (`unknown` is never clean), CI green, and an approved review when one is required
    (GitHub reports no review decision when the branch requires none)."""
    why = []
    if change.get("draft"):
        why.append("it's a draft")
    if change.get("conflicts") != "no":
        why.append(f"conflicts: {change.get('conflicts')}")
    if change.get("pipeline") != "success":
        why.append(f"ci: {change.get('pipeline') or 'none'}")
    if change.get("review") not in (None, "approved"):
        why.append(f"review: {str(change.get('review')).replace('_', ' ')}")
    return why


RECOMMEND_KEYS = ("you", "agent_cap", "start", "wait", "merge", "not_yet")


def validate_recommend(
    raw: Any,
    queue: list[str],
    lanes: list[dict[str, Any]],
    classified: Mapping[str, Any],
    errors: list[str],
) -> Optional[dict[str, Any]]:
    """Check the slate's closing recommendation against the facts and the slate itself, adding
    every problem to `errors`. `you` picks come from the queue (one, then up to two more);
    `start` and `wait` name every lane once, with `start` within `agent_cap` when it's given;
    `merge` holds only open PRs ready to merge; `not_yet` names open issues or PRs the rest of
    the recommendation doesn't pick. Every entry needs a reason."""
    if not isinstance(raw, dict):
        errors.append("recommend must be an object with you, start, wait, merge and not_yet")
        return None
    errors.extend(f"recommend: unknown key {k}" for k in sorted(set(raw) - set(RECOMMEND_KEYS)))
    items = {i["ref"]: i for i in classified["items"]}
    changes = {c["ref"]: c for c in classified.get("open_changes") or []}

    def entries(key: str, what: str) -> list[tuple[int, dict[str, Any], str]]:
        value = raw.get(key) or []
        if not isinstance(value, list):
            errors.append(f"recommend.{key} must be a list of {{{what}, why}}")
            return []
        out = []
        for position, entry in enumerate(value, 1):
            where = f"recommend.{key} {position}"
            if not isinstance(entry, dict):
                errors.append(f"{where} must be an object with {what} and why")
                continue
            extra = sorted(set(entry) - {what, "why"})
            if extra:
                errors.append(f"{where}: unknown key {', '.join(extra)}")
            why = entry.get("why")
            if not isinstance(why, str) or not why.strip():
                errors.append(f"{where} needs a stated reason (why)")
                continue
            out.append((position, entry, why.strip()))
        return out

    def ref_of(value: Any) -> Optional[str]:
        number = _ref_number(value)
        return None if number is None else f"#{number}"

    you = []
    picked: set[str] = set()
    for position, entry, why in entries("you", "ref"):
        where = f"recommend.you {position}"
        ref = ref_of(entry.get("ref"))
        if ref is None or ref not in items:
            errors.append(f"{where}: {entry.get('ref')!r} isn't an open issue in the facts")
        elif ref not in queue:
            errors.append(f"{where}: {ref} isn't in the slate's queue; picks for you come from it")
        elif ref in picked:
            errors.append(f"{where}: {ref} is picked more than once")
        else:
            picked.add(ref)
            you.append({"ref": ref, "title": items[ref]["title"], "why": why})
    if len(you) > 3:
        errors.append("recommend.you: one pick, then at most two after it")

    cap = raw.get("agent_cap")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        errors.append("recommend.agent_cap must be a whole number of agents")
        cap = None

    labels = {lane_label(n): n for n in range(len(lanes))}
    placed: dict[str, str] = {}

    def lanes_in(key: str) -> list[dict[str, Any]]:
        out = []
        for position, entry, why in entries(key, "lane"):
            where = f"recommend.{key} {position}"
            value = entry.get("lane")
            if isinstance(value, int) and not isinstance(value, bool):
                label = lane_label(value - 1) if 1 <= value <= len(lanes) else None
            elif isinstance(value, str):
                label = value.strip().upper()
                label = label if label in labels else None
            else:
                label = None
            if label is None:
                errors.append(f"{where}: {value!r} isn't a lane in the slate (A, B, ...)")
                continue
            if label in placed:
                errors.append(f"{where}: lane {label} is already in recommend.{placed[label]}")
                continue
            placed[label] = key
            out.append({"lane": label, "area": lanes[labels[label]]["area"], "why": why})
        return out

    start = lanes_in("start")
    wait = lanes_in("wait")
    if raw.get("start") is not None or raw.get("wait") is not None:
        missing = [label for label in labels if label not in placed]
        if missing:
            errors.append(
                "recommend: every lane starts now or waits, with a reason; "
                f"missing {', '.join(missing)}"
            )
    if cap is not None and len(start) > cap:
        errors.append(f"recommend.start: {len(start)} lanes is more than the agent cap of {cap}")

    merge = []
    merging: set[str] = set()
    for position, entry, why in entries("merge", "ref"):
        where = f"recommend.merge {position}"
        ref = ref_of(entry.get("ref"))
        if ref is None or ref not in changes:
            errors.append(f"{where}: {entry.get('ref')!r} isn't an open PR in the facts")
            continue
        if ref in merging:
            errors.append(f"{where}: {ref} is listed more than once")
            continue
        blockers = merge_ready(changes[ref])
        if blockers:
            errors.append(f"{where}: {ref} isn't ready to merge ({', '.join(blockers)})")
            continue
        merging.add(ref)
        merge.append({"ref": ref, "title": changes[ref]["title"], "why": why})

    started = {ref for lane in start for ref in lanes[labels[lane["lane"]]]["items"]}
    not_yet = []
    held: set[str] = set()
    for position, entry, why in entries("not_yet", "ref"):
        where = f"recommend.not_yet {position}"
        ref = ref_of(entry.get("ref"))
        if ref is None or (ref not in items and ref not in changes):
            errors.append(f"{where}: {entry.get('ref')!r} isn't an open issue or PR in the facts")
            continue
        clash = (
            "picked for you"
            if ref in picked
            else "in a lane that starts now"
            if ref in started
            else "in recommend.merge"
            if ref in merging
            else "listed more than once"
            if ref in held
            else None
        )
        if clash:
            errors.append(f"{where}: {ref} is {clash}")
            continue
        held.add(ref)
        kind, source = ("issue", items) if ref in items else ("pr", changes)
        not_yet.append({"ref": ref, "kind": kind, "title": source[ref]["title"], "why": why})
    return {
        "you": you,
        "agent_cap": cap,
        "start": start,
        "wait": wait,
        "merge": merge,
        "not_yet": not_yet,
    }


def save_slate(
    payload: Any, directory: str, config: Config, repo_dir: str, now: datetime
) -> dict[str, Any]:
    """Validate the slate against the cached facts and store it with their fingerprint. Reads
    nothing from the forge; the facts must be complete and current for this config and code."""
    facts = state.read_json(os.path.join(directory, "facts.json"))
    if not isinstance(facts, dict) or facts.get("schema") != SCHEMA:
        raise SlateError("no facts are cached for this repo; run forge-tracker next first")
    if not facts.get("ok") or not facts.get("fingerprint"):
        raise SlateError(
            "the cached facts had a failed read; a slate is saved only over complete facts"
        )
    current = fingerprint(facts["inputs"], source_tag(), config_digest(repo_dir))
    if current != facts["fingerprint"]:
        raise SlateError(
            "the config or forge-tracker changed since the facts were read; run forge-tracker "
            "next again"
        )
    classified = classify(facts, config)
    slate = validate_slate(payload, classified)
    slate.update({"schema": SCHEMA, "fingerprint": facts["fingerprint"], "saved_at": iso(now)})
    state.write_json(os.path.join(directory, "slate.json"), slate)
    return slate


def slate_status(slate: Any, facts: Mapping[str, Any], rerank: bool) -> tuple[str, Optional[str]]:
    """(`current` | `needed` | `unavailable`, reason)."""
    if not facts.get("ok"):
        return "unavailable", "a read FAILED; a slate is ranked and shown only over complete facts"
    valid = isinstance(slate, dict) and slate.get("schema") == SCHEMA
    if rerank:
        return "needed", "--rerank"
    if not valid:
        return "needed", "no slate is saved for this repo"
    if slate.get("fingerprint") != facts.get("fingerprint"):
        saved = slate.get("saved_at") or "?"
        return "needed", f"the facts moved since the slate from {saved}"
    return "current", None


# --- the report ---------------------------------------------------------------------------------


def build(
    facts: Mapping[str, Any],
    origin: str,
    config: Config,
    slate: Any,
    rerank: bool,
    pickups: Optional[Mapping[str, int]] = None,
    bodies: bool = False,
    facts_only: bool = False,
) -> dict[str, Any]:
    classified = classify(facts, config, pickups)
    parts = sections(classified)
    if facts_only:
        status, reason = "skipped", "next facts shows no slate"
    else:
        status, reason = slate_status(slate, facts, rerank)
    if not bodies:
        for item in classified["items"]:
            item.pop("body", None)
            item.pop("body_cut", None)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "project": facts["project"],
        "host": facts["host"],
        "forge": facts["forge"],
        "default_branch": facts.get("default_branch"),
        "facts": {
            "read_at": facts["read_at"],
            "origin": origin,
            "fingerprint": facts.get("fingerprint"),
        },
        "slate": {"status": status, "reason": reason},
        "notes": classified["notes"],
        "labelled": classified["labelled"],
        "blocked_label": classified["blocked_label"],
        "cycles": classified["cycles"],
        "other_branches": classified["other_branches"],
        "other_changes": classified["other_changes"],
        "failed": list(facts.get("failed") or []),
        "ok": bool(facts.get("ok")),
    }
    report.update(parts)
    if status == "current":
        report["slate"].update(
            {
                "saved_at": slate["saved_at"],
                "queue": slate["queue"],
                "lanes": slate["lanes"],
                "overrules": slate.get("overrules") or {},
                "person_only": slate.get("person_only") or {},
                "note": slate.get("note"),
                "recommend": slate.get("recommend"),
            }
        )
    return report


# --- rendering ----------------------------------------------------------------------------------


def _when(value: Optional[str], tz: Optional[tzinfo]) -> str:
    when = parse_time(value)
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M") if when else "?"


def _age(value: Optional[str], now: datetime) -> str:
    when = parse_time(value)
    if when is None:
        return "?"
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _details(item: Mapping[str, Any], untagged: bool = False) -> str:
    parts = []
    if item["tier"]:
        parts.append(item["tier"])
    if item["due"]:
        parts.append(f"due {item['due']}")
    parts += item["person_only"]["labels"]
    parts += [f"marker: {m}" for m in item["person_only"]["markers"]]
    if item["agent_label"]:
        parts.append("agent label")
    if untagged and item.get("untagged"):
        parts.append("untagged")
    if item["owners"]:
        parts.append(f"owner: {', '.join(item['owners'])}")
    return f" ({', '.join(parts)})" if parts else ""


def _change_state(c: Mapping[str, Any]) -> str:
    """A change's draft, conflict, CI and review state, as ` (draft, conflicts: no, ...)`."""
    bits = []
    if c.get("draft"):
        bits.append("draft")
    if "conflicts" in c:
        bits.append(f"conflicts: {c['conflicts']}")
    if c.get("pipeline"):
        bits.append(f"ci: {c['pipeline']}")
    if c.get("review"):
        bits.append(f"review: {c['review'].replace('_', ' ')}")
    return f" ({', '.join(bits)})" if bits else ""


def _flight(item: Mapping[str, Any]) -> str:
    out = []
    for f in item["in_flight"]:
        if f["kind"] == "change":
            out.append(f["ref"] + _change_state(f))
        else:
            ahead = f" ({f['ahead_by']} ahead)" if f.get("ahead_by") is not None else ""
            out.append(f"branch {f['ref']}{ahead}")
    return f"  in flight: {', '.join(out)}" if out else ""


def _unblocks(item: Mapping[str, Any]) -> str:
    refs = item["unblocks"]
    if refs is None:
        return "  unblocks: unknown (blocking links FAILED)"
    if not refs:
        return ""
    cycle = ", in a cycle" if item["cycle"] else ""
    return f"  unblocks {len(refs)}{cycle}: {', '.join(refs)}"


def _waits(item: Mapping[str, Any]) -> str:
    waits = [f"{b['ref']} ({b['kind']})" for b in item["blocked_by"]]
    if item["blocked_label"] and not item["blocked_by"]:
        waits.append("nothing linked (blocked label only)")
    return ", ".join(waits)


def render(report: Mapping[str, Any], now: datetime, tz: Optional[tzinfo] = None) -> str:
    slate = report["slate"]
    head = f"{report['project']} on {report['host']}"
    origin = (
        "from cache, fingerprint unchanged" if report["facts"]["origin"] == "cache" else "read now"
    )
    if slate["status"] == "current":
        lines = [
            (
                f"{head}, slate from {_when(slate['saved_at'], tz)} "
                f"({_age(slate['saved_at'], now)} old; facts unchanged since)"
            ),
            "",
        ]
    else:
        lines = [f"{head}, facts from {_when(report['facts']['read_at'], tz)} ({origin})", ""]

    items = {i["ref"]: i for key in SECTIONS for i in report.get(key, [])}
    current = slate["status"] == "current"
    overrules = slate.get("overrules") or {}
    judged = slate.get("person_only") or {}
    unclassified = report.get("unclassified", [])
    if unclassified:
        count = len(unclassified)
        issues = f"{count} open issue{'s' if count != 1 else ''}"
        lines.insert(
            1,
            "Unlabelled: no needs-human or decision label is configured, so "
            + (
                f"the queue and lanes place {issues} by Claude's judgement from their text, "
                "not by a label"
                if current
                else f"{issues} {'are' if count != 1 else 'is'} unclassified until a slate "
                "places them"
            ),
        )
    failed = {f["part"]: f["reason"] for f in report["failed"]}
    names = {
        "issues": "open issues",
        "links": "blocking links",
        "changes": "open changes",
        "branches": "branches",
        "closing": "closing references",
    }

    def failure(*parts: str) -> list[str]:
        return [f"  {names[p]}: FAILED ({failed[p]})" for p in parts if p in failed]

    issues_failed = failure("issues")

    # Your queue.
    lines.append(
        "Your queue" if current else "Your queue (no slate: most agent-ready items unblocked first)"
    )
    queue = list(report["queue"])
    if current:
        ranked = [items[r] for r in slate["queue"] if r in items]
        rest = [i for i in queue if i["ref"] not in slate["queue"] and i["ref"] not in overrules]
        queue = ranked + rest
    for n, item in enumerate(queue, 1):
        unranked = "  (not ranked)" if current and item["ref"] not in slate["queue"] else ""
        why = f"  judged person-only: {judged[item['ref']]}" if item["ref"] in judged else ""
        lines.append(
            f"  {n}. {item['ref']} {item['title']}{_details(item)}{_unblocks(item)}{why}{unranked}"
        )
    if issues_failed:
        lines += issues_failed
    elif not queue and unclassified and not current:
        lines.append(
            "  nothing marked person-only: no label is configured to mark any, so the "
            "unclassified issues below may still need you"
        )
    elif not queue:
        unlabelled = not report.get("labelled", True) and any(
            n.startswith(SETUP_PREFIX) for n in report["notes"]
        )
        lines.append(
            "  nothing person-only"
            + (" (but labels aren't configured to mark any; see setup)" if unlabelled else "")
        )

    # Agent lanes, or the agent-ready list when there's no slate.
    if current:
        lines.append("Agent lanes (grouped by judgement: these shouldn't share files)")
        laned: set[str] = set()
        for n, lane in enumerate(slate["lanes"]):
            letter = lane_label(n)
            members = [items[r] for r in lane["items"] if r in items]
            laned.update(lane["items"])
            flying = any(m["in_flight"] for m in members)
            why = f": {lane['why']}" if lane.get("why") else ""
            lines.append(f"  {letter}  {lane['area']}{why}{'  (in flight)' if flying else ''}")
            for m in members:
                over = f"  overruled: {overrules[m['ref']]}" if m["ref"] in overrules else ""
                lines.append(f"     {m['ref']} {m['title']}{_details(m)}{_flight(m)}{over}")
        rest = [
            i["ref"]
            for i in report["agent_ready"]
            if i["ref"] not in laned and i["ref"] not in judged
        ]
        if rest:
            lines.append(f"  not in a lane: {', '.join(rest)}")
        unplaced = [
            i["ref"] for i in unclassified if i["ref"] not in laned and i["ref"] not in judged
        ]
        if unplaced:
            lines.append(
                "  not placed (unclassified, in neither the queue nor a lane): "
                + ", ".join(unplaced)
            )
        if not slate["lanes"] and not rest and not unplaced:
            lines.append("  nothing agent-ready")
    else:
        lines.append("Agent-ready (no slate: by tier, agent label first, then due date)")
        for item in report["agent_ready"]:
            lines.append(f"  {item['ref']} {item['title']}{_details(item, True)}{_flight(item)}")
        untagged = sum(1 for i in report["agent_ready"] if i.get("untagged"))
        if untagged:
            lines.append(
                f"  ({untagged} untagged: no label marks {'them' if untagged != 1 else 'it'} "
                "either way, so agent-ready is the default, not a judgement; a slate reads "
                "their text)"
            )
        if not report["agent_ready"] and unclassified:
            lines.append(
                "  none: without a needs-human or decision label nothing can be called "
                "agent-ready (see Not classified)"
            )
        elif not report["agent_ready"] and not issues_failed and "links" not in failed:
            lines.append("  nothing agent-ready")
    if issues_failed:
        lines += issues_failed
    if unclassified and not current:
        lines.append(
            "Not classified (no needs-human or decision label is configured; a slate places these)"
        )
        for item in unclassified:
            lines.append(f"  {item['ref']} {item['title']}{_details(item)}{_flight(item)}")
    if report["unknown"]:
        lines.append(
            "  not classified (blocking links FAILED, so these can't be called agent-ready): "
            + ", ".join(i["ref"] for i in report["unknown"])
        )
    flight_failed = failure("changes", "branches")
    if flight_failed and not issues_failed:
        lines += flight_failed
        lines.append("  (so an item may be in flight without being marked)")

    # Blocked.
    lines.append("Blocked")
    for item in report["blocked"]:
        po = item["person_only"]
        person = " (person-only)" if po["labels"] or po["markers"] else ""
        lines.append(f"  {item['ref']} {item['title']}{person} waits on {_waits(item)}")
    if issues_failed:
        lines += issues_failed
    else:
        lines += failure("links")
        if not report["blocked"] and "links" not in failed:
            # Only what the forge records: an issue whose text says it waits isn't counted.
            lines.append(
                "  no blocking links or blocked labels recorded"
                if report.get("blocked_label")
                else "  no blocking links recorded (no blocked label is configured)"
            )

    # Open changes no open issue has in flight: still work in flight, so they get their own list.
    if report["other_changes"]:
        lines.append("Open PRs with no open issue")
        for c in report["other_changes"]:
            refs_named = f", names {', '.join(c['names'])} (not open)" if c["names"] else ""
            lines.append(
                f"  {c['ref']} {c['title']}{_change_state(c)}  branch {c['branch']}{refs_named}"
            )
        if "closing" in failed:
            lines.append(
                "  (closing references FAILED, so a PR here may still close an open issue)"
            )

    # Flags.
    flags = []
    for key in SECTIONS:
        for item in report.get(key, []):
            for m in item["merged_by"] or []:
                base = (
                    f" into {m['base']}"
                    if m["base"] and m["base"] != report.get("default_branch")
                    else ""
                )
                flags.append(
                    f"  {item['ref']} is closed by merged {m['ref']}{base} but still open (run /groom)"
                )
            for s in item["stale_links"]:
                flags.append(
                    f"  {item['ref']} has a link to closed blocker {s['ref']} (run /groom)"
                )
            for text in item["flags"]:
                flags.append(f"  {item['ref']} {text} (run /groom)")
    for cycle in report["cycles"]:
        flags.append(f"  blocking cycle: {' -> '.join(cycle + cycle[:1])}")
    for b in report["other_branches"]:
        ahead = f"{b['ahead_by']} ahead" if b["ahead_by"] is not None else "ahead unknown"
        flags.append(f"  branch {b['name']} has no PR and names no open issue ({ahead})")
    if not issues_failed:
        flags += failure("closing")
        if "closing" in failed:
            flags.append(
                "  (so merged-but-open issues aren't flagged, and closing references don't mark "
                "anything in flight)"
            )
    if flags:
        lines.append("Flags")
        lines += sorted(set(flags), key=flags.index)

    for f in report["failed"]:
        if f["part"] not in names:
            lines.append(f"{f['part']}: FAILED ({f['reason']})")
    for note in report["notes"]:
        lines.append(f"({note})")

    if slate["status"] == "needed":
        lines += [
            "",
            (
                f"slate: needed ({slate['reason']}). Rank from `forge-tracker next facts --json` "
                "and save with `forge-tracker next save-slate`."
            ),
        ]
    elif slate["status"] == "unavailable":
        lines += ["", f"slate: unavailable ({slate['reason']})"]
    elif slate.get("note"):
        lines += ["", f"slate note: {slate['note']}"]
    if slate["status"] == "current" and slate.get("recommend"):
        lines += ["", *_recommended(slate["recommend"])]
    return "\n".join(lines) + "\n"


def _recommended(rec: Mapping[str, Any]) -> list[str]:
    """The slate's closing recommendation. It's a judgement, and /next never acts on it."""
    lines = [
        (
            "Recommended next (Claude's judgement from the facts; nothing starts or merges "
            "until you say so)"
        )
    ]
    if rec.get("merge"):
        lines.append("  Merge first (CI green, no conflicts, review approved or not required)")
        lines += [f"    {m['ref']} {m['title']}: {m['why']}" for m in rec["merge"]]
    if rec.get("you"):
        lines.append("  For you")
        lines += [
            f"    {n}. {p['ref']} {p['title']}: {p['why']}" for n, p in enumerate(rec["you"], 1)
        ]
    if rec.get("start"):
        count = len(rec["start"])
        cap = f", agent cap {rec['agent_cap']}" if rec.get("agent_cap") is not None else ""
        lines.append(f"  Start now ({count} lane{'s' if count != 1 else ''}{cap})")
        lines += [f"    {lane['lane']}  {lane['area']}: {lane['why']}" for lane in rec["start"]]
    if rec.get("wait"):
        lines.append("  Waits")
        lines += [f"    {lane['lane']}  {lane['area']}: {lane['why']}" for lane in rec["wait"]]
    if rec.get("not_yet"):
        lines.append("  Not yet")
        lines += [
            f"    {'PR ' if n['kind'] == 'pr' else ''}{n['ref']} {n['title']}: {n['why']}"
            for n in rec["not_yet"]
        ]
    if len(lines) == 1:
        lines.append("  nothing picked")
    return lines
