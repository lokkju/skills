"""Native blocking links: the link-add and link-remove writes, `forge-tracker link`, and the
reads they check first. Every forge here is fake; nothing can reach a real one."""

import json

import pytest
from conftest import TOKEN, FakeRun, response
from test_groom import CONFIG, Forge, Repo, run_cli


@pytest.fixture
def env(tmp_path, repo_dir):
    return {
        "repo": repo_dir(CONFIG),
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")},
        "log": tmp_path / "cache" / "forge-tracker" / "writes" / "github.com_acme__widgets.jsonl",
    }


def dep(number, project="acme/widgets", state="open"):
    """One entry of GitHub's `GET /issues/{n}/dependencies/blocked_by` list."""
    return {
        "id": 9000 + number,
        "number": number,
        "state": state,
        "closed_at": None,
        "repository_url": f"https://api.github.com/repos/{project}",
    }


def forge(blocked_by=None):
    """A fake forge where #70 is blocked by whatever `blocked_by` lists (nothing by default)."""
    world = Repo().world()
    world.route("/repos/acme/widgets/issues/70/dependencies/blocked_by", blocked_by or [])
    world.route(
        "/repos/acme/other/issues/4", {"id": 7004, "number": 4, "state": "open", "closed_at": None}
    )
    return Forge(world)


# --- the writes ---------------------------------------------------------------------------------


def test_link_add_posts_the_blockers_id(env):
    f = forge()
    code, text = run_cli(env, "link", "add", "#70", "--blocked-by", "71", http=f)
    assert code == 0, text
    assert text == "link-add: #70: now blocked by #71\n"
    assert f.writes == [
        ("POST", "/repos/acme/widgets/issues/70/dependencies/blocked_by", {"issue_id": 9071})
    ]
    entries = [json.loads(line) for line in env["log"].read_text().splitlines()]
    assert [(e["op"], e["status"], e["via"]) for e in entries] == [("link-add", "written", "forge")]


def test_link_add_to_another_project_posts_under_this_one(env):
    f = forge()
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", "acme/other#4", http=f)
    assert code == 0, text
    assert text == "link-add: #70: now blocked by acme/other#4\n"
    assert f.writes == [
        ("POST", "/repos/acme/widgets/issues/70/dependencies/blocked_by", {"issue_id": 7004})
    ]


def test_write_link_add_takes_a_json_payload(env):
    f = forge()
    code, _ = run_cli(
        env, "write", "link-add", http=f, stdin=json.dumps({"issue": 70, "blocker": 71})
    )
    assert code == 0
    assert [w[:2] for w in f.writes] == [
        ("POST", "/repos/acme/widgets/issues/70/dependencies/blocked_by")
    ]


@pytest.mark.parametrize(
    "blocker, existing, message",
    [
        ("71", [dep(71)], "#70 is already blocked by #71; nothing written"),
        ("19", [], "#19 is closed; a link to or from it blocks nothing"),
        ("60", [], "#60 is a pull request; a blocking link joins two issues"),
        ("70", [], "#70 can't block itself"),
        ("acme/widgets#70", [], "#70 can't block itself"),
    ],
)
def test_link_add_is_refused_before_anything_is_sent(env, blocker, existing, message):
    f = forge(existing)
    f.changes.add(60)
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", blocker, http=f)
    assert code == 2
    assert message in text
    assert "REFUSED" in text
    assert f.writes == []


def test_link_add_from_a_closed_issue_is_refused(env):
    f = forge()
    f.issue_state[70] = "closed"
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", "71", http=f)
    assert code == 2
    assert "#70 is closed" in text
    assert f.writes == []


def test_a_link_in_another_project_with_the_same_number_isnt_the_same_link(env):
    f = forge([dep(4)])  # #70 is blocked by acme/widgets#4, not acme/other#4
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", "acme/other#4", http=f)
    assert code == 0, text
    assert len(f.writes) == 1


def test_link_remove_deletes_the_link(env):
    f = forge([dep(71)])
    code, text = run_cli(env, "link", "remove", "70", "--blocked-by", "#71", http=f)
    assert code == 0, text
    assert text == "link-remove: #70: no longer blocked by #71\n"
    assert f.writes == [
        ("DELETE", "/repos/acme/widgets/issues/70/dependencies/blocked_by/9071", None)
    ]


def test_link_remove_of_a_link_that_isnt_there_is_refused(env):
    f = forge([dep(72)])
    code, text = run_cli(env, "link", "remove", "70", "--blocked-by", "71", http=f)
    assert code == 2
    assert "REFUSED (#70 isn't blocked by #71; nothing written)" in text
    assert f.writes == []


def test_a_failed_link_write_says_so_without_the_token(env):
    f = forge()
    f.answers[("POST", "/repos/acme/widgets/issues/70/dependencies/blocked_by")] = (
        422,
        {"message": f"Validation failed {TOKEN}"},
    )
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", "71", http=f)
    assert code == 1
    assert "FAILED (HTTP 422" in text
    assert TOKEN not in text


@pytest.mark.parametrize(
    "args",
    [
        ["link", "add", "abc", "--blocked-by", "71"],
        ["link", "add", "70", "--blocked-by", "acme#71"],
        ["link", "add", "70", "--blocked-by", "#"],
    ],
)
def test_link_arguments_that_dont_parse_write_nothing(env, args):
    f = forge()
    code, _ = run_cli(env, *args, http=f)
    assert code == 2
    assert f.writes == []


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"issue": 70}, "blocker must be a positive issue number"),
        ({"issue": 70, "blocker": 71, "blocker_project": "other"}, "must be owner/repo"),
        ({"issue": 70, "blocker": 71, "label": "x"}, "unknown key label"),
        ({"issue": 70, "blocker": 71, "repo": "acme/other"}, "not acme/widgets"),
    ],
)
def test_link_payloads_are_validated(env, payload, message):
    f = forge()
    code, text = run_cli(env, "write", "link-remove", http=f, stdin=json.dumps(payload))
    assert code == 2
    assert message in text
    assert f.writes == []


def test_record_is_refused_for_link(env):
    code, _ = run_cli(
        env, "--record", "x.json", "link", "add", "70", "--blocked-by", "71", http=forge()
    )
    assert code == 2


def wrapped(env, repo_dir):
    config = CONFIG.replace(
        "stale_days: 10\n", "stale_days: 10\nwrite_wrapper: ./bin/tracker-write\n"
    )
    return dict(env, repo=repo_dir(config))


def test_with_a_wrapper_the_link_goes_to_it_after_the_checks(env, repo_dir):
    env = wrapped(env, repo_dir)
    run = FakeRun()
    run.answer(["./bin/tracker-write"], 0, "linked\n", "")
    f = forge()
    code, text = run_cli(env, "link", "add", "70", "--blocked-by", "71", http=f, run=run)
    assert code == 0, text
    assert text == "link-add: write-wrapper did it: linked\n"
    assert f.writes == []
    calls = [(a, k) for a, k in zip(run.calls, run.kwargs) if a[0] == "./bin/tracker-write"]
    assert [a for a, _ in calls] == [["./bin/tracker-write", "link-add"]]
    assert json.loads(calls[0][1]["input"]) == {"issue": 70, "blocker": 71, "repo": "acme/widgets"}


def test_with_a_wrapper_an_existing_link_never_reaches_it(env, repo_dir):
    env = wrapped(env, repo_dir)
    run = FakeRun()
    run.answer(["./bin/tracker-write"], 0, "linked\n", "")
    code, text = run_cli(
        env, "link", "add", "70", "--blocked-by", "71", http=forge([dep(71)]), run=run
    )
    assert code == 2
    assert "already blocked by #71" in text
    assert not any(a[0] == "./bin/tracker-write" for a in run.calls)


def test_issue_ref_and_blocked_by_read_through_rest(transport_for):
    from forge_tracker.forges.github import GitHub

    calls = []

    def http(method, url, headers, body=None):
        calls.append((method, url))
        if url.startswith("https://api.github.com/repos/acme/widgets/issues/5/dependencies"):
            return response([dep(3), dep(4, project="acme/other", state="closed")])
        return response({"id": 1, "number": 9, "state": "closed", "pull_request": {}})

    gh = GitHub(transport_for(http), "acme/widgets")
    links = gh.blocked_by(5)
    assert [(link.blocker, link.blocker_project, link.blocker_open) for link in links] == [
        (3, None, True),
        (4, "acme/other", False),
    ]
    ref = gh.issue_ref(9, "acme/other")
    assert (ref.project, ref.state, ref.is_change, ref.id) == ("acme/other", "closed", True, 1)
    assert all(method == "GET" for method, _ in calls)
