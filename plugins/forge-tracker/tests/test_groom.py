"""/groom: the checks, the unclear bucket, and applying approved proposals through the write
layer. Every forge here is fake; nothing in these tests can reach a real one."""

import io
import json
import os
import re
import stat
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import FIXTURES, TOKEN, FakeRun, ReplayHttp, World, response
from forge_tracker import cli
from forge_tracker.model import ForgeError, parse_time

NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)

CONFIG = """---
labels:
  needs_human: needs-human
  decision: decision
  decision_made: decided
  blocked: blocked
tiers:
  labels: [P1, P2]
  milestone_prefixes: ["Tier 1", "Tier 2"]
ruling_pattern: '^Ruling \\((\\d{4}-\\d{2}-\\d{2})\\):'
stale_days: 10
---
"""

TIER1 = {"number": 3, "title": "Tier 1 October", "due_on": "2026-10-31T07:00:00Z"}
TIER2 = {"number": 4, "title": "Tier 2 November", "due_on": "2026-11-30T07:00:00Z"}
SOMEDAY = {"number": 5, "title": "Someday", "due_on": "2026-12-31T08:00:00Z"}
LATER = {"number": 6, "title": "Later", "due_on": "2026-12-31T08:00:00Z"}


def issue(number, title, labels=(), **kw):
    base = {
        "number": number,
        "title": title,
        "state": "open",
        "labels": [{"name": n} for n in labels],
        "assignees": [],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "closed_at": None,
        "milestone": None,
        "user": {"login": "someone", "type": "User"},
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "body": "",
    }
    base.update(kw)
    return base


def conn(nodes, key):
    return {
        "repository": {key: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
    }


def open_pr(number, branch, mergeable="MERGEABLE", updated="2026-10-14T00:00:00Z", **kw):
    node = {
        "number": number,
        "title": f"change {number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "headRefName": branch,
        "isDraft": False,
        "mergeable": mergeable,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": updated,
        "mergedAt": None,
        "author": {"__typename": "User", "login": "me"},
        "mergeCommit": None,
        "assignees": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "h", "statusCheckRollup": None}}]},
    }
    node.update(kw)
    return node


def merged_pr(number, branch, title, closes=()):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "headRefName": branch,
        "isDraft": False,
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-10T00:00:00Z",
        "mergedAt": "2026-10-10T00:00:00Z",
        "author": {"__typename": "User", "login": "me"},
        "mergeCommit": {"oid": f"m{number}"},
        "closingIssuesReferences": {
            "nodes": [
                {"number": n, "repository": {"nameWithOwner": "acme/widgets"}} for n in closes
            ]
        },
    }


def closing(number, change, base, sha, title):
    return {
        "number": number,
        "closedByPullRequestsReferences": {
            "totalCount": 1,
            "nodes": [
                {
                    "number": change,
                    "title": title,
                    "state": "MERGED",
                    "mergedAt": "2026-10-10T00:00:00Z",
                    "headRefName": f"fix/{number}",
                    "baseRefName": base,
                    "mergeCommit": {"oid": sha},
                    "repository": {"nameWithOwner": "acme/widgets"},
                }
            ],
        },
    }


def event(kind, label, when):
    return {"event": kind, "label": {"name": label}, "created_at": when}


def comment(body, when):
    return {
        "body": body,
        "created_at": when,
        "user": {"login": "me", "type": "User"},
        "html_url": "https://github.com/acme/widgets/issues/1#c",
    }


class Repo:
    """A fake forge with one of everything /groom looks for."""

    def __init__(self):
        self.broken: set[str] = set()
        self.issues = [
            issue(14, "merged but open"),
            issue(15, "merged into dev"),
            issue(16, "merge commit not on main"),
            issue(18, "footer width"),
            issue(20, "tier label, no milestone", ["P1"]),
            issue(21, "milestone without its label", milestone=TIER2),
            issue(22, "label and milestone disagree", ["P1"], milestone=TIER2),
            issue(25, "blocked by label only", ["blocked"]),
            issue(26, "stale link"),
            issue(27, "ruled on", ["needs-human"]),
            issue(28, "ruling before the label", ["decision"]),
            issue(40, "deferred TaskUpdate loading fails"),
            issue(41, "TaskUpdate deferred loading fails"),
            issue(45, "someday one", milestone=SOMEDAY),
            issue(46, "later one", milestone=LATER),
        ]
        self.recent = self.issues + [
            issue(
                50,
                "an unrelated closed thing",
                state="closed",
                closed_at="2026-10-01T00:00:00Z",
            )
        ]

    def _maybe(self, name, data):
        if name in self.broken:
            return {"errors": [{"message": f"{name} broke with {TOKEN}"}]}
        return data

    def _rest(self, name, body):
        if name in self.broken:
            return response({"message": f"{name} broke"}, status=502)
        return response(body)

    def world(self):
        w = World()
        w.route("/repos/acme/widgets", {"default_branch": "main"})
        w.route(
            "/repos/acme/widgets/issues",
            lambda url: self._rest("issues", self.issues if "state=open" in url else self.recent),
        )
        w.route("/repos/acme/widgets/milestones", [TIER1, TIER2, SOMEDAY, LATER])
        w.op(
            "BlockingLinks",
            lambda v: self._maybe(
                "links",
                conn(
                    [
                        {
                            "number": 26,
                            "blockedBy": {
                                "nodes": [
                                    {
                                        "number": 19,
                                        "state": "CLOSED",
                                        "closedAt": "2026-10-09T00:00:00Z",
                                        "repository": {"nameWithOwner": "acme/widgets"},
                                    }
                                ]
                            },
                        }
                    ],
                    "issues",
                ),
            ),
        )
        w.op(
            "OpenChanges",
            lambda v: self._maybe(
                "changes",
                conn(
                    [
                        open_pr(60, "feat/x", "UNKNOWN"),
                        open_pr(61, "feat/y", "CONFLICTING"),
                        open_pr(62, "feat/z", updated="2026-09-01T00:00:00Z"),
                    ],
                    "pullRequests",
                ),
            ),
        )
        w.op(
            "MergedChanges",
            lambda v: self._maybe(
                "merged",
                conn(
                    [
                        merged_pr(31, "fix/14", "fix: the thing", closes=[14]),
                        merged_pr(29, "18-footer", "fix: footer width"),
                    ],
                    "pullRequests",
                ),
            ),
        )
        w.op(
            "ClosingRefs",
            lambda v: self._maybe(
                "closing",
                conn(
                    [
                        closing(14, 31, "main", "aaa1111", "fix: the thing"),
                        closing(15, 32, "dev", "ccc3333", "fix: on dev"),
                        closing(16, 33, "main", "bbb2222", "fix: elsewhere"),
                    ],
                    "issues",
                ),
            ),
        )
        w.route("/repos/acme/widgets/compare/main...aaa1111", {"status": "behind"})
        w.route("/repos/acme/widgets/compare/main...bbb2222", {"status": "diverged"})
        w.route(
            "/repos/acme/widgets/branches",
            lambda url: self._rest(
                "branches",
                [
                    {"name": "main", "commit": {"sha": "m"}, "protected": True},
                    {"name": "feat/x", "commit": {"sha": "x"}},
                    {"name": "feat/y", "commit": {"sha": "y"}},
                    {"name": "feat/z", "commit": {"sha": "z"}},
                    {"name": "old-1", "commit": {"sha": "d"}},
                    {"name": "release", "commit": {"sha": "r"}, "protected": True},
                    {"name": "wip-idle", "commit": {"sha": "i"}},
                    {"name": "wip-fresh", "commit": {"sha": "f"}},
                    # Bot branches, merged and reused: never proposed.
                    {"name": "yeet/release-main", "commit": {"sha": "y1"}},
                    {"name": "dependabot/npm_and_yarn/x-1.2.3", "commit": {"sha": "dp"}},
                    {"name": "release-please--branches--main", "commit": {"sha": "rp"}},
                    {"name": "changeset-release/main", "commit": {"sha": "cs"}},
                    {"name": "renovate/x", "commit": {"sha": "rn"}},
                    {"name": "keep/this", "commit": {"sha": "k"}},
                ],
            ),
        )
        for name in (
            "yeet/release-main",
            "dependabot/npm_and_yarn/x-1.2.3",
            "release-please--branches--main",
            "changeset-release/main",
            "renovate/x",
            "keep/this",
        ):
            w.route(f"/repos/acme/widgets/compare/main...{name}", {"ahead_by": 0, "commits": []})
        w.route("/repos/acme/widgets/compare/main...old-1", {"ahead_by": 0, "commits": []})
        w.route("/repos/acme/widgets/compare/main...release", {"ahead_by": 0, "commits": []})
        w.route(
            "/repos/acme/widgets/compare/main...wip-idle",
            {
                "ahead_by": 2,
                "commits": [
                    {"commit": {"committer": {"date": "2026-08-30T00:00:00Z"}}},
                    {"commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}},
                ],
            },
        )
        w.route(
            "/repos/acme/widgets/compare/main...wip-fresh",
            {
                "ahead_by": 1,
                "commits": [{"commit": {"committer": {"date": "2026-10-14T00:00:00Z"}}}],
            },
        )
        w.route(
            "/repos/acme/widgets/issues/27/events",
            [
                event("labeled", "P1", "2026-10-01T00:00:00Z"),
                event("labeled", "needs-human", "2026-10-12T00:00:00Z"),
            ],
        )
        w.route(
            "/repos/acme/widgets/issues/27/comments",
            [
                comment("Ruling (2026-10-10): too early", "2026-10-10T00:00:00Z"),
                comment("thinking", "2026-10-13T00:00:00Z"),
                comment("Ruling (2026-10-14): ship the small one\nmore", "2026-10-14T00:00:00Z"),
            ],
        )
        w.route(
            "/repos/acme/widgets/issues/28/events",
            [event("labeled", "decision", "2026-10-13T00:00:00Z")],
        )
        w.route(
            "/repos/acme/widgets/issues/28/comments",
            [comment("Ruling (2026-10-10): before the label", "2026-10-10T00:00:00Z")],
        )
        return w


class Forge:
    """The fake world for reads plus a recorder for writes. Writes never reach the World; each
    is answered from `answers` by (method, path), or 200 with an empty object."""

    def __init__(self, world):
        self.world = world
        self.writes = []
        self.answers = {}
        self.issue_state = {14: "open"}
        self.branch_heads = {"old-1": "d"}

    def __call__(self, method, url, headers, body=None):
        path = urllib.parse.urlsplit(url).path
        if method in ("POST", "PATCH", "DELETE") and path != "/graphql":
            self.writes.append((method, path, json.loads(body) if body else None))
            status, payload = self.answers.get((method, path), (200, {}))
            return response(payload, status)
        if method == "GET" and path.startswith("/repos/acme/widgets/issues/"):
            tail = path.rsplit("/", 1)[-1]
            if tail.isdigit():
                number = int(tail)
                state = "closed" if number == 19 else self.issue_state.get(number, "open")
                return response({"id": 9000 + number, "number": number, "state": state})
        if method == "GET" and path.startswith("/repos/acme/widgets/branches/"):
            name = path[len("/repos/acme/widgets/branches/") :]
            return response({"name": name, "commit": {"sha": self.branch_heads.get(name, "?")}})
        return self.world(method, url, headers, body)


@pytest.fixture
def env(tmp_path, repo_dir):
    return {
        "repo": repo_dir(CONFIG),
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")},
        "log": tmp_path / "cache" / "forge-tracker" / "writes" / "github.com_acme__widgets.jsonl",
    }


def run_cli(env, *args, http=None, run=None, stdin=None):
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], *args],
        http=http if http is not None else Repo().world(),
        run=run or FakeRun(),
        now=NOW,
        environ=env["environ"],
        out=out,
        tz=timezone.utc,
        stdin=io.StringIO(stdin) if stdin is not None else None,
    )
    return code, out.getvalue()


EXPECTED = """\
acme/widgets on github.com: 8 proposals, 7 unclear, 3 listed for you
(merged PRs since 2026-07-17; duplicates among open issues and those closed since 2026-09-15; stale after 10 days)

Proposals
  1. close #14: merged #31 "fix: the thing" closes it, aaa1111 is on main
  2. milestone #20: has P1 and no milestone; 'Tier 1 October' is the only open 'Tier 1' milestone
  3. labels #21 tier: in milestone 'Tier 2 November' without the P2 label
  4. unlink #25 label: the blocked label with no open blocker linked
  5. unlink #26 from #19: blocked by #19, closed 2026-10-09
  6. labels #27 ruling: remove needs-human, add decided: 'Ruling (2026-10-14): ship the small one' on 2026-10-14, after needs-human was added on 2026-10-12
  7. delete-branch keep/this: no open PR, 0 commits ahead of main
  8. delete-branch old-1: no open PR, 0 commits ahead of main
Unclear (not acted on)
  #15 has a closing reference from #32, which merged into dev, not main, so the forge didn't close it
  #16 has a closing reference from merged #33, but bbb2222 isn't on main
  #18 named in the branch 18-footer of merged #29, no closing reference
  #22 has P1 but milestone 'Tier 2 November' (Tier 2); which one is right isn't something the tracker says
  branch wip-idle: no open PR, 2 commits not on main, last commit 2026-09-01
  #60 conflicts: unknown (GitHub hasn't computed it)
  #40 and #41 may be duplicates (1.00): 'deferred TaskUpdate loading fails' / 'TaskUpdate deferred loading fails'
Listed for you
  #61 conflicts: yes: change 61
  #62 no activity for 44 days: change 62
  2 issues share due date 2026-12-31 across 2 milestones ('Later', 'Someday')
"""


def test_report(env):
    code, text = run_cli(env, "groom")
    assert code == 0
    assert text == EXPECTED


def test_groom_reads_only(env):
    world = Repo().world()
    run_cli(env, "groom", http=world)
    for method, url, _headers, body in world.calls:
        assert method == "GET" or (method == "POST" and url == "https://api.github.com/graphql"), (
            method,
            url,
        )
        if body:
            assert "mutation" not in json.loads(body)["query"]


def test_json_carries_each_proposal_with_its_write_payload(env):
    code, text = run_cli(env, "groom", "--json")
    assert code == 0
    report = json.loads(text)
    by_id = {p["id"]: p for p in report["proposals"]}
    assert by_id["close #14"]["op"] == "close"
    assert by_id["close #14"]["payload"] == {
        "issue": 14,
        "change": 31,
        "comment": (
            'Closing: merged #31 "fix: the thing" has a closing reference to this issue, and its '
            "merge commit aaa1111 is on main."
        ),
        "repo": "acme/widgets",
    }
    assert by_id["labels #27 ruling"]["payload"] == {
        "issue": 27,
        "remove": ["needs-human"],
        "add": ["decided"],
        "repo": "acme/widgets",
    }
    assert by_id["milestone #20"]["payload"]["milestone"] == {
        "number": 3,
        "title": "Tier 1 October",
    }
    assert by_id["delete-branch old-1"]["payload"] == {
        "branch": "old-1",
        "sha": "d",
        "repo": "acme/widgets",
    }
    assert [p["n"] for p in report["proposals"]] == list(range(1, 9))
    assert report["ok"] is True and report["failed"] == []


def test_release_and_dependency_bot_branches_are_never_proposed(env):
    _, text = run_cli(env, "groom", "--json")
    report = json.loads(text)
    named = {u["ref"] for u in report["unclear"]} | {
        p["payload"].get("branch") for p in report["proposals"]
    }
    for bot in (
        "yeet/release-main",
        "dependabot/npm_and_yarn/x-1.2.3",
        "release-please--branches--main",
        "changeset-release/main",
        "renovate/x",
    ):
        assert bot not in named
    assert {"old-1", "keep/this"} <= named  # other merged branches still are


def test_configured_keep_branches_are_skipped(env, repo_dir):
    config = CONFIG.replace(
        "stale_days: 10\n", 'stale_days: 10\ngroom:\n  keep_branches: ["keep/*"]\n'
    )
    env = dict(env, repo=repo_dir(config))
    _, text = run_cli(env, "groom")
    assert "delete-branch keep/this" not in text
    assert "delete-branch old-1" in text
    # A configured pattern adds to the built-in ones; it doesn't replace them.
    assert "yeet/release-main" not in text


def test_an_unknown_groom_key_fails_the_config(env, repo_dir):
    env = dict(env, repo=repo_dir("---\ngroom:\n  keep: [x]\n---\n"))
    code, _ = run_cli(env, "groom")
    assert code == 2


def test_a_title_or_branch_mention_is_never_a_close_proposal(env):
    """D22: #18 is named by merged #29's branch but has no closing reference."""
    _, text = run_cli(env, "groom", "--json")
    report = json.loads(text)
    assert not any(p["payload"].get("issue") == 18 for p in report["proposals"])
    assert any(u["ref"] == "#18" and u["check"] == "named" for u in report["unclear"])


@pytest.mark.parametrize(
    "broken, part",
    [
        ("closing", "merged but still open: FAILED (closing references: GraphQL error"),
        ("links", "links to closed blockers: FAILED (blocking links: GraphQL error"),
        ("branches", "stale branches: FAILED (branches: HTTP 502"),
        ("changes", "conflicted or stale PRs: FAILED (open changes: GraphQL error"),
        ("issues", "likely duplicates: FAILED (open issues: HTTP 502"),
    ],
)
def test_each_check_fails_on_its_own_and_says_failed(env, broken, part):
    repo = Repo()
    repo.broken = {broken}
    code, text = run_cli(env, "groom", http=repo.world())
    assert code == 1
    assert part in text
    assert TOKEN not in text
    assert "FAILED (nothing above speaks for these)" in text
    # The checks that don't need the broken read still report.
    if broken != "branches" and broken != "changes":
        assert "delete-branch old-1" in text


def test_a_failed_reachability_check_is_unclear_and_failed(env):
    world = Repo().world()
    world.route("/repos/acme/widgets/compare/main...aaa1111", {"message": "boom"}, status=502)
    code, text = run_cli(env, "groom", http=world)
    assert code == 1
    assert "close #14" not in text
    assert "whether aaa1111 is on main FAILED" in text
    assert "merged but still open: FAILED (#14: HTTP 502" in text


def test_without_config_the_label_checks_are_skipped_and_say_so(env, repo_dir):
    env = dict(env, repo=repo_dir("---\nruling_pattern: ~\n---\n"))
    code, text = run_cli(env, "groom")
    assert code == 0
    assert "Skipped (not configured)" in text
    assert "tier labels and milestones: needs both tiers.labels" in text
    assert "decided but still waiting: labels.needs_human and labels.decision aren't set" in text
    assert "stale after 14 days, the default while stale_days isn't set" in text


# --- groom apply --------------------------------------------------------------------------------


def apply(env, ids, forge=None, run=None):
    forge = forge or Forge(Repo().world())
    code, text = run_cli(
        env, "groom", "apply", http=forge, run=run, stdin=json.dumps({"approved": ids})
    )
    return code, text, forge


def test_apply_writes_exactly_the_approved_proposals(env):
    code, text, forge = apply(
        env, ["close #14", "labels #27 ruling", "unlink #26 from #19", "delete-branch old-1"]
    )
    assert code == 0, text
    assert text == (
        "close #14: commented on and closed #14\n"
        "labels #27 ruling: #27: added decided; removed needs-human\n"
        "unlink #26 from #19: #26: no longer blocked by #19\n"
        "delete-branch old-1: deleted branch old-1 (was d)\n"
    )
    assert forge.writes == [
        (
            "POST",
            "/repos/acme/widgets/issues/14/comments",
            {
                "body": 'Closing: merged #31 "fix: the thing" has a closing reference to this '
                "issue, and its merge commit aaa1111 is on main."
            },
        ),
        (
            "PATCH",
            "/repos/acme/widgets/issues/14",
            {"state": "closed", "state_reason": "completed"},
        ),
        ("POST", "/repos/acme/widgets/issues/27/labels", {"labels": ["decided"]}),
        ("DELETE", "/repos/acme/widgets/issues/27/labels/needs-human", None),
        ("DELETE", "/repos/acme/widgets/issues/26/dependencies/blocked_by/9019", None),
        ("DELETE", "/repos/acme/widgets/git/refs/heads/old-1", None),
    ]


def test_apply_logs_every_operation_0600(env):
    apply(env, ["unlink #25 label", "milestone #20"])
    log = env["log"]
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["op"], e["status"], e["via"]) for e in entries] == [
        ("unlink", "written", "forge"),
        ("milestone", "written", "forge"),
    ]
    assert entries[1]["payload"]["milestone"] == {"number": 3, "title": "Tier 1 October"}


def test_apply_refuses_the_whole_list_when_any_id_isnt_current(env):
    code, text, forge = apply(env, ["close #14", "close #18", "close #99"])
    assert code == 2
    assert text == ""
    assert forge.writes == []


def test_apply_refuses_an_unclear_item(env):
    code, _, forge = apply(env, ["delete-branch wip-idle"])
    assert code == 2
    assert forge.writes == []


def test_apply_refuses_a_malformed_approval(env):
    for body in ("[1, 2]", '{"approved": []}', '{"approved": [1]}', "not json"):
        forge = Forge(Repo().world())
        code, _ = run_cli(env, "groom", "apply", http=forge, stdin=body)
        assert code == 2
        assert forge.writes == []


def test_a_failed_write_is_reported_and_the_rest_still_run(env):
    forge = Forge(Repo().world())
    forge.answers[("POST", "/repos/acme/widgets/issues/14/comments")] = (
        403,
        {"message": f"Resource not accessible by integration {TOKEN}"},
    )
    code, text, _ = apply(env, ["close #14", "unlink #25 label"], forge=forge)
    assert code == 1
    lines = text.splitlines()
    assert lines[0].startswith("close #14: FAILED (HTTP 403 from /repos/acme/widgets/issues/14/")
    assert TOKEN not in text
    assert lines[1] == "unlink #25 label: #25: removed blocked"


def test_a_branch_that_moved_is_kept(env):
    forge = Forge(Repo().world())
    forge.branch_heads["old-1"] = "e"
    code, text, _ = apply(env, ["delete-branch old-1"], forge=forge)
    assert code == 1
    assert text == "delete-branch old-1: REFUSED (old-1 moved to e since d was checked; kept)\n"
    assert forge.writes == []


def test_an_issue_closed_meanwhile_isnt_closed_again(env):
    forge = Forge(Repo().world())
    forge.issue_state[14] = "closed"
    code, text, _ = apply(env, ["close #14"], forge=forge)
    assert code == 1
    assert "REFUSED (#14 is already closed; nothing written)" in text
    assert forge.writes == []


# --- the write-wrapper --------------------------------------------------------------------------


def wrapped_env(env, repo_dir):
    config = CONFIG.replace(
        "stale_days: 10\n", "stale_days: 10\nwrite_wrapper: ./bin/tracker-write\n"
    )
    return dict(env, repo=repo_dir(config))


def test_with_a_wrapper_every_write_goes_to_it_and_none_to_the_forge(env, repo_dir):
    env = wrapped_env(env, repo_dir)
    run = FakeRun()
    run.answer(["./bin/tracker-write"], 0, "posted via decide\n", "")
    code, text, forge = apply(env, ["close #14", "unlink #25 label"], run=run)
    assert code == 0, text
    assert text == (
        "close #14: write-wrapper did it: posted via decide\n"
        "unlink #25 label: write-wrapper did it: posted via decide\n"
    )
    assert forge.writes == []
    wrapper_calls = [
        (argv, kw) for argv, kw in zip(run.calls, run.kwargs) if argv[0] == "./bin/tracker-write"
    ]
    assert [argv for argv, _ in wrapper_calls] == [
        ["./bin/tracker-write", "close"],
        ["./bin/tracker-write", "unlink"],
    ]
    first = wrapper_calls[0][1]
    assert first["cwd"] == env["repo"]
    assert json.loads(first["input"])["issue"] == 14
    assert json.loads(first["input"])["repo"] == "acme/widgets"


def test_a_wrapper_refusal_is_shown_and_nothing_is_written(env, repo_dir):
    env = wrapped_env(env, repo_dir)
    run = FakeRun()
    run.answer(["./bin/tracker-write"], 3, "", f"policy says no ({TOKEN})\n")
    code, text, forge = apply(env, ["close #14"], run=run)
    assert code == 1
    assert text == "close #14: REFUSED (write-wrapper refused: policy says no (***))\n"
    assert forge.writes == []
    entries = [json.loads(line) for line in env["log"].read_text().splitlines()]
    assert entries[0]["status"] == "refused" and entries[0]["via"] == "wrapper"


# --- forge-tracker write ------------------------------------------------------------------------


def test_write_runs_one_named_operation(env):
    forge = Forge(Repo().world())
    code, text = run_cli(
        env,
        "write",
        "labels",
        http=forge,
        stdin=json.dumps({"issue": 22, "add": ["P2"], "remove": ["P1"]}),
    )
    assert code == 0
    assert text == "labels: #22: added P2; removed P1\n"
    assert [w[:2] for w in forge.writes] == [
        ("POST", "/repos/acme/widgets/issues/22/labels"),
        ("DELETE", "/repos/acme/widgets/issues/22/labels/P1"),
    ]


@pytest.mark.parametrize(
    "op, payload, message",
    [
        ("labels", {"issue": 22}, "labels needs something to add or remove"),
        ("close", {"issue": "14", "comment": ""}, "issue must be a positive issue number"),
        ("unlink", {"issue": 3, "blocker": 4, "label": "x"}, "either blocker"),
        ("delete-branch", {"branch": "x", "sha": "y", "repo": "acme/other"}, "not acme/widgets"),
        ("milestone", {"issue": 3}, "milestone must be"),
        ("labels", {"issue": 3, "add": ["a"], "extra": 1}, "unknown key extra"),
    ],
)
def test_write_refuses_a_bad_payload_before_sending_anything(env, op, payload, message):
    forge = Forge(Repo().world())
    code, text = run_cli(env, "write", op, http=forge, stdin=json.dumps(payload))
    assert code == 2
    assert message in text
    assert forge.writes == []


def test_record_is_refused_for_writes(env):
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "--record", str(Path(env["repo"]) / "x.json"), "groom", "apply"],
        http=Forge(Repo().world()),
        run=FakeRun(),
        now=NOW,
        environ=env["environ"],
        out=out,
        stdin=io.StringIO('{"approved": ["close #14"]}'),
    )
    assert code == 2


def test_the_write_transport_sends_nothing_but_writes_inside_the_repo():
    from forge_tracker.writes import WriteTransport

    sent = []
    transport = WriteTransport(
        api_base="https://api.github.com",
        repo_path="/repos/acme/widgets",
        http=lambda *a: sent.append(a) or response({}),
        token=TOKEN,
    )
    for method, path in (
        ("GET", "/repos/acme/widgets/issues/1"),
        ("PUT", "/repos/acme/widgets/issues/1"),
        ("POST", "/repos/acme/other/issues/1/comments"),
        ("POST", "/graphql"),
        ("DELETE", "/repos/acme/widgets"),
    ):
        with pytest.raises(ForgeError, match="refused"):
            transport.send(method, path)
    assert sent == []


def test_only_the_cli_reaches_the_write_layer():
    """Writes go through writes.py alone; no adapter or report imports it."""
    root = Path(cli.__file__).parent
    for path in root.rglob("*.py"):
        if path.name in ("writes.py", "cli.py"):
            continue
        text = path.read_text()
        assert not re.search(r"^(from|import)\b[^\n]*\bwrites\b", text, re.MULTILINE), path
        assert "WriteTransport" not in text, path


# --- a recorded live run --------------------------------------------------------------------


def test_replay_of_a_recorded_github_groom(tmp_path, repo_dir):
    http = ReplayHttp(FIXTURES / "github" / "groom-lokkju-skills.json")
    out = io.StringIO()
    code = cli.main(
        ["--repo", repo_dir("---\n---\n"), *http.meta["argv"]],
        http=http,
        run=FakeRun(origin="https://github.com/lokkju/skills.git"),
        now=parse_time(http.meta["recorded_at"]),
        environ={"HOME": str(tmp_path)},
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    golden = (FIXTURES / "github" / "groom-lokkju-skills.txt").read_text()
    assert out.getvalue() == golden
    assert {call[0] for call in http.calls} <= {"GET", "POST"}
    assert all(
        call[1] == "https://api.github.com/graphql" for call in http.calls if call[0] == "POST"
    )
