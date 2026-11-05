"""The GitLab adapter beyond the shared contract (test_contract.py): the glab transport, the
reads GitLab answers its own way (note events, to-do items, per-MR detail, retried jobs, closing
MRs), and the three reports over a fixture world in the shapes GitLab documents."""

import io
import json
import subprocess
from datetime import datetime, timezone

import pytest
from conftest import FIXTURES, FakeRun, RouteHttp, World, response
from forge_tracker import cli, writes
from forge_tracker.forges import gitlab
from forge_tracker.model import ForgeError, attempt
from forge_tracker.transport import ReadTransport, glab_http, parse_included
from write_kits import GitLabWriteKit

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
BASE = "https://gitlab.example.org/api/v4"
P = "/api/v4/projects/acme%2Fwidgets"


def adapter(world, **kw):
    return gitlab.GitLab(ReadTransport(api_base=BASE, http=world), "acme/widgets", **kw)


def project_world():
    return World().route(P, {"id": 42, "default_branch": "main", "open_issues_count": 1})


# --- the glab transport -------------------------------------------------------------------------


def included(status, body, headers=()):
    head = [f"HTTP/2.0 {status} X"] + [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(head) + "\r\n\r\n" + body).encode()


def test_parse_included_reads_status_headers_and_body():
    raw = included(200, '[{"iid": 1}]', [("Link", '<https://x/a?page=2>; rel="next"')])
    parsed = parse_included(raw)
    assert parsed.status == 200 and parsed.body == b'[{"iid": 1}]'
    assert parsed.headers["link"] == '<https://x/a?page=2>; rel="next"'
    assert parse_included(b"glab: not logged in") is None


def test_glab_http_sends_a_path_under_the_default_base_and_a_full_url_otherwise():
    run = FakeRun()
    run.answer(["glab", "api"], stdout=included(200, "[]"))
    http = glab_http("gitlab.example.org", BASE, run)
    http("GET", f"{BASE}/projects/acme%2Fwidgets/issues?state=opened", {}, None)
    http("GET", "https://gitlab.example.org/code/api/v4/user", {}, None)
    assert run.calls[0] == [
        "glab",
        "api",
        "--hostname",
        "gitlab.example.org",
        "--include",
        "--method",
        "GET",
        "projects/acme%2Fwidgets/issues?state=opened",
    ]
    assert run.calls[1][-1] == "https://gitlab.example.org/code/api/v4/user"
    assert run.kwargs[0]["stdin"] is subprocess.DEVNULL


def test_glab_http_refuses_anything_but_a_get():
    run = FakeRun()
    http = glab_http("gitlab.example.org", BASE, run)
    with pytest.raises(ForgeError, match="reads only"):
        http("POST", f"{BASE}/projects/1/issues", {}, b"{}")
    assert run.calls == []


def test_an_http_error_through_glab_names_the_status_and_message():
    run = FakeRun()
    run.answer(
        ["glab", "api"], 1, included(404, '{"message":"404 Project Not Found"}'), "glab: 404"
    )
    t = ReadTransport(api_base=BASE, http=glab_http("gitlab.example.org", BASE, run))
    read = attempt(adapter_from(t).repo)
    assert not read.ok and read.error.startswith("HTTP 404 from /api/v4/projects/")
    assert "404 Project Not Found" in read.error


def adapter_from(t):
    return gitlab.GitLab(t, "acme/widgets")


def test_a_glab_failure_without_a_response_is_redacted():
    run = FakeRun()
    token = "glpat-AbCdEfGhIjKlMnOpQrSt"
    run.answer(["glab", "api"], 1, b"", f"error: PRIVATE-TOKEN: {token} rejected; also {token}")
    t = ReadTransport(api_base=BASE, http=glab_http("gitlab.example.org", BASE, run))
    read = attempt(adapter_from(t).identity)
    assert not read.ok and token not in read.error and "***" in read.error
    assert read.error.startswith("glab api /api/v4/user failed:")


def test_glab_missing_is_a_named_failure():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    t = ReadTransport(api_base=BASE, http=glab_http("gitlab.example.org", BASE, missing))
    read = attempt(adapter_from(t).identity)
    assert not read.ok and "glab isn't on PATH" in read.error


# --- reads GitLab answers its own way -----------------------------------------------------------


@pytest.mark.parametrize(
    "detailed, has_conflicts, legacy, expected",
    [
        ("conflict", True, None, "yes"),
        ("mergeable", False, None, "no"),
        ("ci_must_pass", False, None, "no"),
        ("not_approved", True, None, "yes"),
        ("unchecked", False, None, "unknown"),
        ("checking", False, None, "unknown"),
        ("preparing", False, None, "unknown"),
        ("approvals_syncing", False, None, "unknown"),
        (None, False, None, "unknown"),
        (None, False, "can_be_merged", "no"),
        (None, False, "unchecked", "unknown"),
        ("mergeable", None, None, "unknown"),
    ],
)
def test_conflicts_trust_has_conflicts_only_once_checked(detailed, has_conflicts, legacy, expected):
    assert gitlab.conflicts(detailed, has_conflicts, legacy) == expected


@pytest.mark.parametrize(
    "status, expected",
    [
        ("success", "success"),
        ("failed", "failure"),
        ("running", "pending"),
        ("created", "pending"),
        ("manual", "pending"),
        ("canceled", "error"),
        ("skipped", None),
        (None, None),
    ],
)
def test_head_pipeline_in_the_reports_words(status, expected):
    assert gitlab.pipeline_state({"status": status} if status else None) == expected


def test_comments_come_from_note_events_on_issues_only():
    def note(iid, body, created, kind="Issue", system=False):
        return {
            "target_type": "Note",
            "created_at": created,
            "note": {
                "body": body,
                "noteable_type": kind,
                "noteable_iid": iid,
                "system": system,
                "created_at": created,
                "author": {"username": "project_42_bot_ab12"},
            },
        }

    world = project_world().route(
        f"{P}/events",
        [
            note(3, "new", "2026-10-11T00:00:00Z"),
            note(4, "the day before the window", "2026-10-09T12:00:00Z"),
            note(5, "on an MR", "2026-10-11T00:00:00Z", kind="MergeRequest"),
            note(6, "added ~1 label", "2026-10-11T00:00:00Z", system=True),
        ],
    )
    (comment,) = adapter(world).comments_since(T0)
    assert (comment.issue, comment.body, comment.author_is_bot) == (3, "new", True)
    url = world.calls[0][1]
    assert "target_type=note" in url and "after=2026-10-09" in url


def test_mentions_are_pending_todos_on_open_items_in_the_window():
    def todo(action, target_type, iid, state, created):
        return {
            "action_name": action,
            "target_type": target_type,
            "target": {"iid": iid, "title": f"t{iid}", "state": state},
            "target_url": f"https://x/{iid}",
            "created_at": created,
        }

    world = project_world().route(
        "/api/v4/todos",
        [
            todo("mentioned", "MergeRequest", 7, "opened", "2026-10-11T00:00:00Z"),
            todo("directly_addressed", "Issue", 3, "opened", "2026-10-12T00:00:00Z"),
            todo("assigned", "Issue", 4, "opened", "2026-10-12T00:00:00Z"),
            todo("mentioned", "Issue", 5, "closed", "2026-10-12T00:00:00Z"),
            todo("mentioned", "Issue", 6, "opened", "2026-10-01T00:00:00Z"),
        ],
    )
    mentions = adapter(world).mentions_since("me", T0)
    assert [(m.number, m.is_change) for m in mentions] == [(7, True), (3, False)]
    assert "project_id=42" in world.calls[-1][1]


def mr(iid, **kw):
    base = {
        "iid": iid,
        "title": f"change {iid}",
        "source_branch": f"feat/{iid}",
        "target_branch": "main",
        "draft": False,
        "author": {"username": "me"},
        "assignees": [],
        "reviewers": [],
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "merged_at": None,
        "merge_commit_sha": None,
        "detailed_merge_status": "mergeable",
        "has_conflicts": False,
        "project_id": 42,
    }
    base.update(kw)
    return base


def test_open_changes_read_each_mr_for_its_pipeline_and_review():
    world = (
        project_world()
        .route(f"{P}/merge_requests", [mr(5, detailed_merge_status="unchecked"), mr(6)])
        .route(
            f"{P}/merge_requests/5",
            mr(5, detailed_merge_status="mergeable", head_pipeline={"status": "running"}),
        )
        .route(f"{P}/merge_requests/6", mr(6, head_pipeline=None))
        .route(f"{P}/merge_requests/5/approvals", {"approvals_required": 1, "approved": True})
        .route(f"{P}/merge_requests/6/approvals", {"approvals_required": 0, "approved_by": []})
    )
    five, six = adapter(world).open_changes()
    # The MR's own read settled the merge status the list still had as unchecked.
    assert (five.conflicts, five.pipeline, five.review) == ("no", "pending", "approved")
    assert (six.conflicts, six.pipeline, six.review) == ("no", None, None)


def test_merged_since_keeps_the_window_and_closing_issues_in_this_project():
    world = (
        project_world()
        .route(
            f"{P}/merge_requests",
            [
                mr(9, merged_at="2026-10-12T00:00:00Z", merge_commit_sha="m9"),
                mr(8, merged_at="2026-10-01T00:00:00Z"),
            ],
        )
        .route(
            f"{P}/merge_requests/9/closes_issues",
            [
                {"iid": 2, "project_id": 42},
                {"iid": 3, "project_id": 77, "references": {"full": "acme/other#3"}},
            ],
        )
    )
    (nine,) = adapter(world).merged_since(T0)
    assert (nine.number, nine.merge_sha, nine.closing_issues) == (9, "m9", (2,))
    assert "state=merged" in world.calls[0][1] and "updated_after=" in world.calls[0][1]


def test_latest_run_maps_source_status_and_failed_jobs():
    world = (
        World()
        .route(
            f"{P}/pipelines",
            [
                {
                    "id": 100,
                    "status": "failed",
                    "source": "push",
                    "created_at": "2026-10-12T00:00:00Z",
                    "web_url": "https://x/-/pipelines/100",
                }
            ],
        )
        .route(
            f"{P}/pipelines/100/jobs",
            [
                {"id": 1, "name": "lint", "status": "success"},
                {"id": 2, "name": "test", "status": "failed"},
                {"id": 3, "name": "docs", "status": "failed", "allow_failure": True},
            ],
        )
    )
    run = adapter(world).latest_run("main", "push")
    assert (run.status, run.conclusion, run.failed_jobs) == ("completed", "failure", ("test",))
    assert "source=push" in world.calls[0][1] and "per_page=1" in world.calls[0][1]
    running = World().route(
        f"{P}/pipelines",
        [{"id": 101, "status": "running", "source": "push", "created_at": "2026-10-12T00:00:00Z"}],
    )
    run = adapter(running).latest_run("main", "push")
    assert (run.status, run.conclusion) == ("running", None)
    assert adapter(World().route(f"{P}/pipelines", [])).latest_run("main", "schedule") is None


def test_passed_on_retry_reads_every_attempt_by_job_id():
    world = (
        World()
        .route(f"{P}/pipelines", [{"id": 1}, {"id": 2}])
        .route(
            f"{P}/pipelines/1/jobs",
            [
                {"id": 11, "name": "e2e", "status": "failed"},
                {"id": 12, "name": "lint", "status": "success"},
                {"id": 13, "name": "e2e", "status": "success"},
            ],
        )
        .route(
            f"{P}/pipelines/2/jobs",
            [
                {"id": 23, "name": "e2e", "status": "failed"},
                {"id": 21, "name": "e2e", "status": "success"},
            ],
        )
    )
    assert adapter(world).passed_on_retry("main", T0) == {"e2e": 1}
    assert all("include_retried=true" in c[1] for c in world.calls if "/jobs" in c[1])


def issue(iid, labels=(), state="opened"):
    return {
        "iid": iid,
        "title": f"issue {iid}",
        "state": state,
        "labels": list(labels),
        "assignees": [],
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "project_id": 42,
    }


def linked(iid, link_type, state="opened", **kw):
    return dict(issue(iid, state=state), link_type=link_type, **kw)


def links_world():
    return (
        project_world()
        .route(f"{P}/issues", [issue(2), issue(3, labels=["blocked"]), issue(5)])
        .route(f"{P}/issues/2/links", [linked(1, "is_blocked_by"), linked(3, "relates_to")])
        .route(f"{P}/issues/3/links", [linked(2, "relates_to")])
        .route(
            f"{P}/issues/5/links",
            [
                linked(
                    8,
                    "is_blocked_by",
                    state="closed",
                    closed_at="2026-10-09T00:00:00Z",
                    project_id=77,
                    references={"full": "acme/meta#8"},
                ),
                linked(6, "blocks"),
            ],
        )
    )


def test_native_links_only_without_the_convention():
    links = adapter(links_world()).blocking_links()
    assert [(l.blocked, l.blocker, l.kind, l.blocker_open, l.blocker_project) for l in links] == [
        (2, 1, "native", True, None),
        (5, 8, "native", False, "acme/meta"),
    ]


def test_the_convention_counts_relates_to_from_an_issue_with_the_blocked_label():
    links = adapter(links_world(), blocking="relates_to+blocked").blocking_links()
    assert [(l.blocked, l.blocker, l.kind) for l in links] == [
        (2, 1, "native"),
        (3, 2, "convention"),
        (5, 8, "native"),
    ]


def test_an_unknown_blocking_value_is_refused():
    with pytest.raises(ForgeError, match="blocking"):
        adapter(World(), blocking="relates_to")


def test_closing_refs_come_from_each_open_issue_closed_by():
    world = (
        project_world()
        .route(f"{P}/issues", [issue(14), issue(15)])
        .route(
            f"{P}/issues/14/closed_by",
            [
                mr(31, state="merged", merged_at="2026-10-10T00:00:00Z", merge_commit_sha="abc"),
                mr(5, state="opened", project_id=77, references={"full": "acme/fork!5"}),
            ],
        )
        .route(f"{P}/issues/15/closed_by", [])
    )
    refs = adapter(world).closing_refs()
    assert [(r.issue, r.change, r.state, r.change_project, r.merge_sha) for r in refs] == [
        (14, 31, "merged", None, "abc"),
        (14, 5, "open", "acme/fork", None),
    ]


def test_branches_compare_from_the_default_branch():
    world = (
        World()
        .route(
            f"{P}/repository/branches",
            [
                {"name": "main", "commit": {"id": "m"}},
                {"name": "feat/5", "commit": {"id": "a"}},
                {"name": "wip", "commit": {"id": "w"}, "protected": True},
            ],
        )
        .route(
            f"{P}/repository/compare",
            {
                "commits": [
                    {"committed_date": "2026-10-02T00:00:00.000+00:00"},
                    {"committed_date": "2026-10-01T00:00:00.000+00:00"},
                ]
            },
        )
    )
    changes = [gitlab._change(mr(5))]
    feat, wip = adapter(world).branches("main", changes)
    assert (feat.has_change, feat.ahead_by) == (True, None)
    assert (wip.ahead_by, wip.protected) == (2, True)
    assert wip.head_date == datetime(2026, 10, 2, tzinfo=timezone.utc)
    compare = [c[1] for c in world.calls if "compare" in c[1]]
    assert compare == [f"{BASE}/projects/acme%2Fwidgets/repository/compare?from=main&to=wip"]


def test_on_branch_reads_the_branches_holding_the_commit():
    world = World().route(f"{P}/repository/commits/abc/refs", [{"type": "branch", "name": "main"}])
    assert adapter(world).on_branch("main", "abc") is True
    assert adapter(world).on_branch("release", "abc") is False


def test_label_events_and_issue_comments():
    world = (
        World()
        .route(
            f"{P}/issues/7/resource_label_events",
            [
                {
                    "action": "add",
                    "label": {"name": "needs-human"},
                    "created_at": "2026-10-12T00:00:00Z",
                },
                {"action": "remove", "label": None, "created_at": "2026-10-13T00:00:00Z"},
            ],
        )
        .route(
            f"{P}/issues/7/notes",
            [
                {
                    "body": "Ruling (2026-10-14): yes",
                    "created_at": "2026-10-14T00:00:00Z",
                    "author": {"username": "me"},
                },
                {
                    "body": "changed the description",
                    "system": True,
                    "created_at": "2026-10-14T00:00:00Z",
                },
            ],
        )
    )
    a = adapter(world)
    assert [(e.event, e.label) for e in a.label_events(7)] == [
        ("labeled", "needs-human"),
        ("unlabeled", ""),
    ]
    assert [c.body for c in a.issue_comments(7)] == ["Ruling (2026-10-14): yes"]


def test_fingerprint_reads_heads_counts_and_each_open_mr_head():
    world = (
        World()
        .route(P, {"id": 42, "default_branch": "main", "open_issues_count": 3})
        .route(f"{P}/repository/branches", [{"name": "main", "commit": {"id": "abc"}}])
        .route(f"{P}/issues", [{"updated_at": "2026-10-14T00:00:00Z"}])
        .route(
            f"{P}/merge_requests", [{"iid": 4, "sha": "d4", "updated_at": "2026-10-13T00:00:00Z"}]
        )
        .route(
            f"{P}/merge_requests/4", {"iid": 4, "sha": "d4", "head_pipeline": {"status": "running"}}
        )
        .route(f"{P}/milestones", [{"id": 31, "title": "Nov", "due_date": None, "state": "active"}])
    )
    assert adapter(world).fingerprint_inputs() == {
        "default_branch": "main",
        "default_head": "abc",
        "open_issues": 3,
        "open_changes": 1,
        "newest_issue": "2026-10-14T00:00:00Z",
        "newest_change": "2026-10-13T00:00:00Z",
        "milestones": [["31", "Nov", "", "active"]],
        "branch_heads": {"main": "abc"},
        "change_heads": {"4": ["d4", "pending"]},
    }


def test_project_path_keeps_subgroups_in_one_segment():
    assert gitlab.project_path("group/sub/project") == "/projects/group%2Fsub%2Fproject"


def test_page_cap_through_x_next_page_alone():
    def endless(url):
        return response([issue(1)], headers={"X-Next-Page": "2"})

    world = World().route(f"{P}/issues", endless)
    t = ReadTransport(api_base=BASE, http=world, page_cap=3)
    read = attempt(gitlab.GitLab(t, "acme/widgets").open_issues)
    assert not read.ok and "page cap of 3 pages hit" in read.error


# --- the reports over the fixture world ---------------------------------------------------------

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
WORLD = FIXTURES / "gitlab"
LAB_BASE = "https://gitlab.example.org/code/api/v4"
P_LAB = "/projects/acme%2Fplatform%2Fwidgets"


@pytest.fixture
def lab(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "forge-tracker.local.md").write_text((WORLD / "config.md").read_text())
    environ = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path)}
    origin = (WORLD / "origin.txt").read_text().strip()

    def run_cli(*args, run=None, extra=None, stdin=None):
        """`extra(url)` answers a GET before the world does, when it returns a response."""
        http = RouteHttp(WORLD / "world.json")

        def served(method, url, headers, body=None):
            return (extra and extra(url)) or http(method, url, headers, body)

        out = io.StringIO()
        code = cli.main(
            ["--repo", str(repo), *args],
            http=served if extra else http,
            run=run or FakeRun(origin=origin),
            now=NOW,
            environ=environ,
            out=out,
            tz=timezone.utc,
            stdin=io.StringIO(stdin) if stdin is not None else None,
        )
        return code, out.getvalue(), http

    return run_cli


@pytest.mark.parametrize(
    "args, golden",
    [
        (("catchup", "--since", "3d"), "catchup.txt"),
        (("next",), "next.txt"),
        (("groom",), "groom.txt"),
        (("smoke",), "smoke.txt"),
    ],
)
def test_reports_over_the_fixture_world_match_their_goldens(lab, args, golden):
    code, text, http = lab(*args)
    assert code == 0
    assert text == (WORLD / golden).read_text()
    # Every request went under the configured api_base (the subpath), and only as a GET.
    assert all(c[0] == "GET" and c[1].startswith(f"{http.base}/") for c in http.calls)


def test_groom_apply_on_gitlab_writes_through_glab(lab):
    """The approved proposals go out through `glab api` with explicit methods, under the
    configured api_base, and the reads they check first stay GETs on the read transport."""
    run = FakeRun(origin=(WORLD / "origin.txt").read_text().strip())
    run.answer(["glab", "api"], 0, "HTTP/1.1 200 OK\r\n\r\n{}", "")
    head = {"name": "old-spike", "commit": {"id": "c" * 40}, "protected": False}

    def extra(url):
        return response(head) if url.endswith(f"{P_LAB}/repository/branches/old-spike") else None

    approved = '{"approved": ["close #1", "labels #1 ruling", "delete-branch old-spike"]}'
    code, text, http = lab("groom", "apply", run=run, extra=extra, stdin=approved)
    assert code == 0, text
    assert text == (
        "close #1: commented on and closed #1\n"
        "labels #1 ruling: #1: added decided; removed needs-human\n"
        "delete-branch old-spike: deleted branch old-spike (was ccccccc)\n"
    )
    sent = [
        (argv[6], argv[7].removeprefix(LAB_BASE), kw.get("input"))
        for argv, kw in zip(run.calls, run.kwargs)
        if argv[:2] == ["glab", "api"]
    ]
    assert [(method, path) for method, path, _ in sent] == [
        ("POST", f"{P_LAB}/issues/1/notes"),
        ("PUT", f"{P_LAB}/issues/1"),
        ("PUT", f"{P_LAB}/issues/1"),
        ("DELETE", f"{P_LAB}/repository/branches/old-spike"),
    ]
    assert json.loads(sent[1][2]) == {"state_event": "close"}
    assert json.loads(sent[2][2]) == {"add_labels": "decided", "remove_labels": "needs-human"}
    assert sent[3][2] is None
    assert all(c[0] == "GET" for c in http.calls)


# --- writes GitLab makes its own way (the shared ones are in test_contract.py) ------------------

CONVENTION = {"blocking": "relates_to+blocked", "labels": {"blocked": "blocked"}}
WK = GitLabWriteKit()


def relates(number):
    return {
        "target_project_id": "acme/widgets",
        "target_issue_iid": number,
        "link_type": "relates_to",
    }


def test_convention_link_add_writes_a_relates_to_link_and_the_blocked_label(tmp_path):
    world = WK.world()
    WK.blocked(world, 70, [], link_type="relates_to")
    WK.issue(world, 70)
    WK.issue(world, 71)
    result = WK.writer(world, tmp_path, **CONVENTION).apply(
        "link-add", {"issue": 70, "blocker": 71}
    )
    assert result.detail == (
        "#70: now blocked by #71 (a relates_to link and the blocked label, the configured "
        "convention)"
    )
    assert [(m, p.removeprefix(WK.W), b) for m, p, b in world.sent] == [
        ("POST", "/issues/70/links", relates(71)),
        ("PUT", "/issues/70", {"add_labels": "blocked"}),
    ]


def test_convention_link_add_keeps_a_relates_to_link_already_there(tmp_path):
    """Without the label the relates_to link doesn't count as blocking yet; the label makes it."""
    world = WK.world()
    WK.blocked(world, 70, [(71, "open", None)], link_type="relates_to")
    WK.issue(world, 70)
    result = WK.writer(world, tmp_path, **CONVENTION).apply(
        "link-add", {"issue": 70, "blocker": 71}
    )
    assert result.ok, result.detail
    assert "(the blocked label, the configured convention)" in result.detail
    assert [(m, p.removeprefix(WK.W)) for m, p, _ in world.sent] == [("PUT", "/issues/70")]


def test_convention_link_remove_takes_the_label_off_with_the_last_link(tmp_path):
    world = WK.world()
    WK.blocked(world, 70, [(71, "open", None)], link_type="relates_to")
    WK.issue(world, 70, labels=("blocked",))
    result = WK.writer(world, tmp_path, **CONVENTION).apply(
        "link-remove", {"issue": 70, "blocker": 71}
    )
    assert result.detail == "#70: no longer blocked by #71; removed blocked, its last blocker"
    assert [(m, p.removeprefix(WK.W), b) for m, p, b in world.sent] == [
        ("DELETE", "/issues/70/links/571", None),
        ("PUT", "/issues/70", {"remove_labels": "blocked"}),
    ]


def test_convention_link_remove_keeps_the_label_while_another_blocker_is_linked(tmp_path):
    world = WK.world()
    WK.blocked(world, 70, [(71, "open", None), (72, "open", None)], link_type="relates_to")
    WK.issue(world, 70, labels=("blocked",))
    result = WK.writer(world, tmp_path, **CONVENTION).apply(
        "link-remove", {"issue": 70, "blocker": 71}
    )
    assert result.detail == "#70: no longer blocked by #71"
    assert [m for m, _, _ in world.sent] == ["DELETE"]


def _native_link_add(tmp_path, answer):
    world = WK.world()
    WK.blocked(world, 70, [])
    WK.issue(world, 70)
    WK.issue(world, 71)
    world.answers[("POST", f"{WK.W}/issues/70/links")] = answer
    result = WK.writer(world, tmp_path).apply("link-add", {"issue": 70, "blocker": 71})
    return result, world


def test_a_native_link_on_a_tier_without_blocks_is_unsupported(tmp_path):
    result, world = _native_link_add(
        tmp_path, (403, {"message": "Blocked issues not available for current license"})
    )
    assert result.status == "unsupported"
    assert "has no blocking links" in result.detail and "relates_to+blocked" in result.detail
    assert len(world.sent) == 1


def test_a_relates_to_link_made_in_place_of_a_native_one_is_taken_back(tmp_path):
    """GitLab CE ignores link_type and makes a relates_to link; the write takes it back and says
    blocking links are unsupported there."""
    world = WK.world()
    WK.issue(world, 70)
    WK.issue(world, 71)
    world.answers[("POST", f"{WK.W}/issues/70/links")] = (201, {"link_type": "relates_to"})
    stray = [
        {
            "iid": 71,
            "issue_link_id": 571,
            "project_id": 42,
            "references": {"full": "acme/widgets#71"},
            "state": "opened",
            "link_type": "relates_to",
        }
    ]
    world.route(f"{WK.P}/issues/70/links", lambda url: response(stray if world.sent else []))
    result = WK.writer(world, tmp_path).apply("link-add", {"issue": 70, "blocker": 71})
    assert result.status == "unsupported"
    assert result.detail.endswith("the relates_to link it made instead was removed")
    assert [(m, p.removeprefix(WK.W)) for m, p, _ in world.sent] == [
        ("POST", "/issues/70/links"),
        ("DELETE", "/issues/70/links/571"),
    ]
    assert writes.render(result).startswith("link-add: UNSUPPORTED (this GitLab has no")


def test_a_label_with_a_comma_is_refused_before_anything_is_sent(tmp_path):
    world = WK.world()
    WK.issue(world, 22)
    result = WK.writer(world, tmp_path).apply("labels", {"issue": 22, "add": ["a,b"]})
    assert result.status == "refused" and "comma" in result.detail
    assert world.sent == []


def test_a_blocker_in_a_subgroup_project_is_accepted_on_gitlab(tmp_path):
    world = WK.world()
    WK.blocked(world, 70, [])
    WK.issue(world, 70)
    WK.issue(world, 4, project="acme/sub/other")
    payload = cli.link_payload("70", "acme/sub/other#4")
    result = WK.writer(world, tmp_path).apply("link-add", payload)
    assert result.detail == "#70: now blocked by acme/sub/other#4"
    assert world.sent[0][2]["target_project_id"] == "acme/sub/other"


def test_glab_writes_name_the_method_and_send_the_body_on_stdin():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"HTTP/1.1 201 Created\r\n\r\n{}", b"")

    http = writes.glab_write_http("gitlab.example.org", BASE, run)
    http("PUT", f"{BASE}/projects/acme%2Fwidgets/issues/3", {}, b'{"state_event": "close"}')
    http("DELETE", "https://other.example.org/api/v4/projects/1/issues/3/links/9", {}, None)
    (put, put_kw), (delete, delete_kw) = calls
    assert put == [
        "glab", "api", "--hostname", "gitlab.example.org", "--include", "--method", "PUT",
        "projects/acme%2Fwidgets/issues/3",
        "--header", "Content-Type: application/json", "--input", "-",
    ]  # fmt: skip
    assert put_kw["input"] == b'{"state_event": "close"}'
    assert delete[6:] == ["DELETE", "https://other.example.org/api/v4/projects/1/issues/3/links/9"]
    assert delete_kw["stdin"] == subprocess.DEVNULL and "input" not in delete_kw
    with pytest.raises(ForgeError, match="refused"):
        http("GET", f"{BASE}/projects/1", {}, None)
    assert len(calls) == 2


def test_glab_missing_or_failing_before_a_response_is_a_failure():
    def missing(argv, **kwargs):
        raise FileNotFoundError

    with pytest.raises(ForgeError, match="glab isn't on PATH"):
        writes.glab_write_http("h", BASE, missing)("POST", f"{BASE}/x", {}, b"{}")

    def broken(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", "error: glpat-AbCdEfGhIjKlMnOpQrSt bad")

    with pytest.raises(ForgeError) as raised:
        writes.glab_write_http("h", BASE, broken)("POST", f"{BASE}/x", {}, b"{}")
    assert "glpat-" not in str(raised.value) and "***" in str(raised.value)
