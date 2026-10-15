"""The GitHub adapter: REST for what REST answers in one list, GraphQL where REST would need a
call per item (mergeability, closing references, blocking links). Every method raises ForgeError
on failure; callers wrap each one in `attempt` so it fails on its own."""

from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

from ..model import (
    Branch,
    Change,
    ClosingRef,
    Comment,
    ForgeError,
    Issue,
    Job,
    Link,
    Mention,
    Milestone,
    Repo,
    Run,
    Tag,
    User,
    iso,
    parse_time,
)
from ..transport import ReadTransport

API_VERSION = "2022-11-28"


def endpoints(host: str, api_base: Optional[str] = None) -> tuple[str, str]:
    """REST base and GraphQL URL. github.com uses api.github.com; GitHub Enterprise Server uses
    /api/v3 and /api/graphql on its own host, unless `api_base` says otherwise."""
    if api_base:
        base = api_base.rstrip("/")
        root = base.removesuffix("/v3")
        return base, root + "/graphql"
    if host == "github.com":
        return "https://api.github.com", "https://api.github.com/graphql"
    return f"https://{host}/api/v3", f"https://{host}/api/graphql"


def headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}


def _is_bot(user: Optional[dict]) -> bool:
    if not user:
        return False
    login = user.get("login") or ""
    return user.get("type") == "Bot" or user.get("__typename") == "Bot" or login.endswith("[bot]")


def conflicts(mergeable: Any) -> str:
    """GitHub's mergeability as yes/no/unknown. GraphQL says CONFLICTING, MERGEABLE or UNKNOWN;
    REST says false, true or null (still computing). Anything else is unknown, never clean."""
    if mergeable in ("CONFLICTING", False):
        return "yes"
    if mergeable in ("MERGEABLE", True):
        return "no"
    return "unknown"


def _issue(raw: dict) -> Issue:
    milestone = raw.get("milestone")
    return Issue(
        number=int(raw["number"]),
        title=raw.get("title") or "",
        state=raw.get("state") or "open",
        labels=tuple(label["name"] for label in raw.get("labels") or []),
        assignees=tuple(a["login"] for a in raw.get("assignees") or []),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        updated=parse_time(raw["updated_at"]),  # type: ignore[arg-type]
        closed=parse_time(raw.get("closed_at")),
        milestone=Milestone(milestone["title"], (milestone.get("due_on") or None))
        if milestone
        else None,
        author=(raw.get("user") or {}).get("login"),
        url=raw.get("html_url") or "",
        body=raw.get("body") or "",
    )


_CHANGE_FIELDS = """
  number title url headRefName isDraft mergeable createdAt updatedAt mergedAt
  author { __typename login }
  mergeCommit { oid }
  assignees(first: 50) { nodes { login } }
  reviewRequests(first: 50) {
    nodes { requestedReviewer { __typename ... on User { login } ... on Bot { login }
                                ... on Team { slug } } }
  }
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
"""

OPEN_CHANGES = (
    """
query OpenChanges($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + _CHANGE_FIELDS
    + """}
    }
  }
}
"""
)

MERGED_CHANGES = """
query MergedChanges($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: 50, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url headRefName isDraft createdAt updatedAt mergedAt
        author { __typename login }
        mergeCommit { oid }
        closingIssuesReferences(first: 25) { nodes { number repository { nameWithOwner } } }
      }
    }
  }
}
"""

BLOCKING_LINKS = """
query BlockingLinks($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        blockedBy(first: 50) {
          nodes { number state repository { nameWithOwner } }
        }
      }
    }
  }
}
"""


def _reviewers(node: dict) -> tuple[str, ...]:
    out = []
    for request in (node.get("reviewRequests") or {}).get("nodes") or []:
        reviewer = request.get("requestedReviewer") or {}
        name = reviewer.get("login") or (
            f"team:{reviewer['slug']}" if reviewer.get("slug") else None
        )
        if name:
            out.append(name)
    return tuple(out)


def _change(node: dict, owner_repo: str) -> Change:
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
    closing = []
    for ref in (node.get("closingIssuesReferences") or {}).get("nodes") or []:
        if (ref.get("repository") or {}).get("nameWithOwner", owner_repo) == owner_repo:
            closing.append(int(ref["number"]))
    return Change(
        number=int(node["number"]),
        title=node.get("title") or "",
        branch=node.get("headRefName") or "",
        draft=bool(node.get("isDraft")),
        author=(node.get("author") or {}).get("login"),
        assignees=tuple(a["login"] for a in (node.get("assignees") or {}).get("nodes") or []),
        reviewers=_reviewers(node),
        conflicts=conflicts(node.get("mergeable")),
        pipeline=(rollup.get("state") or "").lower() or None,
        created=parse_time(node["createdAt"]),  # type: ignore[arg-type]
        updated=parse_time(node["updatedAt"]),  # type: ignore[arg-type]
        url=node.get("url") or "",
        merged_at=parse_time(node.get("mergedAt")),
        merge_sha=(node.get("mergeCommit") or {}).get("oid"),
        closing_issues=tuple(closing),
    )


def _run(raw: dict) -> Run:
    return Run(
        id=int(raw["id"]),
        name=raw.get("name") or "",
        event=raw.get("event") or "",
        status=raw.get("status") or "",
        conclusion=raw.get("conclusion"),
        attempt=int(raw.get("run_attempt") or 1),
        created=parse_time(raw["created_at"]),  # type: ignore[arg-type]
        url=raw.get("html_url") or "",
    )


class GitHub:
    forge = "github"

    def __init__(self, transport: ReadTransport, project: str):
        owner, _, name = project.partition("/")
        if not owner or not name or "/" in name:
            raise ForgeError(f"{project!r} isn't owner/repo")
        self.t = transport
        self.owner, self.name = owner, name
        self.project = project
        self.repo_path = f"/repos/{owner}/{name}"

    def ref(self, number: int, change: bool = False) -> str:
        return f"#{number}"

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

    # --- issues and comments -------------------------------------------------------------------

    def issues_since(self, since: datetime) -> list[Issue]:
        """Issues (not PRs) of any state updated at or after `since`."""
        raw = self.t.get_all(
            f"{self.repo_path}/issues",
            {"state": "all", "since": iso(since), "sort": "updated", "direction": "desc"},
        )
        return [_issue(item) for item in raw if "pull_request" not in item]

    def open_issues(self) -> list[Issue]:
        raw = self.t.get_all(f"{self.repo_path}/issues", {"state": "open"})
        return [_issue(item) for item in raw if "pull_request" not in item]

    def comments_since(self, since: datetime) -> list[Comment]:
        """Comments created at or after `since`, repo-wide. GitHub's `since` filters on update
        time, so edited older comments are dropped here."""
        raw = self.t.get_all(
            f"{self.repo_path}/issues/comments",
            {"since": iso(since), "sort": "created", "direction": "asc"},
        )
        out = []
        for item in raw:
            created = parse_time(item["created_at"])
            if created is None or created < since:
                continue
            number = int(str(item.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1])
            out.append(
                Comment(
                    issue=number,
                    author=(item.get("user") or {}).get("login") or "",
                    author_is_bot=_is_bot(item.get("user")),
                    created=created,
                    body=item.get("body") or "",
                    url=item.get("html_url") or "",
                )
            )
        return out

    def mentions_since(self, login: str, since: datetime) -> list[Mention]:
        query = f"repo:{self.project} is:open mentions:{login} updated:>={iso(since)}"
        raw = self.t.get_all("/search/issues", {"q": query}, key="items")
        return [
            Mention(
                number=int(item["number"]),
                title=item.get("title") or "",
                is_change="pull_request" in item,
                updated=parse_time(item["updated_at"]),  # type: ignore[arg-type]
                url=item.get("html_url") or "",
            )
            for item in raw
        ]

    # --- changes -------------------------------------------------------------------------------

    def open_changes(self) -> list[Change]:
        nodes = self.t.graphql_all(
            OPEN_CHANGES,
            {"owner": self.owner, "name": self.name},
            ("repository", "pullRequests"),
        )
        return [_change(node, self.project) for node in nodes]

    def merged_since(self, since: datetime) -> list[Change]:
        """Changes merged at or after `since`. The list is newest-updated first, and a change
        updated before `since` can't have merged after it, so the read stops there."""
        nodes = self.t.graphql_all(
            MERGED_CHANGES,
            {"owner": self.owner, "name": self.name},
            ("repository", "pullRequests"),
            stop=lambda node: parse_time(node["updatedAt"]) < since,
        )
        out = [_change(node, self.project) for node in nodes]
        return [c for c in out if c.merged_at is not None and c.merged_at >= since]

    # --- CI ------------------------------------------------------------------------------------

    def latest_run(self, branch: str, event: str) -> Optional[Run]:
        """The newest run on `branch` for `event`, with its failed jobs. One item, not a list,
        so there's no pagination to finish."""
        body = self.t.get(
            f"{self.repo_path}/actions/runs",
            {"branch": branch, "event": event, "per_page": 1},
        )
        if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
            raise ForgeError("actions/runs returned no workflow_runs list")
        if not body["workflow_runs"]:
            return None
        run = _run(body["workflow_runs"][0])
        if run.conclusion in ("failure", "timed_out", "startup_failure", "cancelled"):
            jobs = self.jobs(run.id, "latest")
            failed = tuple(
                j.name for j in jobs if j.conclusion in ("failure", "timed_out", "cancelled")
            )
            run = replace(run, failed_jobs=failed)
        return run

    def jobs(self, run_id: int, which: str = "latest") -> list[Job]:
        raw = self.t.get_all(
            f"{self.repo_path}/actions/runs/{run_id}/jobs", {"filter": which}, key="jobs"
        )
        return [
            Job(
                name=j.get("name") or "",
                conclusion=j.get("conclusion"),
                attempt=int(j.get("run_attempt") or 1),
            )
            for j in raw
        ]

    def passed_on_retry(self, branch: str, since: datetime) -> Counter:
        """Job name -> how many runs on `branch` created since `since` it passed only on a retry
        (a failed attempt, then a passing one in the same run)."""
        runs = self.t.get_all(
            f"{self.repo_path}/actions/runs",
            {"branch": branch, "created": f">={iso(since)}"},
            key="workflow_runs",
        )
        counts: Counter = Counter()
        for raw in runs:
            if int(raw.get("run_attempt") or 1) < 2:
                continue
            by_name: dict[str, list[Job]] = {}
            for job in self.jobs(int(raw["id"]), "all"):
                by_name.setdefault(job.name, []).append(job)
            for name, attempts in by_name.items():
                attempts.sort(key=lambda j: j.attempt)
                failed_before = any(j.conclusion == "failure" for j in attempts[:-1])
                if failed_before and attempts[-1].conclusion == "success":
                    counts[name] += 1
        return counts

    # --- tags, branches, links -----------------------------------------------------------------

    def tags(self, patterns: tuple[str, ...]) -> list[Tag]:
        """The newest release or tag matching each glob. `registry:` entries aren't read on
        GitHub yet and come back with a note, not a failure."""
        globs = [p for p in patterns if not p.startswith("registry:")]
        releases = self.t.get_all(f"{self.repo_path}/releases") if globs else []
        tags = self.t.get_all(f"{self.repo_path}/tags") if globs else []
        out = []
        for pattern in patterns:
            if pattern.startswith("registry:"):
                out.append(Tag(pattern, None, note="registry tags aren't read on GitHub yet"))
                continue
            dated = [
                (parse_time(r.get("published_at") or r.get("created_at")), r["tag_name"])
                for r in releases
                if not r.get("draft") and fnmatch.fnmatchcase(r.get("tag_name") or "", pattern)
            ]
            dated = [d for d in dated if d[0] is not None]
            if dated:
                when, name = max(dated)
                out.append(Tag(pattern, name, when))
                continue
            names = [t["name"] for t in tags if fnmatch.fnmatchcase(t.get("name") or "", pattern)]
            out.append(Tag(pattern, names[0] if names else None))
        return out

    def branches(self, default_branch: str, open_changes: list[Change]) -> list[Branch]:
        with_change = {c.branch for c in open_changes}
        out = []
        for raw in self.t.get_all(f"{self.repo_path}/branches"):
            name = raw["name"]
            if name == default_branch:
                continue
            ahead = None
            if name not in with_change:
                compare = self.t.get(f"{self.repo_path}/compare/{default_branch}...{name}")
                ahead = int(compare.get("ahead_by", 0)) if isinstance(compare, dict) else None
            out.append(Branch(name, raw["commit"]["sha"], name in with_change, ahead))
        return out

    def blocking_links(self) -> list[Link]:
        nodes = self.t.graphql_all(
            BLOCKING_LINKS,
            {"owner": self.owner, "name": self.name},
            ("repository", "issues"),
        )
        out = []
        for node in nodes:
            for blocker in (node.get("blockedBy") or {}).get("nodes") or []:
                project = (blocker.get("repository") or {}).get("nameWithOwner")
                out.append(
                    Link(
                        blocked=int(node["number"]),
                        blocker=int(blocker["number"]),
                        blocker_open=blocker.get("state") == "OPEN",
                        kind="native",
                        blocker_project=None if project == self.project else project,
                    )
                )
        return out

    # --- /next: the fingerprint and closing references -----------------------------------------

    def fingerprint_inputs(self) -> dict[str, Any]:
        """The cheap reads `/next` fingerprints its facts with: the default branch and its head,
        open issue and change counts, the newest issue, change and Actions run update times, every
        milestone (id, title, due, state) and every branch head. Four requests on a small repo."""
        data = self.t.graphql(NEXT_FINGERPRINT, {"owner": self.owner, "name": self.name})
        repo = (data or {}).get("repository")
        if not isinstance(repo, dict):
            raise ForgeError("GraphQL returned no repository for the fingerprint")
        default = repo.get("defaultBranchRef") or {}
        if not default.get("name"):
            raise ForgeError("GraphQL returned no default branch for the fingerprint")

        def newest(key: str) -> Optional[str]:
            nodes = (repo.get(key) or {}).get("nodes") or []
            return nodes[0].get("updatedAt") if nodes else None

        milestones = self.t.graphql_all(
            MILESTONES, {"owner": self.owner, "name": self.name}, ("repository", "milestones")
        )
        heads = self.t.graphql_all(
            BRANCH_HEADS, {"owner": self.owner, "name": self.name}, ("repository", "refs")
        )
        runs = self.t.get(f"{self.repo_path}/actions/runs", {"per_page": 1})
        if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
            raise ForgeError("actions/runs returned no workflow_runs list")
        newest_run = runs["workflow_runs"][0] if runs["workflow_runs"] else {}
        return {
            "default_branch": default["name"],
            "default_head": (default.get("target") or {}).get("oid"),
            "open_issues": (repo.get("openIssues") or {}).get("totalCount"),
            "open_changes": (repo.get("openChanges") or {}).get("totalCount"),
            "newest_issue": newest("newestIssue"),
            "newest_change": newest("newestChange"),
            "newest_pipeline": newest_run.get("updated_at"),
            "milestones": sorted(
                [
                    m.get("id") or "",
                    m.get("title") or "",
                    m.get("dueOn") or "",
                    m.get("state") or "",
                ]
                for m in milestones
            ),
            "branch_heads": {
                h["name"]: (h.get("target") or {}).get("oid") for h in heads if h.get("name")
            },
        }

    def closing_refs(self) -> list[ClosingRef]:
        """Every change, open, merged or closed, with a real closing reference to an open issue:
        GitHub's `closedByPullRequestsReferences`, the link that closes the issue on merge. A
        `#N` in a title or branch isn't one."""
        nodes = self.t.graphql_all(
            CLOSING_REFS, {"owner": self.owner, "name": self.name}, ("repository", "issues")
        )
        out = []
        for node in nodes:
            refs = node.get("closedByPullRequestsReferences") or {}
            listed = refs.get("nodes") or []
            if int(refs.get("totalCount") or 0) > len(listed):
                raise ForgeError(
                    f"#{node['number']} has more than {len(listed)} closing references"
                )
            for change in listed:
                project = (change.get("repository") or {}).get("nameWithOwner")
                out.append(
                    ClosingRef(
                        issue=int(node["number"]),
                        change=int(change["number"]),
                        state=(change.get("state") or "").lower(),
                        branch=change.get("headRefName") or "",
                        base=change.get("baseRefName") or "",
                        merged_at=parse_time(change.get("mergedAt")),
                        change_project=None if project in (None, self.project) else project,
                    )
                )
        return out


NEXT_FINGERPRINT = """
query NextFingerprint($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name target { oid } }
    openIssues: issues(states: OPEN) { totalCount }
    openChanges: pullRequests(states: OPEN) { totalCount }
    newestIssue: issues(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { updatedAt }
    }
    newestChange: pullRequests(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { updatedAt }
    }
  }
}
"""

MILESTONES = """
query Milestones($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    milestones(first: 100, after: $cursor, states: [OPEN, CLOSED]) {
      pageInfo { hasNextPage endCursor }
      nodes { id title dueOn state }
    }
  }
}
"""

BRANCH_HEADS = """
query BranchHeads($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { name target { oid } }
    }
  }
}
"""

CLOSING_REFS = """
query ClosingRefs($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        closedByPullRequestsReferences(first: 25, includeClosedPrs: true) {
          totalCount
          nodes {
            number state mergedAt headRefName baseRefName
            repository { nameWithOwner }
          }
        }
      }
    }
  }
}
"""
