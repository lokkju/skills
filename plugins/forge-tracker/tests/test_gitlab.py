"""The GitLab adapter beyond the shared contract (test_contract.py): the glab transport, the
reads GitLab answers its own way (note events, to-do items, per-MR detail, retried jobs, closing
MRs), and the three reports over a fixture world in the shapes GitLab documents."""

import io
import subprocess
from datetime import datetime, timezone

import pytest
from conftest import FIXTURES, FakeRun, RouteHttp, World, response
from forge_tracker import cli
from forge_tracker.forges import gitlab
from forge_tracker.model import ForgeError, attempt
from forge_tracker.transport import ReadTransport, glab_http, parse_included

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


@pytest.fixture
def lab(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "forge-tracker.local.md").write_text((WORLD / "config.md").read_text())
    environ = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path)}
    origin = (WORLD / "origin.txt").read_text().strip()

    def run_cli(*args):
        http = RouteHttp(WORLD / "world.json")
        out = io.StringIO()
        code = cli.main(
            ["--repo", str(repo), *args],
            http=http,
            run=FakeRun(origin=origin),
            now=NOW,
            environ=environ,
            out=out,
            tz=timezone.utc,
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


def test_groom_apply_on_gitlab_refuses_without_a_wrapper(lab, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"approved": ["close #1"]}'))
    code, text, http = lab("groom", "apply")
    assert code == 1
    assert "REFUSED (forge-tracker writes only to GitHub so far, not gitlab" in text
    assert all(c[0] == "GET" for c in http.calls)
