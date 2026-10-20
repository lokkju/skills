import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FIXTURES, TOKEN, FakeRun, ReplayHttp, World, response
from forge_tracker import cli, config
from forge_tracker import next as next_
from forge_tracker.model import parse_time

NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)

CONFIG = """---
labels:
  needs_human: needs-human
  decision: decision
  blocked: blocked
  agent: agent-ready
tiers:
  labels: [P1, P2]
person_only_markers: [legal]
---
"""


def conn(nodes, key):
    return {
        "repository": {key: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
    }


def issue(number, labels=(), **kw):
    base = {
        "number": number,
        "title": f"issue {number}",
        "state": "open",
        "labels": [{"name": n} for n in labels],
        "assignees": [],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "closed_at": None,
        "milestone": None,
        "user": {"login": "someone", "type": "User"},
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "body": f"body of {number}",
    }
    base.update(kw)
    return base


def pr(number, branch, **kw):
    node = {
        "number": number,
        "title": f"change {number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "headRefName": branch,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-10-14T00:00:00Z",
        "updatedAt": "2026-10-14T12:00:00Z",
        "mergedAt": None,
        "author": {"__typename": "User", "login": "me"},
        "mergeCommit": None,
        "assignees": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "commits": {
            "nodes": [{"commit": {"oid": "c1", "statusCheckRollup": {"state": "PENDING"}}}]
        },
    }
    node.update(kw)
    return node


def blocked_by(number, *blockers):
    return {
        "number": number,
        "blockedBy": {
            "nodes": [
                {"number": b, "state": state, "repository": {"nameWithOwner": "acme/widgets"}}
                for b, state in blockers
            ]
        },
    }


def closed_by(number, *changes):
    return {
        "number": number,
        "closedByPullRequestsReferences": {
            "totalCount": len(changes),
            "nodes": [
                {
                    "number": n,
                    "state": state,
                    "mergedAt": "2026-10-10T00:00:00Z" if state == "MERGED" else None,
                    "headRefName": branch,
                    "baseRefName": base,
                    "repository": {"nameWithOwner": "acme/widgets"},
                }
                for n, state, branch, base in changes
            ],
        },
    }


class Repo:
    """A fake forge for /next: a decision that unblocks a chain, a needs-human item, a marker
    hint, a tiered and labelled ready list, an in-flight PR and branch, a merged-but-open issue,
    a blocked label with nothing linked, a link to a closed blocker, and a blocking cycle."""

    def __init__(self):
        self.newest_issue = "2026-10-14T12:00:00Z"
        self.pipeline_updated = "2026-10-14T13:00:00Z"
        self.head = "c1"
        self.checks = "PENDING"
        self.checks_43 = "PENDING"  # #43 has no review required, so SUCCESS makes it ready
        self.broken: set[str] = set()
        self.issues = [
            issue(10, ["decision"], title="pick the storage format"),
            issue(15, ["needs-human"], title="buy the test hardware"),
            issue(13, title="legal review of the license"),
            issue(
                21,
                ["P1"],
                title="GitHub adapter",
                milestone={"title": "October", "due_on": "2026-10-20T07:00:00Z"},
            ),
            issue(22, ["P1"], title="groom"),
            issue(23, title="catchup"),
            issue(24, title="next"),
            issue(20, title="footer width"),
            issue(30, ["P2"], title="second P2"),
            issue(31, ["P2", "agent-ready"], title="labelled P2"),
            issue(14, title="merged but open"),
            issue(25, ["blocked"], title="blocked by label only"),
            issue(26, title="stale link"),
            issue(27, title="pushed branch", assignees=[{"login": "me"}]),
            issue(50, title="cycle a"),
            issue(51, title="cycle b"),
            issue(99, pull_request={"url": "x"}),
        ]
        self.links = [
            blocked_by(22, (10, "OPEN")),
            blocked_by(23, (10, "OPEN")),
            blocked_by(24, (22, "OPEN")),
            blocked_by(20, (15, "OPEN")),
            blocked_by(26, (19, "CLOSED")),
            blocked_by(50, (51, "OPEN")),
            blocked_by(51, (50, "OPEN")),
        ]

    def world(self):
        w = World()
        w.op("NextFingerprint", lambda v: self._fingerprint())
        w.op("Milestones", lambda v: conn([], "milestones"))
        w.op(
            "BranchHeads",
            lambda v: conn(
                [{"name": n, "target": {"oid": n + "-sha"}} for n in ("main", "27-pushed")],
                "refs",
            ),
        )
        w.op("BlockingLinks", lambda v: self._maybe("links", conn(self.links, "issues")))
        w.op(
            "OpenChanges",
            lambda v: self._maybe(
                "changes",
                conn(
                    [
                        pr(
                            41,
                            "feat/github-adapter",
                            mergeable="UNKNOWN",
                            title="feat: adapter",
                            commits=self._head(),
                            reviewDecision="REVIEW_REQUIRED",
                        ),
                        # No issue behind it at all.
                        pr(
                            42,
                            "loki/ruleset-entry-point",
                            title="fix(rulesets): require the dedicated entry point",
                            isDraft=True,
                            mergeable="CONFLICTING",
                            reviewDecision="APPROVED",
                        ),
                        # Names only a closed issue (#19), and a version in its branch.
                        pr(
                            43,
                            "cilium-1.20.2",
                            title="chore(cilium): 1.20.2 (#19)",
                            commits={
                                "nodes": [
                                    {
                                        "commit": {
                                            "oid": "c43",
                                            "statusCheckRollup": {"state": self.checks_43},
                                        }
                                    }
                                ]
                            },
                        ),
                    ],
                    "pullRequests",
                ),
            ),
        )
        w.op(
            "OpenChangeHeads",
            lambda v: conn([{"number": 41, "commits": self._head()}], "pullRequests"),
        )
        w.op(
            "ClosingRefs",
            lambda v: self._maybe(
                "closing",
                conn(
                    [
                        closed_by(21, (41, "OPEN", "feat/github-adapter", "main")),
                        closed_by(14, (31, "MERGED", "fix/14", "main")),
                        closed_by(23),
                    ],
                    "issues",
                ),
            ),
        )
        w.route(
            "/repos/acme/widgets/actions/runs",
            lambda url: response(
                {"workflow_runs": [{"id": 1, "updated_at": self.pipeline_updated}]}
            ),
        )
        w.route("/repos/acme/widgets/issues", lambda url: self._rest("issues", self.issues))
        w.route(
            "/repos/acme/widgets/branches",
            lambda url: self._rest(
                "branches",
                [
                    {"name": "main", "commit": {"sha": "m"}},
                    {"name": "feat/github-adapter", "commit": {"sha": "a"}},
                    {"name": "27-pushed", "commit": {"sha": "b"}},
                    {"name": "wip/2026-10-14", "commit": {"sha": "c"}},
                    {"name": "old-1", "commit": {"sha": "d"}},
                ],
            ),
        )
        w.route("/repos/acme/widgets/compare/main...27-pushed", {"ahead_by": 2})
        w.route("/repos/acme/widgets/compare/main...wip/2026-10-14", {"ahead_by": 1})
        w.route("/repos/acme/widgets/compare/main...old-1", {"ahead_by": 0})
        return w

    def _fingerprint(self):
        return {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "m"}},
                "openIssues": {"totalCount": 16},
                "openChanges": {"totalCount": 1},
                "newestIssue": {"nodes": [{"updatedAt": self.newest_issue}]},
                "newestChange": {"nodes": [{"updatedAt": "2026-10-14T12:00:00Z"}]},
            }
        }

    def _head(self):
        rollup = {"state": self.checks} if self.checks else None
        return {"nodes": [{"commit": {"oid": self.head, "statusCheckRollup": rollup}}]}

    def _maybe(self, name, data):
        if name in self.broken:
            return {"errors": [{"message": f"{name} broke with {TOKEN}"}]}
        return data

    def _rest(self, name, body):
        if name in self.broken:
            return response({"message": f"{name} broke"}, status=502)
        return response(body)


@pytest.fixture
def env(tmp_path, repo_dir):
    directory = repo_dir(CONFIG)
    cache = tmp_path / "cache"
    return {
        "repo": directory,
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(cache)},
        "dir": cache / "forge-tracker" / "next" / "github.com_acme__widgets",
    }


def run_next(env, *args, http=None, now=NOW, stdin=None):
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "next", *args],
        http=http if http is not None else Repo().world(),
        run=FakeRun(),
        now=now,
        environ=env["environ"],
        out=out,
        tz=timezone.utc,
        stdin=io.StringIO(stdin) if stdin is not None else None,
    )
    return code, out.getvalue()


def save(env, slate, now=NOW):
    return run_next(env, "save-slate", stdin=json.dumps(slate), now=now)


EXPECTED = """\
acme/widgets on github.com, facts from 2026-10-15 08:00 (read now)

Your queue (no slate: most agent-ready items unblocked first)
  1. #10 pick the storage format (decision)  unblocks 3: #22, #23, #24
  2. #15 buy the test hardware (needs-human)  unblocks 1: #20
  3. #13 legal review of the license (marker: legal)
Agent-ready (no slate: by tier, agent label first, then due date)
  #21 GitHub adapter (P1, due 2026-10-20, untagged)  in flight: #41 (conflicts: unknown, ci: pending, review: review required)
  #31 labelled P2 (P2, agent label)
  #30 second P2 (P2, untagged)
  #14 merged but open (untagged)
  #26 stale link (untagged)
  #27 pushed branch (untagged, owner: me)  in flight: branch 27-pushed (2 ahead)
  (5 untagged: no label marks them either way, so agent-ready is the default, not a judgement; a slate reads their text)
Blocked
  #22 groom waits on #10 (native)
  #20 footer width waits on #15 (native)
  #23 catchup waits on #10 (native)
  #24 next waits on #22 (native)
  #25 blocked by label only waits on nothing linked (blocked label only)
  #50 cycle a waits on #51 (native)
  #51 cycle b waits on #50 (native)
Open PRs with no open issue
  #42 fix(rulesets): require the dedicated entry point (draft, conflicts: yes, ci: pending, review: approved)  branch loki/ruleset-entry-point
  #43 chore(cilium): 1.20.2 (#19) (conflicts: no, ci: pending)  branch cilium-1.20.2, names #19 (not open)
Flags
  #14 is closed by merged #31 but still open (run /groom)
  #26 has a link to closed blocker #19 (run /groom)
  #25 blocked label with no open blocker linked (run /groom)
  blocking cycle: #50 -> #51 -> #50
  branch wip/2026-10-14 has no PR and names no open issue (1 ahead)

slate: needed (no slate is saved for this repo). Rank from `forge-tracker next facts --json` and save with `forge-tracker next save-slate`.
"""


def test_report_without_a_slate(env):
    code, text = run_next(env)
    assert code == 0
    assert text == EXPECTED


def test_classification_in_json(env):
    code, text = run_next(env, "--json")
    assert code == 0
    report = json.loads(text)
    classes = {
        i["ref"]: i["class"]
        for key in ("queue", "agent_ready", "blocked", "unknown")
        for i in report[key]
    }
    assert classes["#10"] == classes["#15"] == classes["#13"] == "person-only"
    assert classes["#27"] == "agent-ready"  # an assignee is never a reason
    assert classes["#25"] == classes["#50"] == "blocked"
    assert classes["#26"] == "agent-ready"  # a closed blocker doesn't block
    assert "#99" not in classes  # a PR in the issues list isn't an issue
    by_ref = {i["ref"]: i for key in ("queue", "agent_ready", "blocked") for i in report[key]}
    assert by_ref["#13"]["person_only"] == {"labels": [], "markers": ["legal"]}
    assert by_ref["#10"]["unblocks"] == ["#22", "#23", "#24"]
    assert by_ref["#21"]["in_flight"][0]["via"] == ["closing reference"]
    assert by_ref["#21"]["in_flight"][0]["conflicts"] == "unknown"
    assert by_ref["#27"]["owners"] == ["me"]
    assert by_ref["#14"]["merged_by"] == [
        {"ref": "#31", "base": "main", "merged_at": "2026-10-10T00:00:00Z"}
    ]
    assert by_ref["#50"]["cycle"] is True
    assert report["cycles"] == [["#50", "#51"]]
    assert report["slate"] == {"status": "needed", "reason": "no slate is saved for this repo"}
    assert "body" not in by_ref["#10"]


def test_facts_json_carries_bodies_and_no_slate(env):
    code, text = run_next(env, "facts", "--json")
    assert code == 0
    report = json.loads(text)
    assert report["slate"]["status"] == "skipped"
    assert report["queue"][0]["body"] == "body of 10"


def test_facts_are_cached_0600_and_reused_while_the_fingerprint_holds(env):
    repo = Repo()
    run_next(env, http=repo.world())
    facts = env["dir"] / "facts.json"
    assert stat.S_IMODE(os.stat(facts).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env["dir"]).st_mode) == 0o700

    world = repo.world()
    code, text = run_next(env, http=world)
    assert code == 0
    assert "(from cache, fingerprint unchanged)" in text
    ops = {json.loads(c[3])["query"].split("(")[0].split()[-1] for c in world.calls if c[3]}
    assert ops == {"NextFingerprint", "Milestones", "BranchHeads", "OpenChangeHeads"}
    assert not any("/issues" in c[1] for c in world.calls)
    assert not any("/actions/runs" in c[1] for c in world.calls)

    repo.newest_issue = "2026-10-15T07:00:00Z"
    world = repo.world()
    code, text = run_next(env, http=world)
    assert "(read now)" in text
    assert any("/issues" in c[1] for c in world.calls)


def test_refresh_rereads_even_when_the_fingerprint_holds(env):
    run_next(env)
    world = Repo().world()
    _, text = run_next(env, "--refresh", http=world)
    assert "(read now)" in text
    assert any("/issues" in c[1] for c in world.calls)


def test_a_config_change_moves_the_fingerprint(env):
    run_next(env)
    Path(env["repo"], ".claude", "forge-tracker.local.md").write_text(
        CONFIG.replace("[legal]", "[legal, hardware]")
    )
    _, text = run_next(env)
    assert "(read now)" in text
    assert "buy the test hardware (needs-human, marker: hardware)" in text


def test_a_failed_read_is_written_but_never_reused(env):
    repo = Repo()
    repo.broken = {"branches"}
    code, text = run_next(env, http=repo.world())
    assert code == 1
    assert "  branches: FAILED (HTTP 502 from /repos/acme/widgets/branches: branches broke)" in text
    assert "slate: unavailable" in text
    facts = json.loads((env["dir"] / "facts.json").read_text())
    assert facts["ok"] is False

    repo.broken = set()
    world = repo.world()
    code, text = run_next(env, http=world)
    assert code == 0
    assert "(read now)" in text


def test_failed_links_leave_items_unclassified_never_ready(env):
    repo = Repo()
    repo.broken = {"links"}
    code, text = run_next(env, http=repo.world())
    assert code == 1
    assert TOKEN not in text
    assert "  blocking links: FAILED (GraphQL error: links broke with ***)" in text
    assert "not classified (blocking links FAILED, so these can't be called agent-ready)" in text
    assert "nothing blocked" not in text
    assert "nothing agent-ready" not in text
    assert "unblocks: unknown (blocking links FAILED)" in text
    # The blocked label still blocks without the links.
    assert "#25 blocked by label only waits on nothing linked" in text


def test_failed_issues_never_render_as_nothing(env):
    repo = Repo()
    repo.broken = {"issues"}
    code, text = run_next(env, http=repo.world())
    assert code == 1
    assert "nothing" not in text
    assert text.count("open issues: FAILED (HTTP 502 from /repos/acme/widgets/issues") == 3


def test_failed_closing_refs_say_merged_but_open_isnt_checked(env):
    repo = Repo()
    repo.broken = {"closing"}
    code, text = run_next(env, http=repo.world())
    assert code == 1
    assert "  closing references: FAILED (GraphQL error: closing broke with ***)" in text
    assert "merged-but-open issues aren't flagged" in text
    # The open PR still marks #21 in flight? Not by its title or branch: only the closing
    # reference names it, so it's unmarked and the report says marks may be missing.
    assert "#21 GitHub adapter (P1, due 2026-10-20, untagged)\n" in text
    # So #41 is listed with no issue, and the list says why it may be wrong.
    assert "  #41 feat: adapter (conflicts: unknown" in text
    assert "(closing references FAILED, so a PR here may still close an open issue)" in text


def test_open_prs_with_no_open_issue_are_listed_in_json(env):
    code, text = run_next(env, "--json")
    assert code == 0
    others = {c["ref"]: c for c in json.loads(text)["other_changes"]}
    assert sorted(others) == ["#42", "#43"]  # #41 closes #21, so it's in flight there
    assert others["#42"]["draft"] is True
    assert others["#42"]["conflicts"] == "yes"
    assert others["#42"]["review"] == "approved"
    assert others["#42"]["names"] == []
    assert others["#43"]["names"] == ["#19"]


SLATE = {
    "queue": ["#15", "#10"],
    "lanes": [
        {"area": "forges/github.py", "items": ["#21"], "why": "the adapter"},
        {"area": "docs", "items": ["#31", "#13"]},
    ],
    "overrules": {"#13": "a license summary an agent can draft"},
    "note": "ranked for the October milestone",
}

WITH_SLATE = """\
acme/widgets on github.com, slate from 2026-10-15 08:00 (30m old; facts unchanged since)

Your queue
  1. #15 buy the test hardware (needs-human)  unblocks 1: #20
  2. #10 pick the storage format (decision)  unblocks 3: #22, #23, #24
Agent lanes (grouped by judgement: these shouldn't share files)
  A  forges/github.py: the adapter  (in flight)
     #21 GitHub adapter (P1, due 2026-10-20)  in flight: #41 (conflicts: unknown, ci: pending, review: review required)
  B  docs
     #31 labelled P2 (P2, agent label)
     #13 legal review of the license (marker: legal)  overruled: a license summary an agent can draft
  not in a lane: #30, #14, #26, #27
Blocked
"""


def test_saved_slate_is_shown_while_the_fingerprint_holds(env):
    run_next(env)
    code, text = save(env, SLATE)
    assert code == 0
    assert text.startswith("slate saved: 2 in your queue, 2 lanes (facts ")
    assert stat.S_IMODE(os.stat(env["dir"] / "slate.json").st_mode) == 0o600
    code, text = run_next(env, now=NOW + timedelta(minutes=30))
    assert code == 0
    assert text.startswith(WITH_SLATE)
    assert text.endswith("\nslate note: ranked for the October milestone\n")

    code, text = run_next(env, "--rerank", now=NOW + timedelta(minutes=31))
    assert "slate: needed (--rerank)" in text


def test_a_moved_fingerprint_asks_for_a_new_slate(env):
    repo = Repo()
    run_next(env, http=repo.world())
    save(env, SLATE)
    repo.newest_issue = "2026-10-15T07:59:00Z"
    code, text = run_next(env, http=repo.world())
    assert code == 0
    assert "slate: needed (the facts moved since the slate from 2026-10-15T08:00:00Z)" in text


def test_a_pipeline_update_alone_keeps_the_facts_and_the_slate(env):
    repo = Repo()
    run_next(env, http=repo.world())
    save(env, SLATE)
    repo.pipeline_updated = "2026-10-15T07:59:00Z"
    code, text = run_next(env, http=repo.world(), now=NOW + timedelta(minutes=30))
    assert code == 0
    assert text.startswith(WITH_SLATE)


@pytest.mark.parametrize(
    "change",
    [
        {"checks": "SUCCESS"},
        {"checks": "FAILURE"},
        {"checks": None},
        {"head": "c2"},
    ],
    ids=["checks-pass", "checks-fail", "checks-gone", "new-head"],
)
def test_a_check_state_or_head_change_moves_the_fingerprint(env, change):
    repo = Repo()
    run_next(env, http=repo.world())
    save(env, SLATE)
    for key, value in change.items():
        setattr(repo, key, value)
    code, text = run_next(env, http=repo.world())
    assert code == 0
    assert "(read now)" in text
    assert "slate: needed (the facts moved since the slate from 2026-10-15T08:00:00Z)" in text


def test_queue_items_not_ranked_are_appended(env):
    run_next(env)
    save(env, {"queue": ["#15"], "lanes": []})
    _, text = run_next(env)
    assert "  1. #15 buy the test hardware" in text
    assert (
        "  2. #10 pick the storage format (decision)  unblocks 3: #22, #23, #24  (not ranked)"
        in text
    )
    assert "  3. #13 legal review of the license (marker: legal)  (not ranked)" in text


@pytest.mark.parametrize(
    "slate, message",
    [
        ({"queue": ["#77"], "lanes": []}, "queue item 1: #77 isn't an open issue in the facts"),
        (
            {"queue": ["#21"], "lanes": []},
            "queue item 1: #21 is agent-ready (untagged); add it to person_only with the reason",
        ),
        ({"queue": ["#31"], "lanes": []}, "queue item 1: #31 is agent-ready, not person-only"),
        (
            {"queue": [], "lanes": [{"area": "x", "items": ["#22"]}]},
            "lane 1: #22 is blocked, not agent-ready",
        ),
        (
            {"queue": [], "lanes": [{"area": "x", "items": ["#10"]}]},
            "lane 1: #10 is person-only, not agent-ready",
        ),
        (
            {"queue": [], "lanes": [{"area": "x", "items": ["#13"]}]},
            "lane 1: #13 is person-only by marker (legal); add it to overrules",
        ),
        (
            {"queue": [], "lanes": [], "overrules": {"#10": "because"}},
            "overrules: #10 is person-only by label (decision); only marker hints",
        ),
        (
            {"queue": ["#10"], "lanes": [{"area": "x", "items": ["#21", "#21"]}]},
            "lane 1: #21 appears more than once",
        ),
        ({"queue": [], "lanes": [{"items": ["#21"]}]}, "lane 1 needs an area"),
        ({"queue": [], "lanes": [], "rank": 1}, "unknown key rank"),
        ([], "the slate must be a JSON object"),
    ],
)
def test_save_slate_refuses_what_doesnt_fit_the_facts(env, capsys, slate, message):
    run_next(env)
    code, _ = save(env, slate)
    assert code == 2
    assert message in capsys.readouterr().err
    assert not (env["dir"] / "slate.json").exists()


def test_save_slate_needs_complete_current_facts(env, capsys):
    code, _ = save(env, SLATE)
    assert code == 2
    assert "no facts are cached" in capsys.readouterr().err

    repo = Repo()
    repo.broken = {"links"}
    run_next(env, http=repo.world())
    code, _ = save(env, SLATE)
    assert code == 2
    assert "a slate is saved only over complete facts" in capsys.readouterr().err

    run_next(env)
    Path(env["repo"], ".claude", "forge-tracker.local.md").write_text(CONFIG + "\n")
    code, _ = save(env, SLATE)
    assert code == 2
    assert "the config or forge-tracker changed" in capsys.readouterr().err


def test_save_slate_reads_no_forge(env):
    run_next(env)
    run = FakeRun()
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "next", "save-slate"],
        http=World(),  # any request would fail the test
        run=run,
        now=NOW,
        environ=env["environ"],
        out=out,
        stdin=io.StringIO(json.dumps(SLATE)),
    )
    assert code == 0
    assert not any(call[:1] == ["gh"] for call in run.calls)


def test_reads_are_get_or_graphql_queries_only(env):
    world = Repo().world()
    run_next(env, http=world)
    assert {c[0] for c in world.calls} == {"GET", "POST"}
    assert all(c[1] == "https://api.github.com/graphql" for c in world.calls if c[0] == "POST")


# --- a repo with no person-only label (#69) ---------------------------------------------------

UNLABELLED = """---
labels:
  needs_human: ~
  decision: ~
  blocked: blocked
tiers:
  labels: [P1, P2]
---
"""


@pytest.fixture
def bare(env, repo_dir):
    return dict(env, repo=repo_dir(UNLABELLED))


def test_without_a_person_only_label_nothing_is_called_agent_ready(bare):
    code, text = run_next(bare, "--json")
    assert code == 0
    report = json.loads(text)
    assert report["labelled"] is False
    assert report["agent_ready"] == [] and report["queue"] == []
    unclassified = [i["ref"] for i in report["unclassified"]]
    assert {"#10", "#13", "#15", "#21", "#27"} <= set(unclassified)
    # Blocking still comes from the forge: a link or the blocked label.
    assert {i["ref"] for i in report["blocked"]} >= {"#22", "#25"}

    _, text = run_next(bare)
    lines = text.splitlines()
    assert lines[1].startswith(
        "Unlabelled: no needs-human or decision label is configured, so 9 open issues are "
        "unclassified until a slate places them"
    )
    assert "  nothing marked person-only: no label is configured to mark any" in text
    assert "nothing agent-ready" not in text
    assert "Not classified (no needs-human or decision label is configured" in text
    assert "  #15 buy the test hardware\n" in text


def test_with_labels_configured_classification_is_unchanged(env):
    report = json.loads(run_next(env, "--json")[1])
    assert report["labelled"] is True
    assert report["unclassified"] == []
    assert "Unlabelled" not in run_next(env)[1]


def test_nothing_blocked_says_only_what_the_forge_records(bare, repo_dir):
    repo = Repo()
    repo.links = []
    repo.issues = [i for i in repo.issues if i["number"] != 25]
    for config_text, line in (
        (UNLABELLED, "  no blocking links or blocked labels recorded"),
        ("---\n---\n", "  no blocking links recorded (no blocked label is configured)"),
    ):
        env = dict(bare, repo=repo_dir(config_text))
        _, text = run_next(env, "--refresh", http=repo.world())
        assert text.split("Blocked\n", 1)[1].startswith(line + "\n"), text


JUDGED = {
    "queue": ["#15", "#10"],
    "person_only": {
        "#15": "physical work: buying hardware",
        "#10": "research done, waits on the owner's decision",
    },
    "lanes": [{"area": "forges/github.py", "items": ["#21"]}],
}


def test_the_slate_puts_unclassified_items_in_the_queue_with_its_reason(bare):
    run_next(bare)
    code, text = save(bare, JUDGED)
    assert code == 0, text
    assert text.startswith("slate saved: 2 in your queue, 1 lane")
    _, text = run_next(bare)
    assert (
        "Unlabelled: no needs-human or decision label is configured, so the queue and lanes "
        "place 9 open issues by Claude's judgement from their text, not by a label"
    ) in text
    assert (
        "  1. #15 buy the test hardware  unblocks 1: #20  judged person-only: physical work: "
        "buying hardware\n"
    ) in text
    assert "  2. #10 pick the storage format  unblocks 3: #22, #23, #24  judged person-only" in text
    assert "     #21 GitHub adapter (P1, due 2026-10-20)" in text
    assert "  not placed (unclassified, in neither the queue nor a lane): " in text
    assert "Not classified" not in text
    report = json.loads(run_next(bare, "--json")[1])
    assert report["slate"]["person_only"]["#15"] == "physical work: buying hardware"


@pytest.mark.parametrize(
    "slate, message",
    [
        (
            {"queue": ["#15"], "lanes": []},
            "queue item 1: #15 is unclassified; add it to person_only with the reason",
        ),
        (
            {"queue": [], "lanes": [], "person_only": {"#15": "hardware"}},
            "person_only: #15 is judged person-only but isn't in the queue",
        ),
        (
            {"queue": ["#15"], "lanes": [], "person_only": {"#15": " "}},
            "person_only: #15 needs a stated reason",
        ),
        (
            {"queue": ["#22"], "lanes": [], "person_only": {"#22": "waits"}},
            "person_only: #22 is blocked; only an item that isn't blocked",
        ),
        (
            {
                "queue": ["#15"],
                "lanes": [{"area": "x", "items": ["#15"]}],
                "person_only": {"#15": "hardware"},
            },
            "lane 1: #15 appears more than once",
        ),
    ],
)
def test_save_slate_checks_the_judged_queue(bare, capsys, slate, message):
    run_next(bare)
    code, _ = save(bare, slate)
    assert code == 2
    assert message in capsys.readouterr().err


# --- untagged items in a labelled repo (#83) -------------------------------------------------


def test_untagged_items_are_marked_in_json_without_a_slate(env):
    report = json.loads(run_next(env, "--json")[1])
    untagged = {i["ref"]: i["untagged"] for i in report["agent_ready"]}
    assert untagged == {
        "#21": True,
        "#31": False,  # the agent label decided it
        "#30": True,
        "#14": True,
        "#26": True,
        "#27": True,
    }
    assert all(not i["untagged"] for i in report["queue"] + report["blocked"])


def test_a_labelled_repo_can_judge_an_untagged_item_into_the_queue(env):
    run_next(env)
    slate = {
        "queue": ["#10", "#30"],
        "lanes": [{"area": "forges/github.py", "items": ["#21"]}],
        "person_only": {"#30": "a decision: which storage format, with no default"},
    }
    code, text = save(env, slate)
    assert code == 0, text
    _, text = run_next(env)
    assert (
        "  2. #30 second P2 (P2)  judged person-only: a decision: which storage format, "
        "with no default\n"
    ) in text
    assert "  not in a lane: #31, #14, #26, #27\n" in text  # #30 is in the queue, not unlaned
    assert "untagged" not in text  # the slate placed them; the mark is for unranked reports
    report = json.loads(run_next(env, "--json")[1])
    assert report["slate"]["person_only"] == {
        "#30": "a decision: which storage format, with no default"
    }


@pytest.mark.parametrize(
    "slate, message",
    [
        (
            {"queue": ["#31"], "lanes": [], "person_only": {"#31": "hardware"}},
            "person_only: #31 has the agent label; a labelled item stays as its label says",
        ),
        (
            {"queue": ["#10"], "lanes": [], "person_only": {"#10": "a decision"}},
            "person_only: #10 is person-only by label (decision); it's in the queue already",
        ),
        (
            {"queue": ["#22"], "lanes": [], "person_only": {"#22": "waits"}},
            "person_only: #22 is blocked; only an item that isn't blocked",
        ),
        (
            {"queue": ["#30"], "lanes": [], "person_only": {"#30": ""}},
            "person_only: #30 needs a stated reason",
        ),
    ],
)
def test_a_labelled_repo_judges_only_what_no_label_decided(env, capsys, slate, message):
    run_next(env)
    code, _ = save(env, slate)
    assert code == 2
    assert message in capsys.readouterr().err


# --- the closing recommendation (#78) -------------------------------------------------------

RECOMMEND = {
    "you": [
        {"ref": "#10", "why": "it unblocks #22, #23 and #24, more than anything else queued"},
        {"ref": "#15", "why": "the hardware gates #20"},
    ],
    "agent_cap": 3,
    "start": [{"lane": "A", "why": "P1, due 2026-10-20, and its PR is already open"}],
    "wait": [{"lane": "b", "why": "P2 docs; it touches nothing urgent"}],
    "merge": [{"ref": "#43", "why": "a version bump with green CI; the cheapest progress"}],
    "not_yet": [
        {"ref": "#30", "why": "its design is still open"},
        {"ref": "#42", "why": "a draft with conflicts"},
    ],
}

RECOMMENDED = """
Recommended next (Claude's judgement from the facts; nothing starts or merges until you say so)
  Merge first (CI green, no conflicts, review approved or not required)
    #43 chore(cilium): 1.20.2 (#19): a version bump with green CI; the cheapest progress
  For you
    1. #10 pick the storage format: it unblocks #22, #23 and #24, more than anything else queued
    2. #15 buy the test hardware: the hardware gates #20
  Start now (1 lane, agent cap 3)
    A  forges/github.py: P1, due 2026-10-20, and its PR is already open
  Waits
    B  docs: P2 docs; it touches nothing urgent
  Not yet
    #30 second P2: its design is still open
    PR #42 fix(rulesets): require the dedicated entry point: a draft with conflicts
"""


@pytest.fixture
def ready_repo():
    repo = Repo()
    repo.checks_43 = "SUCCESS"
    return repo


def test_the_slate_closes_with_a_recommendation(env, ready_repo):
    run_next(env, http=ready_repo.world())
    code, text = save(env, dict(SLATE, recommend=RECOMMEND))
    assert code == 0, text
    assert text.startswith("slate saved: 2 in your queue, 2 lanes, a recommendation (facts ")
    code, text = run_next(env, http=ready_repo.world())
    assert code == 0
    assert text.endswith("\nslate note: ranked for the October milestone\n" + RECOMMENDED)

    rec = json.loads(run_next(env, "--json", http=ready_repo.world())[1])["slate"]["recommend"]
    assert rec["you"][0] == {
        "ref": "#10",
        "title": "pick the storage format",
        "why": "it unblocks #22, #23 and #24, more than anything else queued",
    }
    assert rec["start"] == [
        {
            "lane": "A",
            "area": "forges/github.py",
            "why": "P1, due 2026-10-20, and its PR is already open",
        }
    ]
    assert rec["wait"][0]["lane"] == "B"  # a lane may be named in either case or by number
    assert rec["merge"][0]["ref"] == "#43"
    assert [(n["ref"], n["kind"]) for n in rec["not_yet"]] == [("#30", "issue"), ("#42", "pr")]


def test_a_slate_without_a_recommendation_prints_none(env):
    run_next(env)
    save(env, SLATE)
    _, text = run_next(env)
    assert "Recommended next" not in text
    assert json.loads(run_next(env, "--json")[1])["slate"]["recommend"] is None


def test_no_recommendation_shows_without_a_current_slate(env, ready_repo):
    run_next(env, http=ready_repo.world())
    save(env, dict(SLATE, recommend=RECOMMEND))
    _, text = run_next(env, "--rerank", http=ready_repo.world())
    assert "Recommended next" not in text


def rec(**changes):
    out = {k: v for k, v in RECOMMEND.items() if k not in changes}
    out.update({k: v for k, v in changes.items() if v is not None})
    return dict(SLATE, recommend=out)


@pytest.mark.parametrize(
    "slate, message",
    [
        (
            rec(you=[{"ref": "#21", "why": "x"}]),
            "recommend.you 1: #21 isn't in the slate's queue",
        ),
        (rec(you=[{"ref": "#10"}]), "recommend.you 1 needs a stated reason (why)"),
        (
            rec(you=[{"ref": r, "why": "x"} for r in ("#10", "#15", "#10")]),
            "recommend.you 3: #10 is picked more than once",
        ),
        (
            rec(merge=[{"ref": "#41", "why": "x"}]),
            (
                "recommend.merge 1: #41 isn't ready to merge (conflicts: unknown, ci: pending, "
                "review: review required)"
            ),
        ),
        (
            rec(merge=[{"ref": "#42", "why": "x"}], not_yet=[]),
            "recommend.merge 1: #42 isn't ready to merge (it's a draft, conflicts: yes",
        ),
        (
            rec(merge=[{"ref": "#21", "why": "x"}]),
            "recommend.merge 1: '#21' isn't an open PR in the facts",
        ),
        (
            rec(agent_cap=1, start=[{"lane": "A", "why": "x"}, {"lane": 2, "why": "y"}], wait=[]),
            "recommend.start: 2 lanes is more than the agent cap of 1",
        ),
        (rec(wait=[]), "every lane starts now or waits, with a reason; missing B"),
        (
            rec(wait=[{"lane": "A", "why": "x"}, {"lane": "B", "why": "y"}]),
            "recommend.wait 1: lane A is already in recommend.start",
        ),
        (rec(wait=[{"lane": "Z", "why": "x"}]), "recommend.wait 1: 'Z' isn't a lane in the slate"),
        (
            rec(not_yet=[{"ref": "#21", "why": "x"}]),
            "recommend.not_yet 1: #21 is in a lane that starts now",
        ),
        (
            rec(not_yet=[{"ref": "#10", "why": "x"}]),
            "recommend.not_yet 1: #10 is picked for you",
        ),
        (
            rec(not_yet=[{"ref": "#77", "why": "x"}]),
            "recommend.not_yet 1: '#77' isn't an open issue or PR in the facts",
        ),
        (rec(agent_cap="three"), "recommend.agent_cap must be a whole number of agents"),
        (rec(lanes_now=[]), "recommend: unknown key lanes_now"),
        (dict(SLATE, recommend=["#10"]), "recommend must be an object"),
    ],
)
def test_save_slate_checks_the_recommendation(env, ready_repo, capsys, slate, message):
    run_next(env, http=ready_repo.world())
    code, _ = save(env, slate)
    assert code == 2
    assert message in capsys.readouterr().err
    assert not (env["dir"] / "slate.json").exists()


def test_merge_ready_needs_green_ci_no_conflicts_and_a_review_when_required():
    ready = {"draft": False, "conflicts": "no", "pipeline": "success", "review": None}
    assert next_.merge_ready(ready) == []
    assert next_.merge_ready(dict(ready, review="approved")) == []
    assert next_.merge_ready(dict(ready, conflicts="unknown")) == ["conflicts: unknown"]
    assert next_.merge_ready(dict(ready, pipeline=None)) == ["ci: none"]
    assert next_.merge_ready(dict(ready, review="changes_requested")) == [
        "review: changes requested"
    ]


# --- classification units -------------------------------------------------------------------


def facts(issues, links=(), **parts):
    def part(value):
        return {"error": value} if isinstance(value, str) else {"data": list(value)}

    out = {
        "parts": {
            "issues": part(issues),
            "links": part(links),
            "changes": part(parts.get("changes", [])),
            "closing": part(parts.get("closing", [])),
            "branches": part(parts.get("branches", [])),
        }
    }
    return out


def fact_issue(number, labels=(), title=None):
    return {
        "number": number,
        "title": title or f"issue {number}",
        "labels": list(labels),
        "assignees": [],
        "milestone": None,
        "url": "",
        "updated": "2026-10-01T00:00:00Z",
    }


def link(blocked, blocker, open_=True):
    return {
        "blocked": blocked,
        "blocker": blocker,
        "blocker_open": open_,
        "kind": "native",
        "blocker_project": None,
    }


def test_unblock_counts_are_transitive_and_cycle_safe():
    cfg = config.from_mapping({"labels": {"decision": "decision"}})
    issues = [fact_issue(1, ["decision"])] + [fact_issue(n) for n in range(2, 7)]
    # 1 blocks 2; 2 blocks 3; 3 blocks 2 (a cycle); 3 blocks 4; 5 is unrelated; 6 blocks 1.
    links = [link(2, 1), link(3, 2), link(2, 3), link(4, 3), link(1, 6)]
    result = next_.classify(facts(issues, links), cfg)
    items = {i["number"]: i for i in result["items"]}
    assert items[1]["unblocks"] == ["#2", "#3", "#4"]
    assert items[1]["cycle"] is True
    assert items[6]["unblocks"] == ["#2", "#3", "#4"]  # 1 is person-only, so not counted
    assert items[5]["unblocks"] == [] and items[5]["cycle"] is False
    assert result["cycles"] == [["#2", "#3"]]


def test_a_long_chain_doesnt_hit_the_recursion_limit():
    cfg = config.from_mapping({})
    n = 1500  # past Python's default recursion limit of 1000
    issues = [fact_issue(i) for i in range(1, n + 1)]
    links = [link(i + 1, i) for i in range(1, n)]
    result = next_.classify(facts(issues, links), cfg)
    assert len(result["items"][0]["unblocks"]) == n - 1
    assert result["cycles"] == []


def test_markers_match_whole_words_in_titles_and_labels():
    cfg = config.from_mapping({"person_only_markers": ["legal", "hardware"]})
    issues = [
        fact_issue(1, title="Legal review"),
        fact_issue(2, title="paralegal tooling"),
        fact_issue(3, labels=["area:hardware"]),
    ]
    items = {i["number"]: i for i in next_.classify(facts(issues), cfg)["items"]}
    assert items[1]["person_only"]["markers"] == ["legal"]
    assert items[2]["person_only"]["markers"] == []
    # Markers alone configure no person-only label, so an unmarked item is unclassified.
    assert items[1]["class"] == "person-only" and items[2]["class"] == "unclassified"
    assert items[3]["person_only"]["markers"] == ["hardware"]


def test_a_cross_project_blocker_blocks_but_isnt_counted():
    cfg = config.from_mapping({})
    links = [dict(link(1, 5), blocker_project="acme/meta")]
    items = next_.classify(facts([fact_issue(1)], links), cfg)["items"]
    assert items[0]["class"] == "blocked"
    assert items[0]["blocked_by"] == [{"ref": "acme/meta#5", "kind": "native"}]


def test_pickups_mark_a_branch_in_flight():
    cfg = config.from_mapping({})
    branches = [{"name": "feature-x", "sha": "s", "has_change": False, "ahead_by": 3}]
    result = next_.classify(
        facts([fact_issue(7)], branches=branches), cfg, pickups={"feature-x": 7}
    )
    assert result["items"][0]["in_flight"] == [
        {"kind": "branch", "ref": "feature-x", "ahead_by": 3, "via": ["pickup"]}
    ]
    assert result["other_branches"] == []


def fact_change(number, title, branch):
    return {
        "number": number,
        "title": title,
        "branch": branch,
        "draft": False,
        "conflicts": "no",
        "pipeline": None,
        "review": None,
        "author": "lokkju",
        "updated": "2026-10-01T00:00:00Z",
        "url": "",
    }


def test_titles_and_branches_mark_in_flight_only_by_a_real_reference():
    # lokkju/skills#96: a version in a title or branch put #300 in flight.
    cfg = config.from_mapping({})
    changes = [
        fact_change(
            65,
            "fix: point the hand-off nudge at deferred TaskCreate "
            "(haiku is Haiku 5.5 since 2.1.300)",
            "fix/task-tools-2-1-300",
        ),
        fact_change(66, "bump yeet to v0.17.1", "chore/yeet"),
        fact_change(67, "fix: x (lokkju/skills#286, acme/other#300, GH-1)", "x"),
    ]
    issues = [fact_issue(1), fact_issue(286), fact_issue(300)]
    result = next_.classify(dict(facts(issues, changes=changes), project="lokkju/skills"), cfg)
    flying = {i["number"]: [e["ref"] for e in i["in_flight"]] for i in result["items"]}
    assert flying == {1: ["#67"], 286: ["#67"], 300: []}


def test_no_pickups_are_recorded_before_phase_2(env):
    from forge_tracker.detect import Remote

    assert next_.recorded_pickups(Remote("github.com", "acme/widgets"), env["environ"]) == {}


def test_source_tag_is_stable():
    assert next_.source_tag() == next_.source_tag()
    assert len(next_.source_tag()) == 16


# --- a recorded live run --------------------------------------------------------------------


def test_replay_of_a_recorded_github_next(tmp_path, repo_dir):
    http = ReplayHttp(FIXTURES / "github" / "next-lokkju-skills.json")
    now = parse_time(http.meta["recorded_at"])
    out = io.StringIO()
    code = cli.main(
        ["--repo", repo_dir("---\n---\n"), *http.meta["argv"]],
        http=http,
        run=FakeRun(origin="https://github.com/lokkju/skills.git"),
        now=now,
        environ={"HOME": str(tmp_path)},
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    golden = (FIXTURES / "github" / "next-lokkju-skills.txt").read_text()
    assert out.getvalue() == golden
    assert all(call[2]["Authorization"] == f"Bearer {TOKEN}" for call in http.calls)
    assert {call[0] for call in http.calls} <= {"GET", "POST"}
