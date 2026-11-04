"""The Forgejo (and Gitea) adapter: REST `/api/v1` through urllib with the user's token, sent as
`Authorization: token <t>`. `tea`'s output is meant for terminals and covers only part of these
reads, so forge-tracker borrows its login's token instead of running it (see cli.open_forge).

Forgejo's API is shaped like GitHub's REST in places (issues, comments, pulls, labels) and not in
others: there's no GraphQL, so an open PR's combined status and reviews, an issue's dependencies
and the compare against the default branch are one read per item; closing references aren't an
API, so they're read from closing keywords in PR bodies; and Actions has a runs API only on newer
instances. A read an instance can't answer raises Unsupported, which the reports show as
unsupported on Forgejo, never as empty. Every method raises ForgeError on failure.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional, TypeVar

from .. import refs
from ..model import (
    Branch,
    Change,
    ClosingRef,
    Comment,
    ForgeError,
    HTTPStatusError,
    Issue,
    IssueRef,
    Label,
    LabelEvent,
    Link,
    Mention,
    Milestone,
    MilestoneRef,
    Repo,
    Run,
    Tag,
    Unsupported,
    User,
    iso,
    parse_time,
)
from ..transport import ReadTransport
from .common import pick_tags

T = TypeVar("T")
U = TypeVar("U")

WORKERS = 4
PAGE_SIZE = 50  # Forgejo's default MAX_RESPONSE_ITEMS; a larger `limit` is cut to it anyway
AUTH_SCHEME = "token"

_BOT_NAME = re.compile(r"(?:\[bot\]|[-_]bot)$", re.IGNORECASE)
_DRAFT_TITLE = re.compile(r"^\s*(?:\[wip\]|wip:|draft:|\[draft\])", re.IGNORECASE)
# Forgejo Actions run statuses (its own runs API) still going, and the finished ones' conclusion.
_RUNNING = {"running", "waiting", "blocked", "queued", "in_progress", "pending"}
_CONCLUSION = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "skipped": "skipped",
}


def default_api_base(host: str) -> str:
    return f"https://{host}/api/v1"


def _is_bot(user: Optional[dict]) -> bool:
    return bool(_BOT_NAME.search((user or {}).get("login") or ""))


def _login(user: Optional[dict]) -> Optional[str]:
    return (user or {}).get("login")


def conflicts(mergeable: Any) -> str:
    """Forgejo's `mergeable` as yes/no/unknown. `true` is clean; `false` is a conflict (the API
    has no separate "still checking" state, so a PR checked in the seconds after its base moved
    can read false until the check finishes); anything else is unknown."""
    if mergeable is True:
        return "no"
    if mergeable is False:
        return "yes"
    return "unknown"


def _issue(raw: dict) -> Issue:
    milestone = raw.get("milestone")
    return Issue(
        number=int(raw["number"]),
        title=raw.get("title") or "",
        state="open" if raw.get("state") == "open" else "closed",
        labels=tuple(label.get("name") or "" for label in raw.get("labels") or []),
        assignees=tuple(_login(a) or "" for a in raw.get("assignees") or []),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        updated=parse_time(raw["updated_at"]),  # type: ignore[arg-type]
        closed=parse_time(raw.get("closed_at")),
        milestone=Milestone(milestone.get("title") or "", milestone.get("due_on") or None)
        if milestone
        else None,
        author=_login(raw.get("user")),
        url=raw.get("html_url") or "",
        body=raw.get("body") or "",
    )


def _review(reviews: list[dict], requested: tuple[str, ...]) -> Optional[str]:
    """The review decision in GitHub's words, from each reviewer's newest review that counts."""
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: int(r.get("id") or 0)):
        state = (review.get("state") or "").upper()
        if review.get("dismissed") or state not in ("APPROVED", "REQUEST_CHANGES"):
            continue
        latest[_login(review.get("user")) or ""] = state
    if "REQUEST_CHANGES" in latest.values():
        return "changes_requested"
    if "APPROVED" in latest.values():
        return "approved"
    return "review_required" if requested else None


def _pipeline(status: Optional[dict]) -> Optional[str]:
    """A combined commit status (`pending`, `success`, `error`, `failure`, `warning`) as the
    reports' check state, or None when the commit has no statuses."""
    if not status or not int(status.get("total_count") or len(status.get("statuses") or [])):
        return None
    state = (status.get("state") or "").lower()
    return {"warning": "success"}.get(state, state) or None


def _change(
    raw: dict,
    status: Optional[dict] = None,
    reviews: Optional[list[dict]] = None,
    project: Optional[str] = None,
) -> Change:
    requested = tuple(_login(r) or "" for r in raw.get("requested_reviewers") or [])
    title = raw.get("title") or ""
    return Change(
        number=int(raw["number"]),
        title=title,
        branch=(raw.get("head") or {}).get("ref") or "",
        draft=bool(raw.get("draft")) or bool(_DRAFT_TITLE.match(title)),
        author=_login(raw.get("user")),
        assignees=tuple(_login(a) or "" for a in raw.get("assignees") or []),
        reviewers=requested,
        conflicts=conflicts(raw.get("mergeable")),
        pipeline=_pipeline(status),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        updated=parse_time(raw["updated_at"]),  # type: ignore[arg-type]
        url=raw.get("html_url") or "",
        merged_at=parse_time(raw.get("merged_at")),
        merge_sha=raw.get("merge_commit_sha"),
        closing_issues=tuple(refs.closing_numbers(raw.get("body") or "", project)),
        review=None if reviews is None else _review(reviews, requested),
    )


def _run(raw: dict) -> Run:
    """A run from either runs API: Gitea's GitHub-shaped one (`status` and `conclusion`) or
    Forgejo's own (`status` alone: running, waiting, success, failure, cancelled, ...)."""
    status = (raw.get("status") or "").lower()
    if "conclusion" in raw:
        state, conclusion = status, raw.get("conclusion")
    elif status in _RUNNING:
        state, conclusion = status, None
    else:
        state, conclusion = "completed", _CONCLUSION.get(status, status or None)
    return Run(
        id=int(raw["id"]),
        name=raw.get("display_title") or raw.get("title") or raw.get("name") or "",
        event=raw.get("event") or raw.get("trigger_event") or "",
        status=state,
        conclusion=conclusion,
        attempt=int(raw.get("run_attempt") or 1),
        created=parse_time(  # type: ignore[arg-type]
            raw.get("created_at")
            or raw.get("started_at")
            or raw.get("created")
            or raw.get("started")
        ),
        url=raw.get("html_url") or "",
    )


def _run_branch(raw: dict) -> str:
    branch = raw.get("head_branch") or raw.get("prettyref") or raw.get("ref") or ""
    return branch.removeprefix("refs/heads/")


class Forgejo:
    forge = "forgejo"

    def __init__(self, transport: ReadTransport, project: str):
        owner, _, name = project.partition("/")
        if not owner or not name or "/" in name:
            raise ForgeError(f"{project!r} isn't owner/repo")
        self.t = transport
        self.project = project
        self.repo_path = f"/repos/{owner}/{name}"

    def ref(self, number: int, change: bool = False) -> str:
        return f"#{number}"

    def _each(self, fn: Callable[[T], U], items: Iterable[T]) -> list[U]:
        items = list(items)
        if len(items) < 2:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(fn, items))

    def _project_of(self, raw: dict) -> Optional[str]:
        """None when an issue is in this project, else its `owner/repo`."""
        full = (raw.get("repository") or {}).get("full_name")
        return None if full in (None, self.project) else full

    # --- identity and repo ---------------------------------------------------------------------

    def identity(self) -> User:
        raw = self.t.get("/user")
        if not isinstance(raw, dict) or not raw.get("login"):
            raise ForgeError("/user returned no login")
        return User(login=raw["login"], is_bot=_is_bot(raw))

    def repo(self) -> Repo:
        raw = self.t.get(self.repo_path)
        if not isinstance(raw, dict) or not raw.get("default_branch"):
            raise ForgeError(f"{self.repo_path} returned no default_branch")
        return Repo(default_branch=raw["default_branch"])

    def labels(self) -> list[Label]:
        raw = self.t.get_all(f"{self.repo_path}/labels")
        return [Label(item["name"], item.get("description") or "") for item in raw]

    # --- issues and comments -------------------------------------------------------------------

    def issues_since(self, since: datetime) -> list[Issue]:
        raw = self.t.get_all(
            f"{self.repo_path}/issues", {"state": "all", "type": "issues", "since": iso(since)}
        )
        return [_issue(item) for item in raw if not item.get("pull_request")]

    def open_issues(self) -> list[Issue]:
        raw = self.t.get_all(f"{self.repo_path}/issues", {"state": "open", "type": "issues"})
        return [_issue(item) for item in raw if not item.get("pull_request")]

    def comments_since(self, since: datetime) -> list[Comment]:
        """Comments created at or after `since`, repo-wide. `since` filters on update time, so
        older comments edited since are dropped here."""
        raw = self.t.get_all(f"{self.repo_path}/issues/comments", {"since": iso(since)})
        out = []
        for item in raw:
            created = parse_time(item.get("created_at"))
            if created is None or created < since:
                continue
            number = str(item.get("issue_url") or "").rstrip("/").rsplit("/", 1)[-1]
            if not number.isdigit():
                continue
            out.append(
                Comment(
                    issue=int(number),
                    author=_login(item.get("user")) or "",
                    author_is_bot=_is_bot(item.get("user")),
                    created=created,
                    body=item.get("body") or "",
                    url=item.get("html_url") or "",
                )
            )
        out.sort(key=lambda c: c.created)
        return out

    def mentions_since(self, login: str, since: datetime) -> list[Mention]:
        """Open issues and PRs in this repo that mention you, updated since `since`, from the
        issue search (`mentioned=true` is the authenticated user)."""
        owner = self.project.split("/", 1)[0]
        raw = self.t.get_all(
            "/repos/issues/search",
            {"mentioned": "true", "state": "open", "since": iso(since), "owner": owner},
        )
        return [
            Mention(
                number=int(item["number"]),
                title=item.get("title") or "",
                is_change=bool(item.get("pull_request")),
                updated=parse_time(item["updated_at"]),  # type: ignore[arg-type]
                url=item.get("html_url") or "",
            )
            for item in raw
            if (item.get("repository") or {}).get("full_name", self.project) == self.project
        ]

    # --- changes -------------------------------------------------------------------------------

    def _status(self, sha: Optional[str]) -> Optional[dict]:
        if not sha:
            return None
        status = self.t.get(f"{self.repo_path}/commits/{sha}/status")
        return status if isinstance(status, dict) else None

    def open_changes(self) -> list[Change]:
        """Open PRs, each read once more for its head commit's combined status and its reviews."""
        raw = self.t.get_all(f"{self.repo_path}/pulls", {"state": "open"})

        def extra(pr: dict) -> tuple[Optional[dict], list[dict]]:
            status = self._status((pr.get("head") or {}).get("sha"))
            reviews = self.t.get_all(f"{self.repo_path}/pulls/{int(pr['number'])}/reviews")
            return status, reviews

        extras = self._each(extra, raw)
        return [_change(pr, s, r, self.project) for pr, (s, r) in zip(raw, extras)]

    def merged_since(self, since: datetime) -> list[Change]:
        """PRs merged at or after `since`, newest-updated first, read back until one updated
        before `since` (which can't have merged after it). Closing issues come from the closing
        keywords in each body."""
        raw = self.t.get_all(
            f"{self.repo_path}/pulls",
            {"state": "closed", "sort": "recentupdate"},
            stop=lambda pr: (parse_time(pr.get("updated_at")) or since) < since,
        )
        out = []
        for pr in raw:
            merged_at = parse_time(pr.get("merged_at"))
            if pr.get("merged") is False or merged_at is None or merged_at < since:
                continue
            out.append(_change(pr, project=self.project))
        return out

    # --- CI ------------------------------------------------------------------------------------

    def _runs(self, params: dict[str, Any], stop: Callable[[Any], bool]) -> list[dict]:
        """Actions runs, newest first, up to `stop`. Unsupported when the instance has no runs
        API (older Forgejo and Gitea answer 404)."""
        try:
            return self.t.get_all(f"{self.repo_path}/actions/runs", params, "workflow_runs", stop)
        except HTTPStatusError as error:
            # A 404 is a missing runs API only when the repo itself reads; otherwise it's the
            # repo that's missing (or hidden from this token), which is a plain failure.
            if error.status == 404 and isinstance(self.t.get(self.repo_path), dict):
                raise Unsupported("this instance has no Actions runs API") from None
            raise

    def latest_run(self, branch: str, event: str) -> Optional[Run]:
        """The newest Actions run on `branch` for `event`, with its failed jobs. Without a runs
        API the push run falls back to the branch head's combined commit status, and the
        schedule run is unsupported, since a status doesn't say what triggered it."""
        found: list[dict] = []

        def match(raw: dict) -> bool:
            if (raw.get("event") or raw.get("trigger_event")) == event and _run_branch(
                raw
            ) == branch:
                found.append(raw)
                return True
            return False

        try:
            self._runs({"event": event, "branch": branch}, match)
        except Unsupported:
            if event != "push":
                raise Unsupported(
                    f"this instance has no Actions runs API, and a commit status doesn't say "
                    f"what triggered it, so there's no {event} run to show"
                ) from None
            return self._status_run(branch)
        if not found:
            return None
        run = _run(found[0])
        if run.conclusion in ("failure", "timed_out", "cancelled"):
            run = replace(run, failed_jobs=self._failed_jobs(run.id))
        return run

    def _failed_jobs(self, run_id: int) -> tuple[str, ...]:
        try:
            jobs = self.t.get_all(f"{self.repo_path}/actions/runs/{run_id}/jobs", key="jobs")
        except HTTPStatusError as error:
            if error.status == 404:
                return ()  # no jobs API here; the run's own conclusion still shows the failure
            raise
        return tuple(
            j.get("name") or ""
            for j in jobs
            if (j.get("conclusion") or j.get("status")) in ("failure", "timed_out", "cancelled")
        )

    def _status_run(self, branch: str) -> Optional[Run]:
        status = self.t.get(f"{self.repo_path}/commits/{urllib.parse.quote(branch)}/status")
        if not isinstance(status, dict) or not _pipeline(status):
            return None
        statuses = status.get("statuses") or []
        state = (status.get("state") or "").lower()
        sha = (status.get("sha") or "")[:7]
        created = max(
            (
                t
                for t in (parse_time(s.get("updated_at") or s.get("created_at")) for s in statuses)
                if t is not None
            ),
            default=None,
        )
        if created is None:
            return None
        return Run(
            id=0,
            name=f"commit status of {sha}" if sha else "commit status",
            event="push",
            status="in_progress" if state == "pending" else "completed",
            conclusion=None
            if state == "pending"
            else ("success" if state in ("success", "warning") else "failure"),
            attempt=1,
            created=created,
            failed_jobs=tuple(
                s.get("context") or ""
                for s in statuses
                if (s.get("status") or s.get("state")) in ("failure", "error")
            ),
        )

    def passed_on_retry(self, branch: str, since: datetime) -> Counter:
        """Job name -> how many runs on `branch` created since `since` it passed only on a retry.
        Needs a runs API that numbers attempts (Gitea's, with `run_attempt`); otherwise it's
        unsupported."""
        runs = [
            r
            for r in self._runs(
                {"branch": branch},
                stop=lambda r: (
                    (parse_time(r.get("created_at") or r.get("created")) or since) < since
                ),
            )
            if _run_branch(r) == branch
        ]
        if runs and not any("run_attempt" in r for r in runs):
            raise Unsupported("this instance's runs API doesn't number retried attempts")
        counts: Counter = Counter()
        for raw in runs:
            if int(raw.get("run_attempt") or 1) < 2:
                continue
            jobs = self.t.get_all(
                f"{self.repo_path}/actions/runs/{int(raw['id'])}/jobs", key="jobs"
            )
            by_name: dict[str, list[dict]] = {}
            for job in jobs:
                by_name.setdefault(job.get("name") or "", []).append(job)
            for name, attempts in by_name.items():
                attempts.sort(key=lambda j: int(j.get("run_attempt") or 1))
                failed_before = any(j.get("conclusion") == "failure" for j in attempts[:-1])
                if failed_before and attempts[-1].get("conclusion") == "success":
                    counts[name] += 1
        return counts

    # --- tags, branches, links -----------------------------------------------------------------

    def tags(self, patterns: tuple[str, ...]) -> list[Tag]:
        globs = [p for p in patterns if not p.startswith("registry:")]
        releases = self._releases() if globs else []
        tags = self.t.get_all(f"{self.repo_path}/tags") if globs else []
        dated = [
            (parse_time(r.get("published_at") or r.get("created_at")), r.get("tag_name") or "")
            for r in releases
            if not r.get("draft")
        ]
        return pick_tags(patterns, dated, [t.get("name") or "" for t in tags], "Forgejo")

    def _releases(self) -> list[dict]:
        """The releases, or none when the repo has releases turned off (a 404 while the repo
        itself reads)."""
        try:
            return self.t.get_all(f"{self.repo_path}/releases")
        except HTTPStatusError as error:
            if error.status == 404 and isinstance(self.t.get(self.repo_path), dict):
                return []
            raise

    def _compare(self, base: str, head: str) -> dict:
        spec = urllib.parse.quote(f"{base}...{head}", safe="/.")
        compare = self.t.get(f"{self.repo_path}/compare/{spec}")
        if not isinstance(compare, dict) or "total_commits" not in compare:
            raise ForgeError(f"compare {base}...{head} returned no total_commits")
        return compare

    def branches(self, default_branch: str, open_changes: list[Change]) -> list[Branch]:
        with_change = {c.branch for c in open_changes}
        raw = [
            b
            for b in self.t.get_all(f"{self.repo_path}/branches")
            if b.get("name") != default_branch
        ]

        def one(b: dict) -> Branch:
            name = b["name"]
            ahead = head_date = None
            if name not in with_change:
                compare = self._compare(default_branch, name)
                ahead = int(compare["total_commits"])
                dates = [
                    parse_time(((c.get("commit") or {}).get("committer") or {}).get("date"))
                    for c in compare.get("commits") or []
                ]
                dates = [d for d in dates if d is not None]
                head_date = max(dates) if dates else None
            return Branch(
                name,
                (b.get("commit") or {}).get("id") or "",
                name in with_change,
                ahead,
                protected=bool(b.get("protected")),
                head_date=head_date,
            )

        return self._each(one, raw)

    # --- /groom: milestones, label history, per-issue comments, reachability ---------------------

    def milestones(self) -> list[MilestoneRef]:
        raw = self.t.get_all(f"{self.repo_path}/milestones", {"state": "open"})
        return [
            MilestoneRef(int(m["id"]), m.get("title") or "", m.get("due_on") or None) for m in raw
        ]

    def label_events(self, number: int) -> list[LabelEvent]:
        """Label changes from the issue's timeline: a `label` event whose body is "1" added the
        label, an empty one removed it."""
        raw = self.t.get_all(f"{self.repo_path}/issues/{number}/timeline")
        return [
            LabelEvent(
                event="labeled" if (e.get("body") or "") == "1" else "unlabeled",
                label=(e.get("label") or {}).get("name") or "",
                created=parse_time(e["created_at"]),  # type: ignore[arg-type]
            )
            for e in raw
            if e.get("type") == "label"
        ]

    def issue_comments(self, number: int) -> list[Comment]:
        raw = self.t.get_all(f"{self.repo_path}/issues/{number}/comments")
        return [
            Comment(
                issue=number,
                author=_login(item.get("user")) or "",
                author_is_bot=_is_bot(item.get("user")),
                created=parse_time(item["created_at"]),  # type: ignore[arg-type]
                body=item.get("body") or "",
                url=item.get("html_url") or "",
            )
            for item in raw
        ]

    def on_branch(self, branch: str, sha: str) -> bool:
        """Whether commit `sha` is reachable from `branch`: nothing in `branch...sha` that the
        branch lacks."""
        return int(self._compare(branch, sha)["total_commits"]) == 0

    def issue_ref(self, number: int, project: Optional[str] = None) -> IssueRef:
        other = project if project and project != self.project else None
        raw = self.t.get(f"/repos/{other or self.project}/issues/{number}")
        if not isinstance(raw, dict) or "state" not in raw:
            raise ForgeError(f"{other or ''}#{number} returned no issue")
        return IssueRef(
            number=number,
            state="open" if raw.get("state") == "open" else "closed",
            is_change=bool(raw.get("pull_request")),
            id=int(raw["id"]) if raw.get("id") else None,
            project=other,
            closed=parse_time(raw.get("closed_at")),
        )

    def blocked_by(self, number: int) -> list[Link]:
        """The issues one issue depends on (Forgejo's native blocking), whatever their state."""
        return [
            Link(
                blocked=number,
                blocker=int(raw["number"]),
                blocker_open=raw.get("state") == "open",
                kind="native",
                blocker_project=self._project_of(raw),
                blocker_closed=parse_time(raw.get("closed_at")),
            )
            for raw in self.t.get_all(f"{self.repo_path}/issues/{number}/dependencies")
        ]

    def blocking_links(self) -> list[Link]:
        """Every open issue's dependencies, one read per issue."""
        issues = self.open_issues()
        per_issue = self._each(lambda i: self.blocked_by(i.number), issues)
        return [link for links in per_issue for link in links]

    # --- /next: the fingerprint and closing references -----------------------------------------

    def fingerprint_inputs(self) -> dict[str, Any]:
        """The reads `/next` fingerprints its facts with, in the same shape as GitHub's. The issue
        list can't be sorted by update time, so the newest issue is the newest open one (closing
        an issue moves the open count instead), and each open PR's head commit has its combined
        status read once."""
        repo = self.t.get(self.repo_path)
        if not isinstance(repo, dict) or not repo.get("default_branch"):
            raise ForgeError(f"{self.repo_path} returned no default branch for the fingerprint")
        default = repo["default_branch"]
        heads = {
            b["name"]: (b.get("commit") or {}).get("id")
            for b in self.t.get_all(f"{self.repo_path}/branches")
        }
        issues = self.open_issues()
        prs = self.t.get_all(f"{self.repo_path}/pulls", {"state": "open"})
        statuses = self._each(lambda pr: self._status((pr.get("head") or {}).get("sha")), prs)
        milestones = self.t.get_all(f"{self.repo_path}/milestones", {"state": "all"})
        return {
            "default_branch": default,
            "default_head": heads.get(default),
            "open_issues": len(issues),
            "open_changes": len(prs),
            # Forgejo writes times in the server's own offset, so they're compared as times.
            "newest_issue": max((iso(i.updated) for i in issues), default=None),
            "newest_change": max(
                (iso(t) for t in (parse_time(pr.get("updated_at")) for pr in prs) if t),
                default=None,
            ),
            "milestones": sorted(
                [
                    str(m.get("id") or ""),
                    m.get("title") or "",
                    m.get("due_on") or "",
                    m.get("state") or "",
                ]
                for m in milestones
            ),
            "branch_heads": heads,
            "change_heads": {
                str(pr["number"]): [(pr.get("head") or {}).get("sha"), _pipeline(s) or "none"]
                for pr, s in zip(prs, statuses)
            },
        }

    def closing_refs(self) -> list[ClosingRef]:
        """Every PR, open, merged or closed, whose body closes an open issue with a closing
        keyword (`Closes #N`), the reference Forgejo acts on when the PR merges. Forgejo has no
        API for these, so every PR is read."""
        open_numbers = {i.number for i in self.open_issues()}
        out = []
        for pr in self.t.get_all(f"{self.repo_path}/pulls", {"state": "all"}):
            for number in refs.closing_numbers(pr.get("body") or "", self.project):
                if number not in open_numbers:
                    continue
                state = (
                    "merged"
                    if pr.get("merged") or pr.get("merged_at")
                    else "open"
                    if pr.get("state") == "open"
                    else "closed"
                )
                out.append(
                    ClosingRef(
                        issue=number,
                        change=int(pr["number"]),
                        state=state,
                        branch=(pr.get("head") or {}).get("ref") or "",
                        base=(pr.get("base") or {}).get("ref") or "",
                        merged_at=parse_time(pr.get("merged_at")),
                        title=pr.get("title") or "",
                        merge_sha=pr.get("merge_commit_sha"),
                    )
                )
        out.sort(key=lambda r: (r.issue, r.change))
        return out
