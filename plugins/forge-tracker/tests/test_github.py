"""The contract suite for the GitHub adapter. Payload shapes follow the recorded responses in
fixtures/github/; the edge cases (conflicts, retries, bots, failures) are set up here because a
live repo rarely has all of them at once."""

from datetime import datetime, timezone

import pytest
from conftest import World, response
from forge_tracker.forges import github
from forge_tracker.model import ForgeError, attempt

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)


def issue(number, **kw):
    base = {
        "number": number,
        "title": f"issue {number}",
        "state": "open",
        "labels": [],
        "assignees": [],
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "closed_at": None,
        "milestone": None,
        "user": {"login": "someone", "type": "User"},
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }
    base.update(kw)
    return base


def pr_node(number, **kw):
    base = {
        "number": number,
        "title": f"change {number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "headRefName": f"feat/{number}",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-10-11T00:00:00Z",
        "updatedAt": "2026-10-12T00:00:00Z",
        "mergedAt": None,
        "author": {"__typename": "User", "login": "me"},
        "mergeCommit": None,
        "assignees": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "commits": {
            "nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {"state": "SUCCESS"}}}]
        },
    }
    base.update(kw)
    return base


def connection(nodes, key="pullRequests"):
    return {
        "repository": {key: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
    }


def adapter(world, transport_for, page_cap=50):
    return github.GitHub(transport_for(world, page_cap), "acme/widgets")


def test_endpoints():
    assert github.endpoints("github.com") == (
        "https://api.github.com",
        "https://api.github.com/graphql",
    )
    assert github.endpoints("git.corp") == (
        "https://git.corp/api/v3",
        "https://git.corp/api/graphql",
    )
    assert github.endpoints("x", "https://x/api/v3/") == (
        "https://x/api/v3",
        "https://x/api/graphql",
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("CONFLICTING", "yes"),
        (False, "yes"),
        ("MERGEABLE", "no"),
        (True, "no"),
        ("UNKNOWN", "unknown"),
        (None, "unknown"),
        ("SOMETHING_NEW", "unknown"),
    ],
)
def test_conflicts_are_three_valued(value, expected):
    assert github.conflicts(value) == expected


def test_identity_and_bot_detection(transport_for):
    world = World().route("/user", {"login": "me", "type": "User"})
    assert adapter(world, transport_for).identity().is_bot is False
    world = World().route("/user", {"login": "release[bot]", "type": "User"})
    assert adapter(world, transport_for).identity().is_bot is True
    world = World().route("/user", {"login": "ci", "type": "Bot"})
    assert adapter(world, transport_for).identity().is_bot is True


def test_issues_since_drops_pull_requests_and_maps_fields(transport_for):
    world = World().route(
        "/repos/acme/widgets/issues",
        [
            issue(
                1,
                labels=[{"name": "P1"}, {"name": "bug"}],
                assignees=[{"login": "me"}],
                milestone={"title": "Tier 1", "due_on": "2026-12-31T08:00:00Z"},
                state="closed",
                closed_at="2026-10-13T00:00:00Z",
            ),
            issue(2, pull_request={"url": "x"}),
        ],
    )
    issues = adapter(world, transport_for).issues_since(T0)
    assert [i.number for i in issues] == [1]
    one = issues[0]
    assert one.labels == ("P1", "bug") and one.assignees == ("me",)
    assert one.milestone.title == "Tier 1" and one.milestone.due == "2026-12-31T08:00:00Z"
    assert one.closed == datetime(2026, 10, 13, tzinfo=timezone.utc)
    url = world.calls[0][1]
    assert "state=all" in url and "since=2026-10-10T00:00:00Z" in url


def test_comments_since_keeps_only_comments_created_in_the_window(transport_for):
    world = World().route(
        "/repos/acme/widgets/issues/comments",
        [
            {
                "issue_url": "https://api.github.com/repos/acme/widgets/issues/7",
                "user": {"login": "me", "type": "User"},
                "created_at": "2026-10-01T00:00:00Z",
                "body": "old, edited later",
            },
            {
                "issue_url": "https://api.github.com/repos/acme/widgets/issues/8",
                "user": {"login": "yeet[bot]", "type": "Bot"},
                "created_at": "2026-10-11T00:00:00Z",
                "body": "new",
            },
        ],
    )
    comments = adapter(world, transport_for).comments_since(T0)
    assert [(c.issue, c.body, c.author_is_bot) for c in comments] == [(8, "new", True)]


def test_mentions_use_search_and_mark_changes(transport_for):
    world = World().route(
        "/search/issues",
        {
            "total_count": 2,
            "incomplete_results": False,
            "items": [
                issue(3, pull_request={"url": "x"}, updated_at="2026-10-12T00:00:00Z"),
                issue(4),
            ],
        },
    )
    mentions = adapter(world, transport_for).mentions_since("me", T0)
    assert [(m.number, m.is_change) for m in mentions] == [(3, True), (4, False)]
    assert "mentions:me" in world.calls[0][1] and "repo:acme/widgets" in world.calls[0][1]


def test_open_changes_map_reviewers_conflicts_and_pipeline(transport_for):
    world = World().op(
        "OpenChanges",
        connection(
            [
                pr_node(
                    5,
                    mergeable="UNKNOWN",
                    isDraft=True,
                    assignees={"nodes": [{"login": "me"}]},
                    reviewRequests={
                        "nodes": [
                            {"requestedReviewer": {"__typename": "User", "login": "me"}},
                            {"requestedReviewer": {"__typename": "Team", "slug": "core"}},
                        ]
                    },
                ),
                pr_node(6, mergeable="CONFLICTING", commits={"nodes": []}),
            ]
        ),
    )
    five, six = adapter(world, transport_for).open_changes()
    assert five.conflicts == "unknown" and five.draft and five.pipeline == "success"
    assert five.reviewers == ("me", "team:core") and five.assignees == ("me",)
    assert six.conflicts == "yes" and six.pipeline is None


def test_merged_since_stops_at_the_window_and_keeps_closing_refs(transport_for):
    nodes = [
        pr_node(
            9,
            mergedAt="2026-10-12T00:00:00Z",
            updatedAt="2026-10-12T00:00:01Z",
            mergeCommit={"oid": "m9"},
            closingIssuesReferences={
                "nodes": [
                    {"number": 2, "repository": {"nameWithOwner": "acme/widgets"}},
                    {"number": 3, "repository": {"nameWithOwner": "acme/other"}},
                ]
            },
        ),
        # Updated in the window but merged before it: dropped.
        pr_node(8, mergedAt="2026-10-01T00:00:00Z", updatedAt="2026-10-11T00:00:00Z"),
        # Updated before the window: the read stops here.
        pr_node(7, mergedAt="2026-10-01T00:00:00Z", updatedAt="2026-10-02T00:00:00Z"),
    ]

    def handler(variables):
        assert variables["cursor"] is None
        conn = connection(nodes)
        conn["repository"]["pullRequests"]["pageInfo"]["hasNextPage"] = True
        conn["repository"]["pullRequests"]["pageInfo"]["endCursor"] = "next"
        return conn

    world = World().op("MergedChanges", handler)
    merged = adapter(world, transport_for).merged_since(T0)
    assert [(c.number, c.merge_sha, c.closing_issues) for c in merged] == [(9, "m9", (2,))]
    assert len(world.calls) == 1


RUN = {
    "id": 100,
    "name": "CI",
    "event": "push",
    "status": "completed",
    "conclusion": "failure",
    "run_attempt": 1,
    "created_at": "2026-10-12T00:00:00Z",
    "html_url": "https://github.com/acme/widgets/actions/runs/100",
}


def test_latest_run_lists_failed_jobs(transport_for):
    world = (
        World()
        .route("/repos/acme/widgets/actions/runs", {"total_count": 1, "workflow_runs": [RUN]})
        .route(
            "/repos/acme/widgets/actions/runs/100/jobs",
            {
                "total_count": 2,
                "jobs": [
                    {"name": "lint", "conclusion": "success", "run_attempt": 1},
                    {"name": "test (3.14)", "conclusion": "failure", "run_attempt": 1},
                ],
            },
        )
    )
    run = adapter(world, transport_for).latest_run("main", "push")
    assert run.failed_jobs == ("test (3.14)",)
    assert "event=push" in world.calls[0][1] and "per_page=1" in world.calls[0][1]


def test_latest_run_none_and_passing_run_reads_no_jobs(transport_for):
    world = World().route("/repos/acme/widgets/actions/runs", {"workflow_runs": []})
    assert adapter(world, transport_for).latest_run("main", "schedule") is None
    world = World().route(
        "/repos/acme/widgets/actions/runs",
        {"workflow_runs": [dict(RUN, conclusion="success")]},
    )
    assert adapter(world, transport_for).latest_run("main", "push").failed_jobs == ()
    assert len(world.calls) == 1


def test_passed_on_retry_counts_jobs_that_failed_then_passed(transport_for):
    runs = [
        dict(RUN, id=1, run_attempt=2, conclusion="success"),
        dict(RUN, id=2, run_attempt=3, conclusion="success"),
        dict(RUN, id=3, run_attempt=1, conclusion="success"),
    ]
    jobs = {
        1: [
            {"name": "e2e", "conclusion": "failure", "run_attempt": 1},
            {"name": "e2e", "conclusion": "success", "run_attempt": 2},
            {"name": "lint", "conclusion": "success", "run_attempt": 1},
        ],
        2: [
            {"name": "e2e", "conclusion": "failure", "run_attempt": 1},
            {"name": "e2e", "conclusion": "failure", "run_attempt": 2},
            {"name": "e2e", "conclusion": "success", "run_attempt": 3},
        ],
    }
    world = World().route("/repos/acme/widgets/actions/runs", {"workflow_runs": runs})
    for run_id, job_list in jobs.items():
        world.route(f"/repos/acme/widgets/actions/runs/{run_id}/jobs", {"jobs": job_list})
    counts = adapter(world, transport_for).passed_on_retry("main", T0)
    assert counts == {"e2e": 2}
    assert all("runs/3/jobs" not in call[1] for call in world.calls)
    assert any("filter=all" in call[1] for call in world.calls)


def test_tags_prefer_dated_releases_and_note_registry(transport_for):
    world = (
        World()
        .route(
            "/repos/acme/widgets/releases",
            [
                {"tag_name": "v1.2.0", "published_at": "2026-10-01T00:00:00Z", "draft": False},
                {"tag_name": "v1.3.0", "published_at": "2026-10-09T00:00:00Z", "draft": False},
                {"tag_name": "v9.0.0", "published_at": "2026-10-10T00:00:00Z", "draft": True},
            ],
        )
        .route("/repos/acme/widgets/tags", [{"name": "nightly-5"}, {"name": "v1.3.0"}])
    )
    tags = adapter(world, transport_for).tags(("v*", "nightly-*", "x*", "registry:app"))
    assert [(t.pattern, t.name, t.note is not None) for t in tags] == [
        ("v*", "v1.3.0", False),
        ("nightly-*", "nightly-5", False),
        ("x*", None, False),
        ("registry:app", None, True),
    ]


def test_branches_compare_only_branches_without_a_change(transport_for):
    world = (
        World()
        .route(
            "/repos/acme/widgets/branches",
            [
                {"name": "main", "commit": {"sha": "m"}},
                {"name": "feat/5", "commit": {"sha": "a"}},
                {"name": "old", "commit": {"sha": "b"}},
            ],
        )
        .route("/repos/acme/widgets/compare/main...old", {"ahead_by": 0})
    )
    changes = [github._change(pr_node(5), "acme/widgets")]
    branches = adapter(world, transport_for).branches("main", changes)
    assert [(b.name, b.has_change, b.ahead_by) for b in branches] == [
        ("feat/5", True, None),
        ("old", False, 0),
    ]


def test_blocking_links_are_native_and_mark_closed_blockers(transport_for):
    world = World().op(
        "BlockingLinks",
        connection(
            [
                {
                    "number": 22,
                    "blockedBy": {
                        "nodes": [
                            {
                                "number": 21,
                                "state": "OPEN",
                                "repository": {"nameWithOwner": "acme/widgets"},
                            },
                            {
                                "number": 4,
                                "state": "CLOSED",
                                "repository": {"nameWithOwner": "acme/meta"},
                            },
                        ]
                    },
                },
                {"number": 23, "blockedBy": {"nodes": []}},
            ],
            key="issues",
        ),
    )
    links = adapter(world, transport_for).blocking_links()
    assert [(l.blocked, l.blocker, l.blocker_open, l.kind, l.blocker_project) for l in links] == [
        (22, 21, True, "native", None),
        (22, 4, False, "native", "acme/meta"),
    ]


def test_each_read_fails_on_its_own(transport_for):
    world = (
        World()
        .route("/user", {"login": "me"})
        .route("/repos/acme/widgets/issues", {"message": "Server Error"}, status=502)
    )
    a = adapter(world, transport_for)
    ident, issues = attempt(a.identity), attempt(a.open_issues)
    assert ident.ok and ident.data.login == "me"
    assert not issues.ok and "HTTP 502" in issues.error
    assert issues.data is None


def test_a_page_cap_is_a_failed_read(transport_for):
    def endless(url):
        return response(
            [issue(1)],
            headers={
                "Link": '<https://api.github.com/repos/acme/widgets/issues?page=2>; rel="next"'
            },
        )

    world = World().route("/repos/acme/widgets/issues", endless)
    read = attempt(adapter(world, transport_for, page_cap=4).open_issues)
    assert not read.ok and "page cap of 4 pages hit" in read.error


def test_project_must_be_owner_repo(transport_for):
    with pytest.raises(ForgeError):
        github.GitHub(transport_for(World()), "group/sub/project")
