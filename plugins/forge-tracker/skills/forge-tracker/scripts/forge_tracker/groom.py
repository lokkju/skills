"""/groom: find where the tracker has drifted from the truth, and propose the fixes.

Read-only. Every check reads what it needs, fails on its own, and sorts what it finds four ways:

- **Proposals:** changes the CLI verified, each a named write (`close`, `labels`, `milestone`,
  `unlink`, `delete-branch`, `link-add`) with its payload and evidence. Nothing is written here;
  `groom apply` writes the ones the user approved, after checking each is still a current
  proposal.
- **Unclear:** what looks like drift but couldn't be verified. Never acted on: a change with
  `conflicts: unknown`, a merged change that names an open issue only in its title or branch (no
  closing reference), a branch with commits the default branch doesn't have, a tier label and a
  milestone that disagree, every likely duplicate, and prose blocking that can't be linked from
  here (a pull request, another project's issue, a blocker that couldn't be read).
- **Listed for you:** conflicted or stale PRs, placeholder due dates, and prose blocking on a
  blocker that has closed, for the user to judge.
- **Possibly done:** open issues a merged change looks to have finished without a closing
  reference (the forge's cross-reference, or a path or symbol the body names that the change
  touched), listed for the user and never proposed.

The report also names the checks that ran, so a sparse report says how much was checked.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional

from . import refs
from .config import Config
from .detect import Remote
from .model import Issue, Read, attempt, iso

SCHEMA = 1
# How far back merged PRs are read for the merged-but-open and named-only checks.
MERGED_DAYS = 90
# Closed issues this recent are compared with open ones for duplicates.
RECENT_CLOSED_DAYS = 30
# A PR or a branch with no activity for this long is stale, when `stale_days` isn't set.
DEFAULT_STALE_DAYS = 14
# Token-set similarity at or above this makes two issues likely duplicates.
DUPLICATE_THRESHOLD = 0.6
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Branches release and dependency bots create and reuse. The stale-branch check never looks at
# them; `groom.keep_branches` in the config adds more globs to this list.
KEEP_BRANCHES = (
    "yeet/*",
    "release-please--*",
    "changeset-release/*",
    "dependabot/*",
    "renovate/*",
)

CHECKS = {
    "merged": "merged but still open",
    "named": "merged PRs that only name an open issue",
    "possibly_done": "possibly done without a closing reference",
    "tiers": "tier labels and milestones",
    "blocked_label": "blocked label with nothing linked",
    "closed_blockers": "links to closed blockers",
    "prose_blocks": "blocking written only as prose",
    "decided": "decided but still waiting",
    "branches": "stale branches",
    "changes": "conflicted or stale PRs",
    "duplicates": "likely duplicates",
    "due_dates": "placeholder due dates",
}


# --- gathering ----------------------------------------------------------------------------------


def _waiting_labels(config: Config) -> list[str]:
    return [label for label in (config.label("needs_human"), config.label("decision")) if label]


def gather(adapter: Any, config: Config, now: datetime, workers: int = 6) -> dict[str, Any]:
    """Every read the checks need, each failing on its own. Branches need the open changes and
    the default branch; per-issue reads (label history and comments for the decided check, and
    whether a merge commit is on the default branch) need the lists they hang off."""
    reads: dict[str, Any] = {}
    repo = attempt(adapter.repo)
    reads["repo"] = repo
    default = repo.data.default_branch if repo.ok else None
    jobs: dict[str, Callable[[], Any]] = {
        "issues": adapter.open_issues,
        "links": adapter.blocking_links,
        "changes": adapter.open_changes,
        "closing": adapter.closing_refs,
        "merged": lambda: adapter.merged_since(now - timedelta(days=MERGED_DAYS)),
        "recent": lambda: adapter.issues_since(now - timedelta(days=RECENT_CLOSED_DAYS)),
        "milestones": adapter.milestones,
        "cross_refs": adapter.cross_refs,
    }
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(attempt, fn) for name, fn in jobs.items()}
        for name, future in futures.items():
            reads[name] = future.result()

    _gather_possibly_done(adapter, reads, workers)

    changes = reads["changes"]
    if not changes.ok:
        reads["branches"] = Read(error="needs the open changes, which failed")
    elif default is None:
        reads["branches"] = Read(error="needs the default branch, which failed")
    else:
        reads["branches"] = attempt(lambda: adapter.branches(default, changes.data))

    # Is each merged closing reference's merge commit on the default branch?
    reachable: dict[tuple[int, int], Read] = {}
    if reads["closing"].ok and default is not None:
        for ref in reads["closing"].data:
            if (
                ref.state == "merged"
                and ref.change_project is None
                and ref.base == default
                and ref.merge_sha
            ):
                sha = ref.merge_sha
                reachable[(ref.issue, ref.change)] = attempt(
                    lambda sha=sha: adapter.on_branch(default, sha)
                )
    reads["reachable"] = reachable

    # Label history and comments for issues still carrying a waiting label.
    history: dict[int, Read] = {}
    waiting = _waiting_labels(config)
    if reads["issues"].ok and waiting and config.ruling_pattern is not None:
        numbers = [i.number for i in reads["issues"].data if set(i.labels) & set(waiting)]

        def one(number: int) -> tuple[Any, Any]:
            return adapter.label_events(number), adapter.issue_comments(number)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures2 = {n: pool.submit(attempt, lambda n=n: one(n)) for n in numbers}
            for number, future in futures2.items():
                history[number] = future.result()
    reads["history"] = history

    # What each blocker an issue body names in prose is, when it isn't an open issue here (or a
    # recently closed one, which the recent read already has).
    blockers: dict[tuple[Optional[str], int], Read] = {}
    if reads["issues"].ok:
        known = {i.number for i in reads["issues"].data}
        if reads["recent"].ok:
            known |= {i.number for i in reads["recent"].data}
        project = getattr(adapter, "project", None)
        wanted = set()
        for issue in reads["issues"].data:
            for direction, other, number, _line in refs.prose_blocks(issue.body):
                other = None if other == project else other
                if direction == "blocked_by" and (other is not None or number not in known):
                    wanted.add((other, number))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures3 = {
                key: pool.submit(attempt, lambda key=key: adapter.issue_ref(key[1], key[0]))
                for key in sorted(wanted, key=lambda k: (k[0] or "", k[1]))
            }
            for key, future in futures3.items():
                blockers[key] = future.result()
    reads["prose_blockers"] = blockers
    return reads


def _gather_possibly_done(adapter: Any, reads: dict[str, Any], workers: int) -> None:
    """The reads the possibly-done check needs beyond the lists: the files each merged change
    touched (only the changes merged since the oldest open issue whose body names a path or a
    symbol was opened, and none when no body names one), then the comments on each issue the
    check will list, to say whether anything happened after the merge."""
    reads["patches"] = bool(getattr(adapter, "file_patches", True))
    files: dict[int, Read] = {}
    if reads["issues"].ok and reads["merged"].ok:
        named = [i for i in reads["issues"].data if any(refs.body_names(i.body))]
        if named:
            since = min(i.created for i in named)
            numbers = sorted(
                c.number for c in reads["merged"].data if c.merged_at and c.merged_at >= since
            )
            if numbers:
                first = attempt(lambda: adapter.change_files(numbers[0]))
                files[numbers[0]] = first
                if first.unsupported:
                    # The forge has no such read; asking once per change would say so again.
                    files.update({n: first for n in numbers[1:]})
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {
                            n: pool.submit(attempt, lambda n=n: adapter.change_files(n))
                            for n in numbers[1:]
                        }
                        for n, future in futures.items():
                            files[n] = future.result()
    reads["files"] = files

    comments: dict[int, Read] = {}
    candidates = sorted(_done_matches(reads))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures4 = {
            n: pool.submit(attempt, lambda n=n: adapter.issue_comments(n)) for n in candidates
        }
        for n, future in futures4.items():
            comments[n] = future.result()
    reads["done_comments"] = comments


# --- the checks ---------------------------------------------------------------------------------


class Findings:
    """What the checks found, plus the checks that failed or were skipped."""

    def __init__(self) -> None:
        self.proposals: list[dict[str, Any]] = []
        self.unclear: list[dict[str, Any]] = []
        self.listed: list[dict[str, Any]] = []
        self.done: list[dict[str, Any]] = []
        self.failed: list[dict[str, str]] = []
        self.skipped: list[dict[str, str]] = []
        self.unsupported: list[dict[str, str]] = []
        # The checks that ran: every read they need succeeded, whatever they found. A check
        # whose items partly failed still ran; its failures are in `failed` as well.
        self.ran: set[str] = set()

    def propose(self, check: str, id_: str, op: str, payload: dict[str, Any], evidence: str):
        if any(p["id"] == id_ for p in self.proposals):
            return
        self.proposals.append(
            {"id": id_, "check": check, "op": op, "payload": payload, "evidence": evidence}
        )

    def unsure(self, check: str, ref: str, evidence: str) -> None:
        self.unclear.append({"check": check, "ref": ref, "evidence": evidence})

    def list(self, check: str, ref: str, evidence: str) -> None:
        self.listed.append({"check": check, "ref": ref, "evidence": evidence})

    def fail(self, check: str, reason: str) -> None:
        self.failed.append({"part": CHECKS.get(check, check), "reason": reason})

    def skip(self, check: str, reason: str) -> None:
        self.skipped.append({"part": CHECKS[check], "reason": reason})

    def cannot(self, check: str, reason: str) -> None:
        """Part of a check the forge has no read for: shown as unsupported, not FAILED."""
        item = {"part": CHECKS[check], "reason": reason}
        if item not in self.unsupported:
            self.unsupported.append(item)


_READ_NAMES = {
    "repo": "repo",
    "issues": "open issues",
    "links": "blocking links",
    "changes": "open changes",
    "closing": "closing references",
    "merged": "merged PRs",
    "recent": "recent issues",
    "milestones": "milestones",
    "branches": "branches",
    "cross_refs": "cross-references",
}


FORGE_NAMES = {"github": "GitHub", "gitlab": "GitLab", "forgejo": "Forgejo"}


def _needs(f: Findings, check: str, reads: Mapping[str, Any], *names: str) -> bool:
    """True when every read the check needs succeeded; otherwise the check FAILED with the
    first failed read named."""
    for name in names:
        read = reads[name]
        if not read.ok:
            f.fail(check, f"{_READ_NAMES[name]}: {read.error}")
            return False
    f.ran.add(check)
    return True


def _short(sha: Optional[str]) -> str:
    return (sha or "?")[:7]


def _day(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d") if value else "?"


def check_merged(f: Findings, reads: Mapping[str, Any], forge: str = "github") -> None:
    """Merged but still open: a merged change with a real closing reference (D22), merged into
    the default branch, its merge commit verified on that branch."""
    if not _needs(f, "merged", reads, "repo", "closing"):
        return
    default = reads["repo"].data.default_branch
    by_issue: dict[int, list[Any]] = {}
    for ref in reads["closing"].data:
        if ref.state == "merged":
            by_issue.setdefault(ref.issue, []).append(ref)
    for number in sorted(by_issue):
        verified = None
        for ref in sorted(by_issue[number], key=lambda r: (r.merged_at or _EPOCH, r.change)):
            cref = refs.change_ref(forge, ref.change)
            change = f"{ref.change_project}{cref}" if ref.change_project else cref
            if ref.change_project:
                f.unsure(
                    "merged",
                    f"#{number}",
                    f"#{number} has a closing reference from merged {change} in another "
                    "project; not verified",
                )
                continue
            if ref.base != default:
                f.unsure(
                    "merged",
                    f"#{number}",
                    f"#{number} has a closing reference from {change}, which merged into "
                    f"{ref.base or '?'}, not {default}, so the forge didn't close it",
                )
                continue
            if not ref.merge_sha:
                f.unsure("merged", f"#{number}", f"merged {change} has no merge commit to check")
                continue
            check = reads["reachable"].get((ref.issue, ref.change))
            if check is None or not check.ok:
                reason = check.error if check is not None else "not read"
                f.unsure(
                    "merged",
                    f"#{number}",
                    f"#{number} has a closing reference from merged {change}, but whether "
                    f"{_short(ref.merge_sha)} is on {default} FAILED ({reason})",
                )
                f.fail("merged", f"#{number}: {reason}")
                continue
            if not check.data:
                f.unsure(
                    "merged",
                    f"#{number}",
                    f"#{number} has a closing reference from merged {change}, but "
                    f"{_short(ref.merge_sha)} isn't on {default}",
                )
                continue
            verified = verified or ref
        if verified is not None:
            title = f' "{verified.title}"' if verified.title else ""
            vref = refs.change_ref(forge, verified.change)
            f.propose(
                "merged",
                f"close #{number}",
                "close",
                {
                    "issue": number,
                    "change": verified.change,
                    "comment": (
                        f"Closing: merged {vref}{title} has a closing reference to "
                        f"this issue, and its merge commit {_short(verified.merge_sha)} is on "
                        f"{default}."
                    ),
                },
                f"merged {vref}{title} closes it, {_short(verified.merge_sha)} is on {default}",
            )


def check_named(
    f: Findings, reads: Mapping[str, Any], project: Optional[str] = None, forge: str = "github"
) -> None:
    """A merged change that names an open issue only in its title or branch, with no closing
    reference to it, goes to the unclear bucket (D22), never to proposals."""
    if not _needs(f, "named", reads, "merged", "issues", "closing"):
        return
    open_numbers = {i.number for i in reads["issues"].data}
    closing = {(r.issue, r.change) for r in reads["closing"].data if r.change_project is None}
    for change in sorted(reads["merged"].data, key=lambda c: c.number):
        via: dict[int, list[str]] = {}
        for n in refs.title_numbers(change.title, project):
            via.setdefault(n, []).append("title")
        for n in refs.branch_numbers(change.branch):
            via.setdefault(n, []).append(f"branch {change.branch}")
        for number in sorted(via):
            if number == change.number or number not in open_numbers:
                continue
            if number in change.closing_issues or (number, change.number) in closing:
                continue
            where = " and ".join(via[number])
            f.unsure(
                "named",
                f"#{number}",
                f"#{number} named in the {where} of merged "
                f"{refs.change_ref(forge, change.number)}, no closing reference",
            )


def _done_matches(reads: Mapping[str, Any]) -> dict[int, dict[int, dict[str, Any]]]:
    """Open issue -> merged change -> what ties them, before activity is weighed:
    `cross` (the forge records the change mentioning the issue) and the `paths` and `symbols`
    the issue's body names that the change touched (a change merged after the issue was opened).
    A change with a closing reference to the issue is left out: the merged-but-open and
    named-only checks own that pair. So is a cross-reference from another project or from a
    change merged into a branch other than the default."""
    if not reads["issues"].ok:
        return {}
    issues = {i.number: i for i in reads["issues"].data}
    default = reads["repo"].data.default_branch if reads["repo"].ok else None
    closing: set[tuple[int, int]] = set()
    if reads["closing"].ok:
        closing = {(r.issue, r.change) for r in reads["closing"].data if r.change_project is None}
    out: dict[int, dict[int, dict[str, Any]]] = {}

    def entry(issue: int, change: int, merged_at: Any, title: str) -> dict[str, Any]:
        return out.setdefault(issue, {}).setdefault(
            change,
            {"cross": False, "paths": [], "symbols": [], "merged_at": merged_at, "title": title},
        )

    cross = reads.get("cross_refs")
    if cross is not None and cross.ok:
        for ref in cross.data:
            if ref.issue not in issues or ref.state != "merged" or ref.change_project:
                continue
            if ref.closes or (ref.issue, ref.change) in closing:
                continue
            if ref.base and default and ref.base != default:
                continue
            entry(ref.issue, ref.change, ref.merged_at, ref.title)["cross"] = True
    if reads["merged"].ok:
        for issue in issues.values():
            paths, symbols = refs.body_names(issue.body)
            if not paths and not symbols:
                continue
            for change in reads["merged"].data:
                if change.merged_at is None or change.merged_at < issue.created:
                    continue
                if (
                    issue.number in change.closing_issues
                    or (issue.number, change.number) in closing
                ):
                    continue
                read = reads["files"].get(change.number)
                if read is None or not read.ok:
                    continue
                touched = [f.path for f in read.data] + [
                    f.previous_path for f in read.data if f.previous_path
                ]
                hit_paths = [p for p in paths if any(refs.path_matches(p, t) for t in touched)]
                hit_symbols = [
                    s
                    for s in symbols
                    if any(f.patch and refs.patch_names(f.patch, s) for f in read.data)
                ]
                if hit_paths or hit_symbols:
                    e = entry(issue.number, change.number, change.merged_at, change.title)
                    e["paths"], e["symbols"] = hit_paths, hit_symbols
    return out


def _listing(items: list[str], most: int = 4) -> str:
    """`a`, `a and b`, `a, b and c`, or the first `most` and how many more."""
    if len(items) > most:
        return f"{', '.join(items[:most])} and {len(items) - most} more"
    if len(items) > 1:
        return f"{', '.join(items[:-1])} and {items[-1]}"
    return items[0] if items else ""


def check_possibly_done(f: Findings, reads: Mapping[str, Any], forge: str = "github") -> None:
    """Open issues a merged change looks to have finished without a closing reference, listed
    for the user and never proposed. Two signals: (a) the forge's own record that a merged
    change mentions the issue in any form, and (b) the issue's body naming a path or a symbol
    that a change merged since the issue was opened touched, with no comment on the issue since
    that merge. Titles, labels and milestones are never evidence: near-duplicate titles and
    stale milestones make confident wrong closes."""
    check = "possibly_done"
    if not _needs(f, check, reads, "issues"):
        return
    cross, merged = reads["cross_refs"], reads["merged"]
    if not cross.ok:
        if cross.unsupported:
            f.cannot(check, f"cross-references: {cross.error}")
        else:
            f.fail(check, f"cross-references: {cross.error}")
    if not merged.ok:
        f.fail(check, f"merged {refs.change_word(forge)}s: {merged.error}")
    if not cross.ok and not merged.ok:
        f.ran.discard(check)
        return
    for number, read in sorted(reads["files"].items()):
        if read.ok:
            continue
        if read.unsupported:
            f.cannot(check, f"changed files: {read.error}")
        else:
            f.fail(check, f"files of {refs.change_ref(forge, number)}: {read.error}")
    if not reads["patches"] and any(
        refs.body_names(i.body)[1] for i in reads["issues"].data if i.body
    ):
        f.cannot(
            check,
            f"symbols: {FORGE_NAMES.get(forge, forge)}'s changed-file list has no diffs, so only "
            "the paths a body names are matched",
        )
    titles = {i.number: i.title for i in reads["issues"].data}
    for number, changes in sorted(_done_matches(reads).items()):
        comments = reads["done_comments"].get(number)
        times = None
        if comments is None or not comments.ok:
            reason = comments.error if comments is not None else "not read"
            f.fail(check, f"comments on #{number}: {reason}")
        else:
            times = sorted(c.created for c in comments.data)
        crossed = sorted(c for c, e in changes.items() if e["cross"])
        named = []
        for change, e in sorted(changes.items()):
            if not (e["paths"] or e["symbols"]):
                continue
            if times is not None and any(t > e["merged_at"] for t in times):
                continue  # a comment since that merge: someone is still working on it
            named.append(change)
        if not crossed and not named:
            continue

        dated = {
            c: f"{refs.change_ref(forge, c)} ({_day(e['merged_at'])})" for c, e in changes.items()
        }
        parts = []
        if crossed:
            verb = "cross-references" if len(crossed) == 1 else "cross-reference"
            parts.append(f"merged {_listing([dated[c] for c in crossed])} {verb} it")
        if named:
            names: list[str] = []
            for change in named:
                for name in changes[change]["paths"] + changes[change]["symbols"]:
                    if name not in names:
                        names.append(name)
            # Every change here merged after the issue's last comment, so the closing
            # "last comment" (or "no comments") says there was no activity since.
            parts.append(
                f"merged {_listing([dated[c] for c in named])} changed {_listing(names)}, "
                "which its body names"
            )
        if times is None:
            parts.append("whether it has comments since FAILED")
        else:
            parts.append(f"last comment {_day(times[-1])}" if times else "no comments")
        f.done.append(
            {
                "check": check,
                "ref": f"#{number}",
                "issue": number,
                "cross_referenced_by": crossed,
                "names_changed_by": [
                    {
                        "change": c,
                        "paths": changes[c]["paths"],
                        "symbols": changes[c]["symbols"],
                    }
                    for c in named
                ],
                "last_comment": iso(times[-1]) if times else None,
                "evidence": f"#{number} {titles.get(number, '')!r}: " + "; ".join(parts),
            }
        )


def _tier_of(title: Optional[str], prefixes: tuple[str, ...]) -> Optional[int]:
    if not title:
        return None
    for index, prefix in enumerate(prefixes):
        if title.startswith(prefix):
            return index
    return None


def check_tiers(f: Findings, reads: Mapping[str, Any], config: Config) -> None:
    """A tier label without its milestone, or a tier milestone without its label. Tier label N
    pairs with milestone prefix N, in the order the config lists them."""
    labels, prefixes = config.tier_labels, config.milestone_prefixes
    if not labels or not prefixes:
        f.skip("tiers", "needs both tiers.labels and tiers.milestone_prefixes")
        return
    if not _needs(f, "tiers", reads, "issues", "milestones"):
        return
    pairs = min(len(labels), len(prefixes))
    labels, prefixes = labels[:pairs], prefixes[:pairs]
    milestones = reads["milestones"].data
    for issue in sorted(reads["issues"].data, key=lambda i: i.number):
        held = [n for n, label in enumerate(labels) if label in issue.labels]
        title = issue.milestone.title if issue.milestone else None
        tier = _tier_of(title, prefixes)
        ref = f"#{issue.number}"
        if len(held) > 1:
            names = ", ".join(labels[n] for n in held)
            f.unsure("tiers", ref, f"{ref} has more than one tier label ({names})")
        elif held and tier is None:
            want = held[0]
            if title:
                f.unsure(
                    "tiers",
                    ref,
                    f"{ref} has {labels[want]} but milestone {title!r}, not a "
                    f"{prefixes[want]!r} milestone",
                )
                continue
            matches = [m for m in milestones if m.title.startswith(prefixes[want])]
            if len(matches) == 1:
                m = matches[0]
                f.propose(
                    "tiers",
                    f"milestone {ref}",
                    "milestone",
                    {"issue": issue.number, "milestone": {"number": m.number, "title": m.title}},
                    f"has {labels[want]} and no milestone; {m.title!r} is the only open "
                    f"{prefixes[want]!r} milestone",
                )
            else:
                f.unsure(
                    "tiers",
                    ref,
                    f"{ref} has {labels[want]} and no milestone, and {len(matches)} open "
                    f"milestones start with {prefixes[want]!r}",
                )
        elif held and tier is not None and held[0] != tier:
            f.unsure(
                "tiers",
                ref,
                f"{ref} has {labels[held[0]]} but milestone {title!r} ({prefixes[tier]}); "
                "which one is right isn't something the tracker says",
            )
        elif not held and tier is not None:
            f.propose(
                "tiers",
                f"labels {ref} tier",
                "labels",
                {"issue": issue.number, "add": [labels[tier]]},
                f"in milestone {title!r} without the {labels[tier]} label",
            )


def _blocker_ref(link: Any) -> str:
    return f"{link.blocker_project}#{link.blocker}" if link.blocker_project else f"#{link.blocker}"


def check_blocked_label(f: Findings, reads: Mapping[str, Any], config: Config) -> None:
    label = config.label("blocked")
    if not label:
        f.skip("blocked_label", "labels.blocked isn't set")
        return
    if not _needs(f, "blocked_label", reads, "issues", "links"):
        return
    open_blockers = {link.blocked for link in reads["links"].data if link.blocker_open}
    closed: dict[int, list[str]] = {}
    for link in reads["links"].data:
        if not link.blocker_open:
            closed.setdefault(link.blocked, []).append(_blocker_ref(link))
    for issue in sorted(reads["issues"].data, key=lambda i: i.number):
        if label not in issue.labels or issue.number in open_blockers:
            continue
        ref = f"#{issue.number}"
        extra = (
            f" (only closed ones: {', '.join(closed[issue.number])})"
            if issue.number in closed
            else ""
        )
        f.propose(
            "blocked_label",
            f"unlink {ref} label",
            "unlink",
            {"issue": issue.number, "label": label},
            f"the {label} label with no open blocker linked{extra}",
        )


def check_closed_blockers(f: Findings, reads: Mapping[str, Any]) -> None:
    if not _needs(f, "closed_blockers", reads, "links"):
        return
    for link in sorted(reads["links"].data, key=lambda l: (l.blocked, l.blocker)):
        if link.blocker_open:
            continue
        blocker = _blocker_ref(link)
        payload: dict[str, Any] = {"issue": link.blocked, "blocker": link.blocker}
        if link.blocker_project:
            payload["blocker_project"] = link.blocker_project
        f.propose(
            "closed_blockers",
            f"unlink #{link.blocked} from {blocker}",
            "unlink",
            payload,
            f"blocked by {blocker}, closed {_day(link.blocker_closed)}",
        )


def _quoted(line: str) -> str:
    return repr(line if len(line) <= 80 else line[:77] + "...")


def check_prose_blocks(f: Findings, reads: Mapping[str, Any], project: str) -> None:
    """Blocking an open issue's body states only in words (`Blocked by: #17`, `Blocks #18`),
    with no native link behind it. `/next` and the other checks read only the links, so the
    sentence blocks nothing. An open blocker is a `link-add` proposal; a closed one is listed
    for the user, since the prose is stale and only a person edits a body; anything that can't
    be linked from here is unclear. Bodies are never edited."""
    if not _needs(f, "prose_blocks", reads, "issues", "links"):
        return
    issues = {i.number: i for i in reads["issues"].data}
    recent = {i.number: i for i in reads["recent"].data} if reads["recent"].ok else {}
    native = {(link.blocked, link.blocker_project, link.blocker) for link in reads["links"].data}
    for issue in sorted(issues.values(), key=lambda i: i.number):
        for direction, other, number, line in refs.prose_blocks(issue.body):
            other = None if other == project else other
            ref = f"{other}#{number}" if other else f"#{number}"
            says = f"#{issue.number}'s body says {_quoted(line)}"
            if other is None and number == issue.number:
                continue
            if direction == "blocks":
                if other is not None:
                    f.unsure(
                        "prose_blocks",
                        f"#{issue.number}",
                        f"{says}: {ref} is in another project; link it there",
                    )
                    continue
                if number not in issues:
                    continue  # closed, a pull request, or no such issue: nothing waits on it
                blocked, blocker, blocker_project, blocker_open = number, issue.number, None, True
                closed_on = None
            else:
                blocked, blocker, blocker_project = issue.number, number, other
                state = issues.get(number) if other is None else None
                if state is None and other is None and number in recent:
                    state = recent[number]
                if state is not None:
                    blocker_open, closed_on = state.state == "open", state.closed
                else:
                    read = reads["prose_blockers"].get((other, number))
                    if read is None or not read.ok:
                        reason = read.error if read is not None else "not read"
                        f.unsure(
                            "prose_blocks", f"#{issue.number}", f"{says}: {ref} FAILED ({reason})"
                        )
                        f.fail("prose_blocks", f"{ref}: {reason}")
                        continue
                    if read.data.is_change:
                        f.unsure(
                            "prose_blocks",
                            f"#{issue.number}",
                            f"{says}: {ref} is a pull request; a blocking link joins two issues",
                        )
                        continue
                    blocker_open, closed_on = read.data.state == "open", read.data.closed
            if (blocked, blocker_project, blocker) in native:
                continue
            if not blocker_open:
                f.list(
                    "prose_blocks",
                    f"#{blocked}",
                    f"{says}, but {ref} closed {_day(closed_on)} and nothing is linked; the "
                    "prose is stale",
                )
                continue
            payload: dict[str, Any] = {"issue": blocked, "blocker": blocker}
            if blocker_project:
                payload["blocker_project"] = blocker_project
            blocker_name = f"{blocker_project}#{blocker}" if blocker_project else f"#{blocker}"
            f.propose(
                "prose_blocks",
                f"link-add #{blocked} blocked by {blocker_name}",
                "link-add",
                payload,
                f"{says}; no native link, so /next doesn't see it",
            )


def check_decided(f: Findings, reads: Mapping[str, Any], config: Config) -> None:
    """An issue still carrying needs-human or decision whose newest ruling comment is dated after
    the label was added. The ruling is trusted by its pattern, never by its author (D20)."""
    waiting = _waiting_labels(config)
    if not waiting:
        f.skip("decided", "labels.needs_human and labels.decision aren't set")
        return
    if config.ruling_pattern is None:
        f.skip("decided", "ruling_pattern isn't set")
        return
    if not _needs(f, "decided", reads, "issues"):
        return
    made = config.label("decision_made")
    for issue in sorted(reads["issues"].data, key=lambda i: i.number):
        held = [label for label in waiting if label in issue.labels]
        if not held:
            continue
        ref = f"#{issue.number}"
        read = reads["history"].get(issue.number)
        if read is None or not read.ok:
            f.fail("decided", f"{ref}: {read.error if read is not None else 'not read'}")
            continue
        events, comments = read.data
        added: dict[str, datetime] = {}
        for event in events:
            if event.event == "labeled" and event.label in held:
                added[event.label] = event.created
        if set(held) - set(added):
            missing = ", ".join(sorted(set(held) - set(added)))
            f.unsure("decided", ref, f"{ref}: no record of when {missing} was added")
            continue
        since = max(added.values())
        rulings = [
            c for c in comments if c.created > since and config.ruling_pattern.search(c.body)
        ]
        if not rulings:
            continue
        ruling = rulings[-1]
        match = config.ruling_pattern.search(ruling.body)
        line_start = ruling.body.rfind("\n", 0, match.start()) + 1 if match else 0
        line_end = ruling.body.find("\n", line_start)
        line = ruling.body[line_start : line_end if line_end >= 0 else None].strip()
        if len(line) > 80:
            line = line[:77] + "..."
        payload: dict[str, Any] = {"issue": issue.number, "remove": held}
        if made and made not in issue.labels:
            payload["add"] = [made]
        change = f"remove {', '.join(held)}" + (f", add {made}" if "add" in payload else "")
        f.propose(
            "decided",
            f"labels {ref} ruling",
            "labels",
            payload,
            f"{change}: {line!r} on {_day(ruling.created)}, after "
            f"{', '.join(held)} was added on {_day(since)}",
        )


def check_branches(
    f: Findings,
    reads: Mapping[str, Any],
    stale: timedelta,
    now: datetime,
    keep: tuple[str, ...] = KEEP_BRANCHES,
    forge: str = "github",
) -> None:
    """A branch with no open PR and every commit on the default branch is a delete proposal.
    One with commits the default branch lacks is unclear when it's idle past the stale age or is
    the branch of a merged PR (a squash or rebase merge leaves its commits off the default).
    Protected branches and those matching a `keep` glob are never looked at."""
    if not _needs(f, "branches", reads, "branches", "repo"):
        return
    default = reads["repo"].data.default_branch
    merged_from: dict[str, int] = {}
    if reads["merged"].ok:
        for change in reads["merged"].data:
            merged_from.setdefault(change.branch, change.number)
    for branch in sorted(reads["branches"].data, key=lambda b: b.name):
        if branch.has_change or branch.protected or branch.name == default:
            continue
        if any(fnmatch.fnmatchcase(branch.name, pattern) for pattern in keep):
            continue
        merged = (
            f", branch of merged {refs.change_ref(forge, merged_from[branch.name])}"
            if branch.name in merged_from
            else ""
        )
        word = refs.change_word(forge)
        if branch.ahead_by == 0:
            f.propose(
                "branches",
                f"delete-branch {branch.name}",
                "delete-branch",
                {"branch": branch.name, "sha": branch.sha},
                f"no open {word}, 0 commits ahead of {default}{merged}",
            )
        elif branch.ahead_by is None:
            f.unsure(
                "branches", branch.name, f"branch {branch.name}: commits ahead of {default} unknown"
            )
        else:
            idle = branch.head_date is not None and now - branch.head_date >= stale
            if idle or merged:
                age = (
                    f", last commit {_day(branch.head_date)}"
                    if branch.head_date is not None
                    else ""
                )
                f.unsure(
                    "branches",
                    branch.name,
                    f"branch {branch.name}: no open {word}, {branch.ahead_by} commit"
                    f"{'s' if branch.ahead_by != 1 else ''} not on {default}{age}{merged}",
                )


def check_changes(
    f: Findings, reads: Mapping[str, Any], stale: timedelta, now: datetime, forge: str = "github"
) -> None:
    if not _needs(f, "changes", reads, "changes"):
        return
    name = FORGE_NAMES.get(forge, forge)
    for change in sorted(reads["changes"].data, key=lambda c: c.number):
        ref = refs.change_ref(forge, change.number)
        draft = " (draft)" if change.draft else ""
        if change.conflicts == "unknown":
            f.unsure("changes", ref, f"{ref} conflicts: unknown ({name} hasn't computed it){draft}")
        elif change.conflicts == "yes":
            f.list("changes", ref, f"{ref} conflicts: yes{draft}: {change.title}")
        idle = now - change.updated
        if idle >= stale:
            f.list(
                "changes",
                ref,
                f"{ref} no activity for {idle.days} days{draft}: {change.title}",
            )


_STOP = {"a", "an", "and", "the", "of", "to", "in", "on", "for", "is", "it", "with", "or", "by"}


def _tokens(issue: Issue) -> set[str]:
    words = {w for w in re.findall(r"[a-z0-9]+", issue.title.lower()) if w not in _STOP}
    return words | {f"label:{label.lower()}" for label in issue.labels}


def check_duplicates(f: Findings, reads: Mapping[str, Any], now: datetime) -> None:
    """Token-set similarity over titles and labels, among open issues and issues closed in the
    last RECENT_CLOSED_DAYS days. Each pair is a candidate for the user, never a proposal."""
    if not _needs(f, "duplicates", reads, "issues", "recent"):
        return
    since = now - timedelta(days=RECENT_CLOSED_DAYS)
    pool: dict[int, Issue] = {i.number: i for i in reads["issues"].data}
    for issue in reads["recent"].data:
        if issue.state == "closed" and issue.closed and issue.closed >= since:
            pool.setdefault(issue.number, issue)
    tokens = {n: _tokens(i) for n, i in pool.items()}
    index: dict[str, set[int]] = {}
    for number, words in tokens.items():
        for word in words:
            if not word.startswith("label:"):
                index.setdefault(word, set()).add(number)
    seen: set[tuple[int, int]] = set()
    for numbers in index.values():
        for a in numbers:
            for b in numbers:
                if a < b:
                    seen.add((a, b))
    for a, b in sorted(seen):
        ia, ib = pool[a], pool[b]
        if ia.state != "open" and ib.state != "open":
            continue
        ta, tb = tokens[a], tokens[b]
        score = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
        if score < DUPLICATE_THRESHOLD:
            continue
        closed = [f"#{i.number}" for i in (ia, ib) if i.state != "open"]
        note = f" ({', '.join(closed)} closed)" if closed else ""
        f.unsure(
            "duplicates",
            f"#{a}",
            f"#{a} and #{b} may be duplicates ({score:.2f}){note}: {ia.title!r} / {ib.title!r}",
        )


def check_due_dates(f: Findings, reads: Mapping[str, Any]) -> None:
    """On GitHub a due date belongs to a milestone, so every issue in one shares its date by
    design. A date is a likely placeholder when more than one open milestone uses it."""
    if not _needs(f, "due_dates", reads, "issues"):
        return
    by_date: dict[str, dict[str, int]] = {}
    for issue in reads["issues"].data:
        if issue.milestone and issue.milestone.due:
            day = issue.milestone.due[:10]
            counts = by_date.setdefault(day, {})
            counts[issue.milestone.title] = counts.get(issue.milestone.title, 0) + 1
    for day in sorted(by_date):
        counts = by_date[day]
        if len(counts) < 2:
            continue
        names = ", ".join(repr(t) for t in sorted(counts))
        f.list(
            "due_dates",
            day,
            f"{sum(counts.values())} issues share due date {day} across {len(counts)} "
            f"milestones ({names})",
        )


# --- the report ---------------------------------------------------------------------------------


def build(
    reads: Mapping[str, Any], config: Config, remote: Remote, forge: str, now: datetime
) -> dict[str, Any]:
    f = Findings()
    stale = timedelta(days=config.stale_days or DEFAULT_STALE_DAYS)
    check_merged(f, reads, forge)
    check_named(f, reads, remote.project, forge)
    check_possibly_done(f, reads, forge)
    check_tiers(f, reads, config)
    check_blocked_label(f, reads, config)
    check_closed_blockers(f, reads)
    check_prose_blocks(f, reads, remote.project)
    check_decided(f, reads, config)
    check_branches(f, reads, stale, now, KEEP_BRANCHES + config.keep_branches, forge)
    check_changes(f, reads, stale, now, forge)
    check_duplicates(f, reads, now)
    check_due_dates(f, reads)
    for n, proposal in enumerate(f.proposals, 1):
        proposal["n"] = n
        proposal["payload"] = dict(proposal["payload"], repo=remote.project)
    notes = []
    setup = config.setup_note(("labels.needs_human", "labels.decision", "labels.blocked"))
    if setup:
        notes.append(setup)
    if forge not in ("github", "gitlab") and not config.write_wrapper:
        notes.append(
            f"writes: unsupported on {FORGE_NAMES.get(forge, forge)} (forge-tracker writes only "
            "to GitHub and GitLab so far), so `groom apply` refuses each proposal; a "
            "write_wrapper: in the config can make the approved changes instead"
        )
    if reads["repo"].ok is False:
        f.failed.insert(0, {"part": "repo", "reason": reads["repo"].error})
    return {
        "schema": SCHEMA,
        "project": remote.project,
        "host": remote.host,
        "forge": forge,
        "read_at": iso(now),
        "default_branch": reads["repo"].data.default_branch if reads["repo"].ok else None,
        "window": {
            "merged_since": iso(now - timedelta(days=MERGED_DAYS)),
            "closed_since": iso(now - timedelta(days=RECENT_CLOSED_DAYS)),
            "stale_days": stale.days,
            "stale_days_set": config.stale_days is not None,
        },
        "proposals": f.proposals,
        "unclear": f.unclear,
        "listed": f.listed,
        "possibly_done": f.done,
        "checks_run": [CHECKS[c] for c in CHECKS if c in f.ran],
        "checks_total": len(CHECKS),
        "failed": _dedupe(f.failed),
        "skipped": f.skipped,
        "unsupported": f.unsupported,
        "notes": notes,
        "ok": not f.failed,
    }


def _dedupe(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render(report: Mapping[str, Any], tz: Optional[tzinfo] = None) -> str:
    del tz  # every date in the report is a day, already in UTC
    window = report["window"]
    counts = (
        f"{_plural(len(report['proposals']), 'proposal')}, {len(report['unclear'])} unclear, "
        f"{len(report['listed'])} listed for you, {len(report['possibly_done'])} possibly done"
    )
    stale = f"stale after {window['stale_days']} days" + (
        "" if window["stale_days_set"] else ", the default while stale_days isn't set"
    )
    scope = (
        f"(merged {refs.change_word(report.get('forge'))}s since {window['merged_since'][:10]}; "
        "duplicates among open issues and "
        f"those closed since {window['closed_since'][:10]}; {stale})"
    )
    ran = report["checks_run"]
    checks = f"Checks run ({len(ran)} of {report['checks_total']}): " + (
        ", ".join(ran) if ran else "none"
    )
    lines = [f"{report['project']} on {report['host']}: {counts}", scope, checks, "", "Proposals"]
    for p in report["proposals"]:
        lines.append(f"  {p['n']}. {p['id']}: {p['evidence']}")
    if not report["proposals"]:
        lines.append("  none" + (" from the checks that ran" if report["failed"] else ""))
    lines.append("Unclear (not acted on)")
    for u in report["unclear"]:
        lines.append(f"  {u['evidence']}")
    if not report["unclear"]:
        lines.append("  none")
    lines.append("Listed for you")
    for item in report["listed"]:
        lines.append(f"  {item['evidence']}")
    if not report["listed"]:
        lines.append("  none")
    lines.append("Possibly done (listed, not proposed)")
    for item in report["possibly_done"]:
        lines.append(f"  {item['evidence']}")
    if not report["possibly_done"]:
        lines.append("  none")
    if report["failed"]:
        lines.append("FAILED (nothing above speaks for these)")
        for failed in report["failed"]:
            lines.append(f"  {failed['part']}: FAILED ({failed['reason']})")
    if report["skipped"]:
        lines.append("Skipped (not configured)")
        for skipped in report["skipped"]:
            lines.append(f"  {skipped['part']}: {skipped['reason']}")
    if report["unsupported"]:
        lines.append(f"Unsupported on {FORGE_NAMES.get(report['forge'], report['forge'])}")
        for item in report["unsupported"]:
            lines.append(f"  {item['part']}: {item['reason']}")
    for note in report["notes"]:
        lines.append(f"({note})")
    return "\n".join(lines) + "\n"


# --- applying approved proposals ----------------------------------------------------------------


class ApplyError(ValueError):
    """An approval list that can't be applied; the message names every problem."""


def approved(payload: Any, report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The current proposals the user approved, in their order. Every id must be a proposal in
    this run's report: one that's gone (fixed, now unclear, or its check FAILED) refuses the
    whole list, so nothing is written on a stale approval."""
    if not isinstance(payload, dict) or set(payload) != {"approved"}:
        raise ApplyError('the approval must be a JSON object {"approved": ["close #14", ...]}')
    ids = payload["approved"]
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ApplyError("approved must be a non-empty list of proposal ids")
    current = {p["id"]: p for p in report["proposals"]}
    errors = []
    seen: set[str] = set()
    out = []
    for id_ in ids:
        if id_ in seen:
            errors.append(f"{id_!r} is listed twice")
            continue
        seen.add(id_)
        if id_ not in current:
            errors.append(
                f"{id_!r} isn't a current proposal (fixed already, now unclear, or its check "
                "FAILED this run)"
            )
            continue
        out.append(current[id_])
    if errors:
        raise ApplyError("; ".join(errors))
    return out
