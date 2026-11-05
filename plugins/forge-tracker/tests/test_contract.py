"""#21's contract suite: the same scenarios run against every forge's adapter, each written in that
forge's own response shapes by its kit (kits.py), and every adapter must read them back into the
same normalized shapes. Forge-specific reads live in test_github.py and test_gitlab.py.

The write half (#154) runs each write operation through the write layer the CLI builds for every
forge, over a write kit's fake forge (write_kits.py), and asserts the exact calls sent."""

import json
from datetime import datetime, timezone

import pytest
from forge_tracker.model import ForgeError, attempt
from kits import KITS
from write_kits import WRITE_KITS

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.fixture(params=KITS, ids=lambda kit: kit.name)
def kit(request):
    return request.param


def test_identity_and_bot_flag(kit):
    world = kit.world()
    kit.user(world, "me", bot=None)
    assert kit.adapter(world).identity().login == "me"
    assert kit.adapter(world).identity().is_bot is False
    world = kit.world()
    kit.user(world, "ci", bot="type")
    assert kit.adapter(world).identity().is_bot is True


def test_issues_carry_labels_milestone_assignees_and_timestamps(kit):
    world = kit.world()
    kit.issues(
        world,
        [
            {
                "number": 1,
                "title": "pin the CA",
                "labels": ("P1", "bug"),
                "assignees": ("me",),
                "milestone": ("Tier 1", "2026-12-31"),
                "state": "closed",
                "closed": "2026-10-13T00:00:00Z",
                "author": "alex",
                "body": "details",
            },
            {"number": 2},
        ],
    )
    one, two = kit.adapter(world).open_issues()
    assert (one.number, one.title, one.state) == (1, "pin the CA", "closed")
    assert one.labels == ("P1", "bug") and one.assignees == ("me",)
    assert (one.milestone.title, one.milestone.due[:10]) == ("Tier 1", "2026-12-31")
    assert one.created == datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert one.updated == datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert one.closed == datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert (one.author, one.body) == ("alex", "details")
    assert (two.labels, two.milestone, two.closed) == ((), None, None)


@pytest.mark.parametrize(
    "scenario, expected",
    [("yes", "yes"), ("no", "no"), ("unknown", "unknown"), ("settling", "unknown")],
)
def test_conflicts_are_three_valued(kit, scenario, expected):
    world = kit.world()
    kit.changes(world, [{"number": 5, "conflicts": scenario}])
    (change,) = kit.adapter(world).open_changes()
    assert change.conflicts == expected


def test_open_changes_carry_branch_draft_people_and_pipeline(kit):
    world = kit.world()
    kit.changes(
        world,
        [
            {
                "number": 5,
                "branch": "21-adapter",
                "draft": True,
                "reviewers": ("me",),
                "assignees": ("alex",),
                "pipeline": "success",
            },
            {"number": 6, "pipeline": "failure"},
            {"number": 7, "pipeline": None},
        ],
    )
    five, six, seven = kit.adapter(world).open_changes()
    assert (five.number, five.branch, five.draft) == (5, "21-adapter", True)
    assert (five.reviewers, five.assignees, five.author) == (("me",), ("alex",), "me")
    assert (five.pipeline, six.pipeline, seven.pipeline) == ("success", "failure", None)


def test_merged_since_keeps_the_window_merge_sha_and_closing_issues(kit):
    world = kit.world()
    kit.merged(
        world,
        [
            {
                "number": 9,
                "merged": "2026-10-12T00:00:00Z",
                "updated": "2026-10-12T00:00:01Z",
                "sha": "m9",
                "closes": (2,),
            },
            {"number": 8, "merged": "2026-10-01T00:00:00Z", "updated": "2026-10-11T00:00:00Z"},
        ],
    )
    (nine,) = kit.adapter(world).merged_since(T0)
    assert (nine.number, nine.merge_sha, nine.closing_issues) == (9, "m9", (2,))
    assert nine.merged_at == datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_native_blocking_links(kit):
    world = kit.world()
    kit.links(world, [(22, 21, True, None), (22, 4, False, "acme/meta")])
    links = kit.adapter(world).blocking_links()
    assert [(l.blocked, l.blocker, l.blocker_open, l.kind, l.blocker_project) for l in links] == [
        (22, 21, True, "native", None),
        (22, 4, False, "native", "acme/meta"),
    ]


def test_each_read_fails_on_its_own(kit):
    world = kit.world()
    kit.user(world, "me", bot=None)
    kit.failing_issues(world, 502, "Server Error")
    a = kit.adapter(world)
    ident, issues = attempt(a.identity), attempt(a.open_issues)
    assert ident.ok and ident.data.login == "me"
    assert not issues.ok and "HTTP 502" in issues.error and issues.data is None


def test_a_page_cap_hit_is_a_failed_read_not_a_short_list(kit):
    world = kit.world()
    kit.endless_issues(world)
    read = attempt(kit.adapter(world, page_cap=4).open_issues)
    assert not read.ok and "page cap of 4 pages hit" in read.error


def test_a_token_in_an_error_comes_out_as_stars(kit):
    world = kit.world()
    kit.failing_issues(world, 401, f"bad credentials {kit.secret}")
    read = attempt(kit.adapter(world).open_issues)
    assert not read.ok and kit.secret not in read.error and "***" in read.error


def test_latest_run_names_its_failed_jobs(kit):
    world = kit.world()
    kit.run(world, "failure", [("lint", "success"), ("test", "failure")])
    run = kit.adapter(world).latest_run("main", "push")
    assert (run.id, run.status, run.conclusion, run.failed_jobs) == (
        100,
        "completed",
        "failure",
        ("test",),
    )
    world = kit.world()
    kit.run(world, None, [])
    assert kit.adapter(world).latest_run("main", "push") is None


def test_tags_prefer_the_newest_dated_release_then_a_tag(kit):
    world = kit.world()
    kit.releases(
        world,
        [("v1.2.0", "2026-10-01T00:00:00Z"), ("v1.3.0", "2026-10-09T00:00:00Z")],
        ["nightly-5", "v1.3.0"],
    )
    tags = kit.adapter(world).tags(("v*", "nightly-*", "x*", "registry:app"))
    assert [(t.pattern, t.name, t.note is not None) for t in tags] == [
        ("v*", "v1.3.0", False),
        ("nightly-*", "nightly-5", False),
        ("x*", None, False),
        ("registry:app", None, True),
    ]


def test_references_are_written_the_forges_way(kit):
    a = kit.adapter(kit.world())
    assert a.ref(5) == "#5"
    assert a.ref(5, change=True) == f"{kit.change_sigil}5"


# --- /groom's possibly-done reads ---------------------------------------------------------------


def test_cross_refs_are_the_changes_that_mention_an_open_issue(kit):
    world = kit.world()
    kit.cross_refs(
        world,
        {3: [(9, "merged", "2026-10-12T00:00:00Z"), (10, "open", None)], 4: []},
    )
    found = sorted(kit.adapter(world).cross_refs(), key=lambda r: (r.issue, r.change))
    assert [(r.issue, r.change, r.state, r.merged_at, r.change_project) for r in found] == [
        (3, 9, "merged", datetime(2026, 10, 12, tzinfo=timezone.utc), None),
        (3, 10, "open", None, None),
    ]
    assert found[0].title == "change 9"


def test_change_files_name_each_path_with_its_diff_where_the_forge_has_one(kit):
    world = kit.world()
    patch = "@@ -1,2 +1,2 @@ def parse_rows():\n-    x\n+    y"
    kit.files(world, 9, [("src/a.py", patch, None), ("src/new.py", None, "src/old.py")])
    one, two = kit.adapter(world).change_files(9)
    assert (one.path, one.previous_path) == ("src/a.py", None)
    assert (two.path, two.previous_path, two.patch) == ("src/new.py", "src/old.py", None)
    assert one.patch == (patch if kit.patches else None)
    assert kit.adapter(world).file_patches is kit.patches


def test_a_forge_without_a_changed_files_read_says_unsupported(kit):
    if not kit.can_miss_files:
        pytest.skip(f"{kit.name} always has the changed-files read")
    world = kit.world()
    kit.missing_files(world, 9)
    read = attempt(lambda: kit.adapter(world).change_files(9))
    assert not read.ok and read.unsupported


# --- the write layer ----------------------------------------------------------------------------
# Each scenario runs one operation through the write layer the CLI builds for that forge and
# asserts the exact calls it sent: (method, path under the project's REST path, JSON body).

COMMENT = "Closing: merged #31 closes it."


@pytest.fixture(params=WRITE_KITS, ids=lambda kit: kit.name)
def wkit(request):
    return request.param


def sent(world, kit):
    return [(m, p.removeprefix(kit.W), body) for m, p, body in world.sent]


def apply(kit, world, tmp_path, op, payload, **config):
    return kit.writer(world, tmp_path, **config).apply(op, payload)


EXPECT_CLOSE = {
    "github": [
        ("POST", "/issues/14/comments", {"body": COMMENT}),
        ("PATCH", "/issues/14", {"state": "closed", "state_reason": "completed"}),
    ],
    "gitlab": [
        ("POST", "/issues/14/notes", {"body": COMMENT}),
        ("PUT", "/issues/14", {"state_event": "close"}),
    ],
}


def test_write_close_comments_then_closes(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 14)
    result = apply(wkit, world, tmp_path, "close", {"issue": 14, "comment": COMMENT})
    assert (result.status, result.detail) == ("written", "commented on and closed #14")
    assert sent(world, wkit) == EXPECT_CLOSE[wkit.name]


def test_write_close_refuses_an_issue_already_closed(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 14, state="closed")
    result = apply(wkit, world, tmp_path, "close", {"issue": 14, "comment": COMMENT})
    assert (result.status, result.detail) == ("refused", "#14 is already closed; nothing written")
    assert world.sent == []


def test_a_failed_write_is_failed_with_the_secret_hidden(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 14)
    comment_path = EXPECT_CLOSE[wkit.name][0][1]
    wkit.fail(world, "POST", comment_path, 403, f"forbidden {wkit.secret}")
    result = apply(wkit, world, tmp_path, "close", {"issue": 14, "comment": COMMENT})
    assert result.status == "failed" and "HTTP 403" in result.detail
    assert wkit.secret not in result.detail and "***" in result.detail
    assert len(world.sent) == 1  # nothing after the failed comment


EXPECT_LABELS = {
    "github": [
        ("POST", "/issues/22/labels", {"labels": ["P2"]}),
        ("DELETE", "/issues/22/labels/P1", None),
        ("DELETE", "/issues/22/labels/stale", None),
    ],
    "gitlab": [("PUT", "/issues/22", {"add_labels": "P2", "remove_labels": "P1"})],
}


def test_write_labels_adds_removes_and_says_what_was_already_off(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 22, labels=("P1",))
    payload = {"issue": 22, "add": ["P2"], "remove": ["P1", "stale"]}
    result = apply(wkit, world, tmp_path, "labels", payload)
    assert (result.status, result.detail) == (
        "written",
        "#22: added P2; removed P1; stale already off",
    )
    assert sent(world, wkit) == EXPECT_LABELS[wkit.name]


EXPECT_MILESTONE = {
    "github": [
        ("PATCH", "/issues/20", {"milestone": 3}),
        ("PATCH", "/issues/20", {"milestone": None}),
    ],
    "gitlab": [
        ("PUT", "/issues/20", {"milestone_id": 3}),
        ("PUT", "/issues/20", {"milestone_id": 0}),
    ],
}


def test_write_milestone_sets_and_clears(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 20)
    writer = wkit.writer(world, tmp_path)
    one = writer.apply("milestone", {"issue": 20, "milestone": {"number": 3, "title": "Tier 1"}})
    two = writer.apply("milestone", {"issue": 20, "milestone": None})
    assert (one.detail, two.detail) == ("#20: milestone set to Tier 1", "#20: milestone cleared")
    assert sent(world, wkit) == EXPECT_MILESTONE[wkit.name]


EXPECT_UNLINK_LABEL = {
    "github": [("DELETE", "/issues/25/labels/blocked", None)],
    "gitlab": [("PUT", "/issues/25", {"remove_labels": "blocked"})],
}


def test_write_unlink_takes_off_the_blocked_label(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 25, labels=("blocked",))
    result = apply(wkit, world, tmp_path, "unlink", {"issue": 25, "label": "blocked"})
    assert (result.status, result.detail) == ("written", "#25: removed blocked")
    assert sent(world, wkit) == EXPECT_UNLINK_LABEL[wkit.name]


def test_write_unlink_refuses_a_label_already_gone(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 25, labels=())
    result = apply(wkit, world, tmp_path, "unlink", {"issue": 25, "label": "blocked"})
    assert (result.status, result.detail) == (
        "refused",
        "#25 no longer has blocked; nothing written",
    )


EXPECT_UNLINK_BLOCKER = {
    "github": [("DELETE", "/issues/26/dependencies/blocked_by/9019", None)],
    "gitlab": [("DELETE", "/issues/26/links/519", None)],
}


def test_write_unlink_removes_the_link_to_a_closed_blocker(wkit, tmp_path):
    world = wkit.world()
    wkit.blocked(world, 26, [(19, "closed", None)])
    result = apply(wkit, world, tmp_path, "unlink", {"issue": 26, "blocker": 19})
    assert (result.status, result.detail) == ("written", "#26: no longer blocked by #19")
    assert sent(world, wkit) == EXPECT_UNLINK_BLOCKER[wkit.name]


def test_write_unlink_keeps_the_link_to_a_blocker_open_again(wkit, tmp_path):
    world = wkit.world()
    wkit.blocked(world, 26, [(19, "open", None)])
    result = apply(wkit, world, tmp_path, "unlink", {"issue": 26, "blocker": 19})
    assert (result.status, result.detail) == ("refused", "#19 is open again; the link stays")
    assert world.sent == []


def _link(project, number):
    return {"target_project_id": project, "target_issue_iid": number, "link_type": "is_blocked_by"}


EXPECT_LINK_ADD = {
    "github": [
        ("POST", "/issues/70/dependencies/blocked_by", {"issue_id": 9071}),
        ("POST", "/issues/70/dependencies/blocked_by", {"issue_id": 7004}),
    ],
    "gitlab": [
        ("POST", "/issues/70/links", _link("acme/widgets", 71)),
        ("POST", "/issues/70/links", _link("acme/other", 4)),
    ],
}


def test_write_link_add_records_a_native_link_here_and_across_projects(wkit, tmp_path):
    world = wkit.world()
    wkit.blocked(world, 70, [])
    wkit.issue(world, 70)
    wkit.issue(world, 71)
    wkit.issue(world, 4, project="acme/other")
    writer = wkit.writer(world, tmp_path)
    here = writer.apply("link-add", {"issue": 70, "blocker": 71})
    there = writer.apply("link-add", {"issue": 70, "blocker": 4, "blocker_project": "acme/other"})
    assert (here.detail, there.detail) == (
        "#70: now blocked by #71",
        "#70: now blocked by acme/other#4",
    )
    assert sent(world, wkit) == EXPECT_LINK_ADD[wkit.name]


def test_write_link_add_refuses_a_link_already_there(wkit, tmp_path):
    world = wkit.world()
    wkit.blocked(world, 70, [(71, "open", None)])
    wkit.issue(world, 70)
    result = apply(wkit, world, tmp_path, "link-add", {"issue": 70, "blocker": 71})
    assert (result.status, result.detail) == (
        "refused",
        "#70 is already blocked by #71; nothing written",
    )
    assert world.sent == []


EXPECT_LINK_REMOVE = {
    "github": [("DELETE", "/issues/70/dependencies/blocked_by/9071", None)],
    "gitlab": [("DELETE", "/issues/70/links/571", None)],
}


def test_write_link_remove_deletes_the_link_whatever_the_blockers_state(wkit, tmp_path):
    world = wkit.world()
    wkit.blocked(world, 70, [(71, "open", None)])
    result = apply(wkit, world, tmp_path, "link-remove", {"issue": 70, "blocker": 71})
    assert (result.status, result.detail) == ("written", "#70: no longer blocked by #71")
    assert sent(world, wkit) == EXPECT_LINK_REMOVE[wkit.name]


EXPECT_DELETE_BRANCH = {
    "github": [("DELETE", "/git/refs/heads/feat/old-1", None)],
    "gitlab": [("DELETE", "/repository/branches/feat%2Fold-1", None)],
}


def test_write_delete_branch_deletes_one_still_at_the_checked_sha(wkit, tmp_path):
    world = wkit.world()
    wkit.branch(world, "feat/old-1", "d" * 40)
    payload = {"branch": "feat/old-1", "sha": "d" * 40}
    result = apply(wkit, world, tmp_path, "delete-branch", payload)
    assert (result.status, result.detail) == ("written", "deleted branch feat/old-1 (was ddddddd)")
    assert sent(world, wkit) == EXPECT_DELETE_BRANCH[wkit.name]


@pytest.mark.parametrize(
    "head, protected, detail",
    [
        ("e" * 40, False, "feat/old-1 moved to eeeeeee since ddddddd was checked; kept"),
        ("d" * 40, True, "feat/old-1 is protected; kept"),
    ],
)
def test_write_delete_branch_keeps_a_moved_or_protected_branch(
    wkit, tmp_path, head, protected, detail
):
    world = wkit.world()
    wkit.branch(world, "feat/old-1", head, protected=protected)
    payload = {"branch": "feat/old-1", "sha": "d" * 40}
    result = apply(wkit, world, tmp_path, "delete-branch", payload)
    assert (result.status, result.detail) == ("refused", detail)
    assert world.sent == []


EXPECT_LABEL_CREATE = {
    "github": [("POST", "/labels", {"name": "needs-human", "color": "d93f0b", "description": "x"})],
    "gitlab": [
        ("POST", "/labels", {"name": "needs-human", "color": "#d93f0b", "description": "x"})
    ],
}


def test_write_label_create(wkit, tmp_path):
    world = wkit.world()
    wkit.repo_labels(world, ["bug"])
    payload = {"name": "needs-human", "color": "D93F0B", "description": "x"}
    result = apply(wkit, world, tmp_path, "label-create", payload)
    assert (result.status, result.detail) == ("written", "created label needs-human (#d93f0b)")
    assert sent(world, wkit) == EXPECT_LABEL_CREATE[wkit.name]


def test_the_write_transport_sends_only_its_forges_write_calls(wkit, tmp_path):
    world = wkit.world()
    transport = wkit.writer(world, tmp_path).forge.w
    base = transport.repo_path
    for method, path in (
        ("GET", f"{base}/issues/1"),
        ("POST", f"{base}/issues/1"),
        ("DELETE", base),
        ("POST", f"{base}/hooks"),
        ("DELETE", f"{base}/issues/1/labels/x/y"),
        ("POST", "/projects/acme%2Fother/issues/1/notes"),
        ("POST", "/repos/acme/other/issues/1/comments"),
        ("POST", "/graphql"),
    ):
        with pytest.raises(ForgeError, match="refused"):
            transport.send(method, path, {})
    assert world.sent == []


def test_every_write_is_logged_with_its_result(wkit, tmp_path):
    world = wkit.world()
    wkit.issue(world, 14, state="closed")
    writer = wkit.writer(world, tmp_path)
    writer.apply("close", {"issue": 14, "comment": COMMENT})
    writer.apply("milestone", {"issue": 14, "milestone": None})
    with open(writer.log_path) as log:
        entries = [json.loads(line) for line in log]
    assert [(e["op"], e["status"], e["via"]) for e in entries] == [
        ("close", "refused", "forge"),
        ("milestone", "written", "forge"),
    ]
