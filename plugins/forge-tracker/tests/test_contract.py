"""#21's contract suite: the same scenarios run against every forge's adapter, each written in that
forge's own response shapes by its kit (kits.py), and every adapter must read them back into the
same normalized shapes. Forge-specific reads live in test_github.py and test_gitlab.py."""

from datetime import datetime, timezone

import pytest
from forge_tracker.model import attempt
from kits import KITS

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
