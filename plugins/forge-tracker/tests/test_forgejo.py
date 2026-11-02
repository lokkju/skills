"""The Forgejo adapter beyond the shared contract (test_contract.py): auth and paging, the reads
Forgejo answers its own way (per-PR status and reviews, closing keywords, dependencies, the
timeline, the runs API or its absence), detection by tea login or probe, and the reports over a
fixture world in the shapes Forgejo documents, on an instance with no Actions runs API."""

import io
from datetime import datetime, timezone

import pytest
from conftest import FIXTURES, FakeRun, RouteHttp, World, response
from forge_tracker import cli, refs
from forge_tracker.forges import forgejo
from forge_tracker.model import attempt
from forge_tracker.transport import ReadTransport
from kits import FORGEJO_TOKEN

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
BASE = "https://code.example.org/api/v1"
R = "/api/v1/repos/acme/widgets"


def adapter(world, page_cap=50):
    t = ReadTransport(
        api_base=BASE,
        http=world,
        token=FORGEJO_TOKEN,
        page_cap=page_cap,
        auth_scheme=forgejo.AUTH_SCHEME,
        page_size_param="limit",
        page_size=forgejo.PAGE_SIZE,
    )
    return forgejo.Forgejo(t, "acme/widgets")


def test_requests_send_the_token_scheme_and_page_by_limit():
    world = World().route(f"{R}/labels", [{"name": "bug", "description": "x"}])
    assert [label.name for label in adapter(world).labels()] == ["bug"]
    _, url, headers, _ = world.calls[0]
    assert headers["Authorization"] == f"token {FORGEJO_TOKEN}"
    assert "limit=50" in url and "per_page" not in url


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Closes #4", [4]),
        ("fixes acme/widgets#5 and resolves #6, #7", [5, 6, 7]),
        ("Fixed: #8\nCloses acme/other#9", [8]),
        ("closes #3 twice: Closes #3", [3]),
        ("see #4; `closes #5` is an example\n```\nfixes #6\n```", []),
        ("prefixes #4, disclosed #5", []),
    ],
)
def test_closing_keywords(body, expected):
    assert refs.closing_numbers(body, "acme/widgets") == expected


def pr(number, **kw):
    base = {
        "number": number,
        "title": f"change {number}",
        "body": "",
        "state": "open",
        "draft": False,
        "mergeable": True,
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "user": {"login": "me"},
        "assignees": None,
        "requested_reviewers": None,
        "head": {"ref": f"feat/{number}", "sha": f"h{number}"},
        "base": {"ref": "main"},
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
    }
    base.update(kw)
    return base


def test_open_changes_read_status_and_reviews_per_pr():
    world = (
        World()
        .route(
            f"{R}/pulls",
            [pr(5, title="WIP: half done", requested_reviewers=[{"login": "me"}]), pr(6)],
        )
        .route(f"{R}/commits/h5/status", {"state": "warning", "total_count": 1})
        .route(f"{R}/commits/h6/status", {"state": "", "total_count": 0, "statuses": []})
        .route(f"{R}/pulls/5/reviews", [])
        .route(
            f"{R}/pulls/6/reviews",
            [
                {"id": 1, "user": {"login": "a"}, "state": "APPROVED"},
                {"id": 2, "user": {"login": "b"}, "state": "REQUEST_CHANGES"},
                {"id": 3, "user": {"login": "c"}, "state": "REQUEST_CHANGES", "dismissed": True},
            ],
        )
    )
    five, six = adapter(world).open_changes()
    assert (five.draft, five.pipeline, five.review, five.reviewers) == (
        True,
        "success",
        "review_required",
        ("me",),
    )
    assert (six.draft, six.pipeline, six.review) == (False, None, "changes_requested")


def test_merged_since_stops_at_the_window_and_reads_closing_keywords():
    world = World().route(
        f"{R}/pulls",
        [
            pr(
                9,
                state="closed",
                merged=True,
                merged_at="2026-10-12T00:00:00Z",
                merge_commit_sha="m9",
                body="Closes #2",
                updated_at="2026-10-12T00:00:01Z",
            ),
            pr(8, state="closed", merged=False, updated_at="2026-10-11T00:00:00Z"),
            pr(
                7,
                state="closed",
                merged=True,
                merged_at="2026-10-01T00:00:00Z",
                updated_at="2026-10-02T00:00:00Z",
            ),
            pr(
                6,
                state="closed",
                merged=True,
                merged_at="2026-10-11T00:00:00Z",
                updated_at="2026-10-11T00:00:00Z",
            ),
        ],
    )
    merged = adapter(world).merged_since(T0)
    # #6 sits after #7, which was updated before the window, so the read had already stopped.
    assert [(c.number, c.merge_sha, c.closing_issues) for c in merged] == [(9, "m9", (2,))]
    assert "sort=recentupdate" in world.calls[0][1] and "state=closed" in world.calls[0][1]


def test_closing_refs_read_every_pr_body_against_the_open_issues():
    world = (
        World()
        .route(
            f"{R}/issues",
            [
                {
                    "number": 4,
                    "title": "x",
                    "state": "open",
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-01T00:00:00Z",
                }
            ],
        )
        .route(
            f"{R}/pulls",
            [
                pr(7, body="Closes #4"),
                pr(
                    5,
                    body="Fixes #4",
                    state="closed",
                    merged=True,
                    merged_at="2026-10-10T00:00:00Z",
                    merge_commit_sha="abc",
                ),
                pr(3, body="Closes #4", state="closed"),
                pr(2, body="Closes #99"),
            ],
        )
    )
    out = adapter(world).closing_refs()
    assert [(r.issue, r.change, r.state, r.merge_sha) for r in out] == [
        (4, 3, "closed", None),
        (4, 5, "merged", "abc"),
        (4, 7, "open", None),
    ]


def test_dependencies_are_native_links_with_their_project():
    world = (
        World()
        .route(
            f"{R}/issues",
            [
                {
                    "number": 22,
                    "title": "x",
                    "state": "open",
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-01T00:00:00Z",
                }
            ],
        )
        .route(
            f"{R}/issues/22/dependencies",
            [
                {"number": 21, "state": "open", "repository": {"full_name": "acme/widgets"}},
                {
                    "number": 4,
                    "state": "closed",
                    "closed_at": "2026-10-09T00:00:00Z",
                    "repository": {"full_name": "acme/meta"},
                },
            ],
        )
    )
    links = adapter(world).blocking_links()
    assert [(l.blocker, l.blocker_open, l.blocker_project) for l in links] == [
        (21, True, None),
        (4, False, "acme/meta"),
    ]
    assert links[1].blocker_closed == datetime(2026, 10, 9, tzinfo=timezone.utc)


def test_label_events_come_from_the_timeline():
    world = World().route(
        f"{R}/issues/7/timeline",
        [
            {
                "type": "label",
                "body": "1",
                "label": {"name": "needs-human"},
                "created_at": "2026-10-12T00:00:00Z",
            },
            {"type": "comment", "body": "hi", "created_at": "2026-10-12T01:00:00Z"},
            {
                "type": "label",
                "body": "",
                "label": {"name": "P1"},
                "created_at": "2026-10-13T00:00:00Z",
            },
        ],
    )
    out = adapter(world).label_events(7)
    assert [(e.event, e.label) for e in out] == [("labeled", "needs-human"), ("unlabeled", "P1")]


def test_mentions_keep_this_repo_only():
    world = World().route(
        "/api/v1/repos/issues/search",
        [
            {
                "number": 3,
                "title": "a",
                "updated_at": "2026-10-12T00:00:00Z",
                "pull_request": {"merged": False},
                "repository": {"full_name": "acme/widgets"},
            },
            {
                "number": 4,
                "title": "b",
                "updated_at": "2026-10-12T00:00:00Z",
                "pull_request": None,
                "repository": {"full_name": "acme/gadgets"},
            },
        ],
    )
    mentions = adapter(world).mentions_since("me", T0)
    assert [(m.number, m.is_change) for m in mentions] == [(3, True)]
    url = world.calls[0][1]
    assert "mentioned=true" in url and "owner=acme" in url and "state=open" in url


def runs(*items):
    return {"workflow_runs": list(items), "total_count": len(items)}


def test_forgejo_shaped_runs_map_status_to_a_conclusion():
    world = (
        World()
        .route(
            f"{R}/actions/runs",
            runs(
                {
                    "id": 3,
                    "title": "CI",
                    "trigger_event": "push",
                    "prettyref": "other",
                    "status": "success",
                    "created": "2026-10-12T00:00:00Z",
                },
                {
                    "id": 2,
                    "title": "CI",
                    "trigger_event": "push",
                    "prettyref": "main",
                    "status": "failure",
                    "created": "2026-10-11T00:00:00Z",
                },
            ),
        )
        .route(f"{R}/actions/runs/2/jobs", {"message": "Not Found"}, status=404)
    )
    run = adapter(world).latest_run("main", "push")
    assert (run.id, run.status, run.conclusion, run.failed_jobs) == (2, "completed", "failure", ())


def test_without_a_runs_api_push_falls_back_to_the_commit_status():
    world = (
        World()
        .route(f"{R}/actions/runs", {"message": "Not Found"}, status=404)
        .route(
            f"{R}/commits/main/status",
            {
                "state": "failure",
                "sha": "abcdef0123",
                "total_count": 2,
                "statuses": [
                    {
                        "context": "ci/lint",
                        "status": "success",
                        "updated_at": "2026-10-12T00:00:00Z",
                    },
                    {"context": "ci/test", "status": "error", "updated_at": "2026-10-12T00:05:00Z"},
                ],
            },
        )
    )
    a = adapter(world)
    run = a.latest_run("main", "push")
    assert (run.id, run.name, run.conclusion, run.failed_jobs) == (
        0,
        "commit status of abcdef0",
        "failure",
        ("ci/test",),
    )
    schedule = attempt(lambda: a.latest_run("main", "schedule"))
    flaky = attempt(lambda: a.passed_on_retry("main", T0))
    assert schedule.unsupported and not schedule.ok and "no Actions runs API" in schedule.error
    assert flaky.unsupported and not flaky.ok


def test_passed_on_retry_needs_numbered_attempts():
    world = (
        World()
        .route(
            f"{R}/actions/runs",
            runs(
                {
                    "id": 1,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 2,
                    "created_at": "2026-10-12T00:00:00Z",
                },
                {
                    "id": 9,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                    "created_at": "2026-10-01T00:00:00Z",
                },
            ),
        )
        .route(
            f"{R}/actions/runs/1/jobs",
            {
                "jobs": [
                    {"name": "e2e", "conclusion": "failure", "run_attempt": 1},
                    {"name": "e2e", "conclusion": "success", "run_attempt": 2},
                ]
            },
        )
    )
    assert adapter(world).passed_on_retry("main", T0) == {"e2e": 1}
    forgejo_runs = World().route(
        f"{R}/actions/runs",
        runs(
            {"id": 1, "prettyref": "main", "status": "success", "created": "2026-10-12T00:00:00Z"}
        ),
    )
    read = attempt(lambda: adapter(forgejo_runs).passed_on_retry("main", T0))
    assert read.unsupported and "doesn't number retried attempts" in read.error


def test_compare_drives_branches_and_on_branch():
    world = (
        World()
        .route(
            f"{R}/branches",
            [
                {"name": "main", "commit": {"id": "m"}},
                {"name": "feat/x", "commit": {"id": "f"}, "protected": True},
            ],
        )
        .route(
            f"{R}/compare/main...feat/x",
            {
                "total_commits": 1,
                "commits": [{"commit": {"committer": {"date": "2026-10-02T00:00:00Z"}}}],
            },
        )
        .route(f"{R}/compare/main...abc", {"total_commits": 0, "commits": []})
    )
    (branch,) = adapter(world).branches("main", [])
    assert (branch.name, branch.ahead_by, branch.protected) == ("feat/x", 1, True)
    assert branch.head_date == datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert adapter(world).on_branch("main", "abc") is True


def test_a_page_cap_through_link_headers():
    def endless(url):
        return response(
            [], headers={"Link": f'<{BASE}/repos/acme/widgets/labels?page=2>; rel="next"'}
        )

    read = attempt(adapter(World().route(f"{R}/labels", endless), page_cap=2).labels)
    assert not read.ok and "page cap of 2 pages hit" in read.error


# --- opening a Forgejo repo: token, API base, probe -----------------------------------------------


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "repo"
    (directory / ".claude").mkdir(parents=True)
    return directory


def run_smoke(repo, environ, http, origin="https://code.example.org/acme/widgets.git"):
    out = io.StringIO()
    code = cli.main(
        ["--repo", str(repo), "smoke"],
        http=http,
        run=FakeRun(origin=origin),
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
        environ=environ,
        out=out,
    )
    return code, out.getvalue()


def test_no_token_is_a_usage_error_that_says_how_to_log_in(repo, tmp_path, capsys):
    (repo / ".claude" / "forge-tracker.local.md").write_text("---\nforge: forgejo\n---\n")
    code, _ = run_smoke(
        repo, {"XDG_CACHE_HOME": str(tmp_path / "c"), "HOME": str(tmp_path)}, World()
    )
    assert code == 2
    assert "no Forgejo token for code.example.org" in capsys.readouterr().err


def test_the_tea_login_gives_the_token_and_a_subpath_api_base(repo, tmp_path):
    (tmp_path / "tea").mkdir()
    (tmp_path / "tea" / "config.yml").write_text(
        "logins:\n- name: work\n  url: https://code.example.org/forgejo\n  token: tea-tok-123\n"
    )
    environ = {"XDG_CONFIG_HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    calls = []

    def forge(method, url, headers, body=None):
        # /user answers; every other read is a 404, which smoke reports as FAILED lines.
        calls.append((url, dict(headers)))
        if url.endswith("/user"):
            return response({"login": "me"})
        return response({"message": "Not Found"}, status=404)

    code, text = run_smoke(
        repo, environ, forge, origin="https://code.example.org/forgejo/acme/widgets.git"
    )
    # Detected by the tea login, with no probe, and the subpath remote read as owner/repo.
    assert code == 1 and "identity: me (bot: no)" in text
    assert calls[0][0] == "https://code.example.org/forgejo/api/v1/user"
    assert all(h["Authorization"] == "token tea-tok-123" for _, h in calls)
    assert any(u.startswith(f"https://code.example.org/forgejo/api/v1{R[7:]}") for u, _ in calls)
    assert not any(u.endswith("/version") for u, _ in calls)


def test_the_probe_detects_forgejo_and_is_cached_for_a_day(tmp_path):
    cache = {"XDG_CACHE_HOME": str(tmp_path)}
    world = World().route("/api/v1/version", {"version": "11.0.1+gitea-1.22.0"})
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    probe = cli.forgejo_probe(world, cache, now)
    assert probe("code.example.org") is True
    assert world.calls[0][1] == "https://code.example.org/api/v1/version"
    assert "Authorization" not in world.calls[0][2]
    assert probe("code.example.org") is True and len(world.calls) == 1
    later = cli.forgejo_probe(world, cache, datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert later("code.example.org") is True and len(world.calls) == 2
    not_forgejo = World().route("/api/v1/version", {"message": "nope"}, status=404)
    assert cli.forgejo_probe(not_forgejo, cache, now)("git.other.test") is False


def test_a_probe_that_cant_connect_is_no_answer_and_isnt_cached(tmp_path):
    def down(method, url, headers, body=None):
        raise OSError("connection refused")

    assert cli.forgejo_probe(down, {"XDG_CACHE_HOME": str(tmp_path)})("x.test") is False
    assert not (tmp_path / "forge-tracker" / "hosts.json").exists()


# --- the reports over the fixture world ---------------------------------------------------------

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
WORLD = FIXTURES / "forgejo"


@pytest.fixture
def lab(repo, tmp_path):
    (repo / ".claude" / "forge-tracker.local.md").write_text((WORLD / "config.md").read_text())
    environ = {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "HOME": str(tmp_path),
        "FORGEJO_TOKEN": FORGEJO_TOKEN,
    }
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
    # Unsupported reads (no runs API here) don't fail the run.
    assert code == 0
    assert text == (WORLD / golden).read_text()
    assert all(c[0] == "GET" and c[1].startswith(f"{http.base}/") for c in http.calls)
    assert all(c[2]["Authorization"] == f"token {FORGEJO_TOKEN}" for c in http.calls)


def test_catchup_json_lists_unsupported_parts_apart_from_failures(lab):
    import json

    code, text, _ = lab("catchup", "--since", "3d", "--json")
    report = json.loads(text)
    assert code == 0 and report["ok"] is True and report["failed"] == []
    assert [u["part"] for u in report["health"]["unsupported"]] == ["schedule_run", "flaky"]
