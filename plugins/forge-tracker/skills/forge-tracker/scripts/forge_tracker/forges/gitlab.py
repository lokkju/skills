"""The GitLab adapter: REST v4 through `glab api` (see transport.glab_http), so glab's own login,
host settings and pinned CA apply. The list endpoints leave out what a per-item read has (an MR's
head pipeline, its closing issues, an issue's links), so those are read one item at a time, a few
at once. Every method raises ForgeError on failure; callers wrap each one in `attempt`.

Blocking: GitLab's native `blocks` / `is_blocked_by` links exist only on the paid tiers. On CE
the config's `blocking: relates_to+blocked` convention makes a `relates_to` link from an issue
carrying the blocked label count as "blocked by" the linked issue, with `kind: convention`.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, TypeVar

from ..model import (
    Branch,
    Change,
    ChangedFile,
    ClosingRef,
    Comment,
    CrossRef,
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

# The `blocking:` values: native links only (the default), or the CE convention.
NATIVE, CONVENTION = "native", "relates_to+blocked"
BLOCKING = (NATIVE, CONVENTION)

WORKERS = 4

# detailed_merge_status values that mean the mergeability check hasn't finished.
_UNCHECKED = {"unchecked", "checking", "preparing", "approvals_syncing", "cannot_be_merged_recheck"}
# Pipeline statuses that are still going, and the GitHub-style conclusion of the finished ones.
_RUNNING = {
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "scheduled",
    "waiting_for_callback",
    "canceling",
}
_CONCLUSION = {
    "success": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "skipped": "skipped",
    "manual": "action_required",
}
# A head pipeline's status as the combined check state the reports read (GitHub's rollup words).
_PIPELINE = {
    "success": "success",
    "failed": "failure",
    "canceled": "error",
    "canceling": "error",
    "manual": "pending",
}
_BOT_NAME = re.compile(r"^(?:project|group)_\d+_bot(?:_[0-9a-f]+)?$|\[bot\]$")


def default_api_base(host: str) -> str:
    return f"https://{host}/api/v4"


def project_path(project: str) -> str:
    return "/projects/" + urllib.parse.quote(project, safe="")


def _is_bot(user: Optional[dict]) -> bool:
    if not user:
        return False
    return bool(user.get("bot")) or bool(_BOT_NAME.search(user.get("username") or ""))


def _login(user: Optional[dict]) -> Optional[str]:
    return (user or {}).get("username")


def conflicts(
    detailed: Optional[str], has_conflicts: Any, merge_status: Optional[str] = None
) -> str:
    """GitLab's mergeability as yes/no/unknown. `has_conflicts` is believed only once the status
    says the check ran: right after the target branch moves GitLab reports `unchecked` (or
    `checking`) with `has_conflicts: false` until it re-checks."""
    status = (detailed or "").lower()
    if not status:
        legacy = (merge_status or "").lower()
        if not legacy or legacy in _UNCHECKED:
            return "unknown"
        status = legacy
    if status == "conflict":
        return "yes"
    if status in _UNCHECKED:
        return "unknown"
    if has_conflicts is True:
        return "yes"
    if has_conflicts is False:
        return "no"
    return "unknown"


def pipeline_state(pipeline: Optional[dict]) -> Optional[str]:
    """A head pipeline's status in the reports' words, or None when there's no pipeline (or it
    was skipped, which ran nothing)."""
    status = ((pipeline or {}).get("status") or "").lower()
    if not status or status == "skipped":
        return None
    if status in _PIPELINE:
        return _PIPELINE[status]
    if status in _RUNNING:
        return "pending"
    return status


def _milestone(raw: Optional[dict]) -> Optional[Milestone]:
    if not raw:
        return None
    return Milestone(raw.get("title") or "", raw.get("due_date") or None)


def _labels(raw: Iterable[Any]) -> tuple[str, ...]:
    return tuple(label if isinstance(label, str) else label.get("name") or "" for label in raw)


def _state(value: Optional[str]) -> str:
    return "open" if value in (None, "opened", "open", "reopened") else "closed"


def _issue(raw: dict) -> Issue:
    return Issue(
        number=int(raw["iid"]),
        title=raw.get("title") or "",
        state=_state(raw.get("state")),
        labels=_labels(raw.get("labels") or []),
        assignees=tuple(_login(a) or "" for a in raw.get("assignees") or []),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        updated=parse_time(raw["updated_at"]),  # type: ignore[arg-type]
        closed=parse_time(raw.get("closed_at")),
        milestone=_milestone(raw.get("milestone")),
        author=_login(raw.get("author")),
        url=raw.get("web_url") or "",
        body=raw.get("description") or "",
    )


def _review(approvals: Optional[dict], detailed: Optional[str]) -> Optional[str]:
    """The review decision in GitHub's words. None when no approval is required and nobody has
    approved, which is how GitHub reports a branch that requires no review."""
    if (detailed or "").lower() == "requested_changes":
        return "changes_requested"
    if not approvals:
        return None
    required = int(approvals.get("approvals_required") or 0)
    if required > 0:
        return "approved" if approvals.get("approved") else "review_required"
    return "approved" if approvals.get("approved_by") else None


def _change(
    raw: dict,
    detail: Optional[dict] = None,
    approvals: Optional[dict] = None,
    closing: tuple[int, ...] = (),
) -> Change:
    merged = detail or raw
    return Change(
        number=int(raw["iid"]),
        title=raw.get("title") or "",
        branch=raw.get("source_branch") or "",
        draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        author=_login(raw.get("author")),
        assignees=tuple(_login(a) or "" for a in raw.get("assignees") or []),
        reviewers=tuple(_login(r) or "" for r in raw.get("reviewers") or []),
        conflicts=conflicts(
            merged.get("detailed_merge_status"),
            merged.get("has_conflicts"),
            merged.get("merge_status"),
        ),
        pipeline=pipeline_state(merged.get("head_pipeline")),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        updated=parse_time(raw["updated_at"]),  # type: ignore[arg-type]
        url=raw.get("web_url") or "",
        merged_at=parse_time(raw.get("merged_at")),
        merge_sha=raw.get("merge_commit_sha") or raw.get("squash_commit_sha"),
        closing_issues=closing,
        review=_review(approvals, merged.get("detailed_merge_status")),
    )


def _run(raw: dict) -> Run:
    status = (raw.get("status") or "").lower()
    return Run(
        id=int(raw["id"]),
        name=raw.get("name") or "pipeline",
        event=raw.get("source") or "",
        status=status if status in _RUNNING else "completed",
        conclusion=None if status in _RUNNING else _CONCLUSION.get(status, status or None),
        attempt=1,
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        url=raw.get("web_url") or "",
    )


def _reference_project(raw: dict, sigil: str) -> Optional[str]:
    """The project path in an item's full reference (`group/sub/project#5`, `...!5`)."""
    full = (raw.get("references") or {}).get("full") or ""
    if sigil in full:
        return full.rsplit(sigil, 1)[0] or None
    return None


class GitLab:
    forge = "gitlab"

    def __init__(
        self,
        transport: ReadTransport,
        project: str,
        blocking: Optional[str] = None,
        blocked_label: Optional[str] = None,
    ):
        if "/" not in project:
            raise ForgeError(f"{project!r} isn't a group/project path")
        if blocking not in (None, *BLOCKING):
            raise ForgeError(f"blocking: {blocking!r} isn't one of {', '.join(BLOCKING)}")
        self.t = transport
        self.project = project
        self.repo_path = project_path(project)
        self.convention = blocking == CONVENTION
        self.blocked_label = blocked_label or "blocked"
        self._lock = threading.Lock()
        self._raw_project: Optional[dict] = None

    def ref(self, number: int, change: bool = False) -> str:
        return f"!{number}" if change else f"#{number}"

    def _each(self, fn: Callable[[T], U], items: Iterable[T]) -> list[U]:
        """`fn` over `items`, a few at once, in order. The first ForgeError fails the read."""
        items = list(items)
        if len(items) < 2:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(fn, items))

    def _project(self) -> dict:
        with self._lock:
            if self._raw_project is None:
                raw = self.t.get(self.repo_path)
                if not isinstance(raw, dict) or "id" not in raw:
                    raise ForgeError(f"{self.repo_path} returned no project")
                self._raw_project = raw
            return self._raw_project

    def _other_project(self, raw: dict, sigil: str) -> Optional[str]:
        """None when `raw` (an issue or MR) is in this project, else its project path."""
        project_id = raw.get("project_id")
        if project_id is not None and project_id == self._project().get("id"):
            return None
        path = _reference_project(raw, sigil)
        if path == self.project:
            return None
        if path:
            return path
        return None if project_id is None else f"project {project_id}"

    # --- identity and repo ---------------------------------------------------------------------

    def identity(self) -> User:
        raw = self.t.get("/user")
        if not isinstance(raw, dict) or not raw.get("username"):
            raise ForgeError("/user returned no username")
        return User(login=raw["username"], is_bot=_is_bot(raw))

    def repo(self) -> Repo:
        raw = self._project()
        if not raw.get("default_branch"):
            raise ForgeError(f"{self.project} has no default branch (an empty repository?)")
        return Repo(default_branch=raw["default_branch"])

    def labels(self) -> list[Label]:
        raw = self.t.get_all(f"{self.repo_path}/labels")
        return [Label(item["name"], item.get("description") or "") for item in raw]

    # --- issues and comments -------------------------------------------------------------------

    def issues_since(self, since: datetime) -> list[Issue]:
        raw = self.t.get_all(
            f"{self.repo_path}/issues",
            {"updated_after": iso(since), "order_by": "updated_at", "sort": "desc"},
        )
        return [_issue(item) for item in raw]

    def open_issues(self) -> list[Issue]:
        raw = self.t.get_all(f"{self.repo_path}/issues", {"state": "opened"})
        return [_issue(item) for item in raw]

    def comments_since(self, since: datetime) -> list[Comment]:
        """Issue comments created at or after `since`. GitLab has no project-wide list of notes,
        so this reads the project's note events (`after` takes a date and is exclusive, hence the
        day before) and keeps the non-system notes on issues."""
        after = (since - timedelta(days=1)).date().isoformat()
        raw = self.t.get_all(f"{self.repo_path}/events", {"target_type": "note", "after": after})
        out = []
        for event in raw:
            note = event.get("note") or {}
            if note.get("noteable_type") != "Issue" or note.get("system"):
                continue
            created = parse_time(note.get("created_at") or event.get("created_at"))
            number = note.get("noteable_iid")
            if created is None or created < since or number is None:
                continue
            author = note.get("author") or event.get("author")
            out.append(
                Comment(
                    issue=int(number),
                    author=_login(author) or "",
                    author_is_bot=_is_bot(author),
                    created=created,
                    body=note.get("body") or "",
                )
            )
        out.sort(key=lambda c: c.created)
        return out

    def mentions_since(self, login: str, since: datetime) -> list[Mention]:
        """Your pending to-do items in this project that mention you (or address you directly),
        created since `since`, on open issues and MRs. To-do items belong to the authenticated
        user, so `login` only has to be that user."""
        project_id = self._project()["id"]
        raw = self.t.get_all("/todos", {"project_id": project_id})
        out = []
        for todo in raw:
            if todo.get("action_name") not in ("mentioned", "directly_addressed"):
                continue
            created = parse_time(todo.get("created_at"))
            target = todo.get("target") or {}
            if created is None or created < since or _state(target.get("state")) != "open":
                continue
            if target.get("iid") is None:
                continue
            out.append(
                Mention(
                    number=int(target["iid"]),
                    title=target.get("title") or "",
                    is_change=todo.get("target_type") == "MergeRequest",
                    updated=created,
                    url=todo.get("target_url") or target.get("web_url") or "",
                )
            )
        return out

    # --- changes -------------------------------------------------------------------------------

    def _open_mrs(self) -> list[dict]:
        return self.t.get_all(f"{self.repo_path}/merge_requests", {"state": "opened"})

    def _detail(self, iid: int) -> tuple[dict, dict]:
        """One MR's own read (head pipeline, settled merge status) and its approvals."""
        detail = self.t.get(f"{self.repo_path}/merge_requests/{iid}")
        approvals = self.t.get(f"{self.repo_path}/merge_requests/{iid}/approvals")
        if not isinstance(detail, dict):
            raise ForgeError(f"merge request !{iid} returned no object")
        return detail, approvals if isinstance(approvals, dict) else {}

    def open_changes(self) -> list[Change]:
        """Open MRs. The list has no head pipeline, so each MR is read once more, with its
        approvals for the review decision."""
        raw = self._open_mrs()
        details = self._each(lambda mr: self._detail(int(mr["iid"])), raw)
        return [_change(mr, detail, approvals) for mr, (detail, approvals) in zip(raw, details)]

    def _closes(self, iid: int) -> tuple[int, ...]:
        raw = self.t.get_all(f"{self.repo_path}/merge_requests/{iid}/closes_issues")
        return tuple(int(i["iid"]) for i in raw if self._other_project(i, "#") is None)

    def merged_since(self, since: datetime) -> list[Change]:
        """MRs merged at or after `since`, with the issues each closes (`closes_issues`)."""
        raw = self.t.get_all(
            f"{self.repo_path}/merge_requests",
            {"state": "merged", "updated_after": iso(since), "order_by": "updated_at"},
        )
        merged = [
            mr
            for mr in raw
            if (when := parse_time(mr.get("merged_at"))) is not None and when >= since
        ]
        closing = self._each(lambda mr: self._closes(int(mr["iid"])), merged)
        return [_change(mr, closing=c) for mr, c in zip(merged, closing)]

    # --- CI ------------------------------------------------------------------------------------

    def latest_run(self, branch: str, event: str) -> Optional[Run]:
        """The newest pipeline on `branch` from `event` (GitLab's `source`: push, schedule), with
        its failed jobs. One item, so there's no pagination to finish."""
        body = self.t.get(
            f"{self.repo_path}/pipelines",
            {"ref": branch, "source": event, "order_by": "id", "sort": "desc", "per_page": 1},
        )
        if not isinstance(body, list):
            raise ForgeError("pipelines returned something other than a list")
        if not body:
            return None
        run = _run(body[0])
        if run.conclusion in ("failure", "cancelled"):
            jobs = self.t.get_all(f"{self.repo_path}/pipelines/{run.id}/jobs")
            failed = tuple(
                j.get("name") or ""
                for j in jobs
                if j.get("status") in ("failed", "canceled") and not j.get("allow_failure")
            )
            run = replace(run, failed_jobs=failed)
        return run

    def passed_on_retry(self, branch: str, since: datetime) -> Counter:
        """Job name -> how many pipelines on `branch` updated since `since` it passed only on a
        retry: an earlier attempt failed and the newest one passed. GitLab keeps every attempt as
        its own job (`include_retried`), newest with the highest id."""
        pipelines = self.t.get_all(
            f"{self.repo_path}/pipelines", {"ref": branch, "updated_after": iso(since)}
        )

        def jobs(pipeline: dict) -> list[dict]:
            return self.t.get_all(
                f"{self.repo_path}/pipelines/{int(pipeline['id'])}/jobs",
                {"include_retried": "true"},
            )

        counts: Counter = Counter()
        for job_list in self._each(jobs, pipelines):
            by_name: dict[str, list[dict]] = {}
            for job in job_list:
                by_name.setdefault(job.get("name") or "", []).append(job)
            for name, attempts in by_name.items():
                attempts.sort(key=lambda j: int(j.get("id") or 0))
                failed_before = any(j.get("status") == "failed" for j in attempts[:-1])
                if failed_before and attempts[-1].get("status") == "success":
                    counts[name] += 1
        return counts

    # --- tags, branches, links -----------------------------------------------------------------

    def tags(self, patterns: tuple[str, ...]) -> list[Tag]:
        globs = [p for p in patterns if not p.startswith("registry:")]
        releases = self.t.get_all(f"{self.repo_path}/releases") if globs else []
        tags = self.t.get_all(f"{self.repo_path}/repository/tags") if globs else []
        dated = [
            (parse_time(r.get("released_at") or r.get("created_at")), r.get("tag_name") or "")
            for r in releases
            if not r.get("upcoming_release")
        ]
        return pick_tags(patterns, dated, [t.get("name") or "" for t in tags], "GitLab")

    def _compare(self, base: str, head: str) -> dict:
        compare = self.t.get(f"{self.repo_path}/repository/compare", {"from": base, "to": head})
        if not isinstance(compare, dict) or not isinstance(compare.get("commits"), list):
            raise ForgeError(f"compare {base}...{head} returned no commits")
        return compare

    def branches(self, default_branch: str, open_changes: list[Change]) -> list[Branch]:
        with_change = {c.branch for c in open_changes}
        raw = [
            b
            for b in self.t.get_all(f"{self.repo_path}/repository/branches")
            if b["name"] != default_branch
        ]

        def one(b: dict) -> Branch:
            name = b["name"]
            ahead = head_date = None
            if name not in with_change:
                commits = self._compare(default_branch, name)["commits"]
                ahead = len(commits)
                dates = [parse_time(c.get("committed_date")) for c in commits]
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
        """The active milestones. The number is the milestone's global id, which an issue
        update takes as `milestone_id`."""
        raw = self.t.get_all(f"{self.repo_path}/milestones", {"state": "active"})
        return [
            MilestoneRef(int(m["id"]), m.get("title") or "", m.get("due_date") or None) for m in raw
        ]

    def label_events(self, number: int) -> list[LabelEvent]:
        raw = self.t.get_all(f"{self.repo_path}/issues/{number}/resource_label_events")
        out = []
        for e in raw:
            action = {"add": "labeled", "remove": "unlabeled"}.get(e.get("action") or "")
            if action is None:
                continue
            out.append(
                LabelEvent(
                    event=action,
                    label=(e.get("label") or {}).get("name") or "",
                    created=parse_time(e["created_at"]),  # type: ignore[arg-type]
                )
            )
        return out

    def issue_comments(self, number: int) -> list[Comment]:
        raw = self.t.get_all(
            f"{self.repo_path}/issues/{number}/notes", {"sort": "asc", "order_by": "created_at"}
        )
        return [
            Comment(
                issue=number,
                author=_login(n.get("author")) or "",
                author_is_bot=_is_bot(n.get("author")),
                created=parse_time(n["created_at"]),  # type: ignore[arg-type]
                body=n.get("body") or "",
            )
            for n in raw
            if not n.get("system")
        ]

    def on_branch(self, branch: str, sha: str) -> bool:
        """Whether commit `sha` is reachable from `branch`: the branches that contain it."""
        raw = self.t.get_all(f"{self.repo_path}/repository/commits/{sha}/refs", {"type": "branch"})
        return any(r.get("name") == branch for r in raw)

    def issue_ref(self, number: int, project: Optional[str] = None) -> IssueRef:
        other = project if project and project != self.project else None
        raw = self.t.get(f"{project_path(other or self.project)}/issues/{number}")
        if not isinstance(raw, dict) or "state" not in raw:
            raise ForgeError(f"{other or ''}#{number} returned no issue")
        return IssueRef(
            number=number,
            state=_state(raw.get("state")),
            is_change=False,
            id=int(raw["id"]) if raw.get("id") else None,
            project=other,
            closed=parse_time(raw.get("closed_at")),
        )

    def _links(self, number: int, labels: tuple[str, ...]) -> list[Link]:
        """What blocks one issue: `is_blocked_by` links, and under the convention its `relates_to`
        links when it carries the blocked label."""
        convention = self.convention and self.blocked_label in labels
        out = []
        for raw in self.t.get_all(f"{self.repo_path}/issues/{number}/links"):
            kind = {"is_blocked_by": "native", "relates_to": "convention"}.get(
                raw.get("link_type") or ""
            )
            if kind is None or (kind == "convention" and not convention):
                continue
            out.append(
                Link(
                    blocked=number,
                    blocker=int(raw["iid"]),
                    blocker_open=_state(raw.get("state")) == "open",
                    kind=kind,
                    blocker_project=self._other_project(raw, "#"),
                    blocker_closed=parse_time(raw.get("closed_at")),
                )
            )
        return out

    def blocked_by(self, number: int) -> list[Link]:
        labels: tuple[str, ...] = ()
        if self.convention:
            raw = self.t.get(f"{self.repo_path}/issues/{number}")
            labels = _labels((raw or {}).get("labels") or []) if isinstance(raw, dict) else ()
        return self._links(number, labels)

    def blocking_links(self) -> list[Link]:
        """Every open issue's blockers, one links read per issue."""
        issues = self.open_issues()
        per_issue = self._each(lambda i: self._links(i.number, i.labels), issues)
        return [link for links in per_issue for link in links]

    # --- /next: the fingerprint and closing references -----------------------------------------

    def fingerprint_inputs(self) -> dict[str, Any]:
        """The reads `/next` fingerprints its facts with, in the same shape as GitHub's. The MR
        list has no head pipeline, so each open MR's head commit and pipeline status come from
        its own read: one request per open MR on top of the five lists."""
        project = self.t.get(self.repo_path)
        if not isinstance(project, dict) or not project.get("default_branch"):
            raise ForgeError(f"{self.project} returned no default branch for the fingerprint")
        default = project["default_branch"]
        heads = {
            b["name"]: (b.get("commit") or {}).get("id")
            for b in self.t.get_all(f"{self.repo_path}/repository/branches")
        }

        def newest(path: str, params: dict[str, Any]) -> Optional[str]:
            body = self.t.get(
                f"{self.repo_path}/{path}",
                dict(params, order_by="updated_at", sort="desc", per_page=1),
            )
            if not isinstance(body, list):
                raise ForgeError(f"{path} returned something other than a list")
            return body[0].get("updated_at") if body else None

        milestones = self.t.get_all(f"{self.repo_path}/milestones")
        open_mrs = self._open_mrs()
        details = self._each(
            lambda mr: self.t.get(f"{self.repo_path}/merge_requests/{int(mr['iid'])}"), open_mrs
        )
        open_issue_count = project.get("open_issues_count")
        if open_issue_count is None:
            open_issue_count = len(self.open_issues())
        return {
            "default_branch": default,
            "default_head": heads.get(default),
            "open_issues": open_issue_count,
            "open_changes": len(open_mrs),
            "newest_issue": newest("issues", {}),
            "newest_change": newest("merge_requests", {"state": "all"}),
            "milestones": sorted(
                [
                    str(m.get("id") or ""),
                    m.get("title") or "",
                    m.get("due_date") or "",
                    m.get("state") or "",
                ]
                for m in milestones
            ),
            "branch_heads": heads,
            "change_heads": {
                str(mr["iid"]): [
                    (detail or {}).get("sha") or mr.get("sha"),
                    pipeline_state((detail or {}).get("head_pipeline")) or "none",
                ]
                for mr, detail in zip(open_mrs, details)
            },
        }

    # --- /groom: possibly done -----------------------------------------------------------------

    file_patches = True

    def cross_refs(self) -> list[CrossRef]:
        """Every MR that mentions an open issue in any form: the issue's related merge requests
        (`GET /issues/:iid/related_merge_requests`, the list behind its "mentioned in merge
        request" notes), one read per open issue. GitLab's list doesn't say which mentions are
        closing references; `closed_by` does, and /groom reads that separately."""
        issues = self.open_issues()
        per_issue = self._each(
            lambda i: self.t.get_all(f"{self.repo_path}/issues/{i.number}/related_merge_requests"),
            issues,
        )
        out = []
        for issue, mrs in zip(issues, per_issue):
            for mr in mrs:
                state = (mr.get("state") or "").lower()
                out.append(
                    CrossRef(
                        issue=issue.number,
                        change=int(mr["iid"]),
                        state="open" if state == "opened" else state,
                        merged_at=parse_time(mr.get("merged_at")),
                        title=mr.get("title") or "",
                        base=mr.get("target_branch") or None,
                        change_project=self._other_project(mr, "!"),
                    )
                )
        return out

    def change_files(self, number: int) -> list[ChangedFile]:
        """The files one MR changed, with each file's diff (`GET /merge_requests/:iid/diffs`,
        every page; GitLab 15.7 and later). Unsupported on an instance without that endpoint."""
        try:
            raw = self.t.get_all(f"{self.repo_path}/merge_requests/{number}/diffs")
        except HTTPStatusError as error:
            if error.status == 404 and isinstance(self.t.get(self.repo_path), dict):
                raise Unsupported(
                    "this instance has no merge request diffs API (GitLab 15.7 and later)"
                ) from None
            raise
        return [
            ChangedFile(
                path=item.get("new_path") or item.get("old_path") or "",
                patch=item.get("diff") or None,
                previous_path=item.get("old_path")
                if item.get("old_path") != item.get("new_path")
                else None,
            )
            for item in raw
        ]

    def closing_refs(self) -> list[ClosingRef]:
        """Every MR, open, merged or closed, that closes an open issue when merged
        (`GET /issues/:iid/closed_by`), one read per open issue."""
        issues = self.open_issues()
        per_issue = self._each(
            lambda i: self.t.get_all(f"{self.repo_path}/issues/{i.number}/closed_by"), issues
        )
        out = []
        for issue, mrs in zip(issues, per_issue):
            for mr in mrs:
                state = (mr.get("state") or "").lower()
                out.append(
                    ClosingRef(
                        issue=issue.number,
                        change=int(mr["iid"]),
                        state="open" if state == "opened" else state,
                        branch=mr.get("source_branch") or "",
                        base=mr.get("target_branch") or "",
                        merged_at=parse_time(mr.get("merged_at")),
                        change_project=self._other_project(mr, "!"),
                        title=mr.get("title") or "",
                        merge_sha=mr.get("merge_commit_sha") or mr.get("squash_commit_sha"),
                    )
                )
        return out
