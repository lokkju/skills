import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FIXTURES, TOKEN, FakeRun, ReplayHttp, World
from forge_tracker import catchup, cli
from forge_tracker.model import Change, parse_time

NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)

CONFIG = """---
labels:
  needs_human: needs-human
  decision: decision
tiers:
  labels: [P0, P1, P2, P3]
ruling_pattern: '^Ruling \\((\\d{4}-\\d{2}-\\d{2})\\):'
report_tags: ["v*"]
---
"""


def gql_conn(nodes, key="pullRequests"):
    return {
        "repository": {key: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
    }


def change(number, **kw):
    node = {
        "number": number,
        "title": f"change {number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "headRefName": f"feat/{number}",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-10-14T00:00:00Z",
        "updatedAt": "2026-10-14T12:00:00Z",
        "mergedAt": None,
        "author": {"__typename": "User", "login": "me"},
        "mergeCommit": None,
        "assignees": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "commits": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
    }
    node.update(kw)
    return node


def issue(number, **kw):
    base = {
        "number": number,
        "title": f"issue {number}",
        "state": "open",
        "labels": [],
        "assignees": [],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "closed_at": None,
        "milestone": None,
        "user": {"login": "someone", "type": "User"},
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }
    base.update(kw)
    return base


def world(broken=()):
    """A small repo: one review request, one fresh needs-human issue and a standing backlog, two
    merges, a ruling, and a failed push run."""
    w = World()
    w.route("/user", {"login": "me", "type": "User"})
    w.route("/repos/acme/widgets", {"default_branch": "main"})
    w.route(
        "/repos/acme/widgets/issues",
        lambda url: _issues(url),
    )
    w.route(
        "/repos/acme/widgets/issues/comments",
        [
            {
                "issue_url": "https://api.github.com/repos/acme/widgets/issues/10",
                "user": {"login": "me", "type": "User"},
                "created_at": "2026-10-14T09:00:00Z",
                "body": "Thanks.\nRuling (2026-10-14): ship the GitHub adapter first.\nMore.",
                "html_url": "https://github.com/acme/widgets/issues/10#c1",
            },
            {
                "issue_url": "https://api.github.com/repos/acme/widgets/issues/11",
                "user": {"login": "me", "type": "User"},
                "created_at": "2026-10-14T10:00:00Z",
                "body": "no ruling here",
            },
        ],
    )
    w.route(
        "/search/issues",
        {
            "incomplete_results": False,
            "items": [issue(12, title="mentioned you", updated_at="2026-10-14T08:00:00Z")],
        },
    )
    w.op(
        "OpenChanges",
        gql_conn(
            [
                change(
                    41,
                    title="feat: GitHub adapter",
                    mergeable="UNKNOWN",
                    reviewRequests={
                        "nodes": [{"requestedReviewer": {"__typename": "User", "login": "me"}}]
                    },
                ),
                change(
                    42,
                    title="old review",
                    updatedAt="2026-09-01T00:00:00Z",
                    reviewRequests={
                        "nodes": [{"requestedReviewer": {"__typename": "User", "login": "me"}}]
                    },
                ),
            ]
        ),
    )
    w.op(
        "MergedChanges",
        gql_conn(
            [
                change(
                    44,
                    title="feat: read transport",
                    headRefName="21-transport",
                    mergedAt="2026-10-14T06:00:00Z",
                    updatedAt="2026-10-14T06:00:00Z",
                    mergeCommit={"oid": "a44"},
                    closingIssuesReferences={
                        "nodes": [{"number": 21, "repository": {"nameWithOwner": "acme/widgets"}}]
                    },
                ),
                change(
                    33,
                    title="chore: release 2026.10.15.1",
                    headRefName="yeet/release-2026-10-14",
                    mergedAt="2026-10-14T05:00:00Z",
                    updatedAt="2026-10-14T05:00:00Z",
                ),
            ]
        ),
    )
    w.route(
        "/repos/acme/widgets/actions/runs",
        lambda url: _runs(url),
    )
    w.route(
        "/repos/acme/widgets/actions/runs/900/jobs",
        {"jobs": [{"name": "test", "conclusion": "failure", "run_attempt": 1}]},
    )
    w.route(
        "/repos/acme/widgets/releases",
        [{"tag_name": "v1.0.0", "published_at": "2026-10-13T00:00:00Z", "draft": False}],
    )
    w.route("/repos/acme/widgets/tags", [{"name": "v1.0.0"}])
    for path in broken:
        w.route(path, {"message": f"boom with {TOKEN}"}, status=500)
    return w


def _issues(url):
    from conftest import response

    if "state=open" in url:
        return response(
            [
                issue(
                    13,
                    labels=[{"name": "needs-human"}],
                    updated_at="2026-10-14T11:00:00Z",
                    title="pick a name",
                ),
                issue(14, labels=[{"name": "needs-human"}], assignees=[{"login": "me"}]),
                issue(15, assignees=[{"login": "me"}]),
                issue(16, labels=[{"name": "decision"}]),
                issue(17, pull_request={"url": "x"}, assignees=[{"login": "me"}]),
            ]
        )
    return response(
        [
            issue(
                18,
                created_at="2026-10-14T01:00:00Z",
                updated_at="2026-10-14T01:00:00Z",
                labels=[{"name": "P1"}],
            ),
            issue(
                19,
                state="closed",
                created_at="2026-09-01T00:00:00Z",
                closed_at="2026-10-14T02:00:00Z",
                updated_at="2026-10-14T02:00:00Z",
            ),
            issue(20, updated_at="2026-10-14T03:00:00Z"),
        ]
    )


def _runs(url):
    from conftest import response

    base = {
        "name": "CI",
        "status": "completed",
        "run_attempt": 1,
        "created_at": "2026-10-14T07:00:00Z",
        "html_url": "https://github.com/acme/widgets/actions/runs/900",
    }
    if "event=push" in url:
        return response({"workflow_runs": [dict(base, id=900, event="push", conclusion="failure")]})
    if "event=schedule" in url:
        return response(
            {"workflow_runs": [dict(base, id=901, event="schedule", conclusion="success")]}
        )
    return response({"workflow_runs": []})


@pytest.fixture
def env(tmp_path, repo_dir):
    directory = repo_dir(CONFIG)
    cache = tmp_path / "cache"
    return {
        "repo": directory,
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(cache)},
        "marker": cache / "forge-tracker" / "catchup" / "github.com_acme__widgets_me.json",
    }


def run_catchup(env, *args, http=None, now=NOW, out=None):
    out = out or io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "catchup", *args],
        http=http or world(),
        run=FakeRun(),
        now=now,
        environ=env["environ"],
        out=out,
        tz=timezone.utc,
    )
    return code, out.getvalue() if isinstance(out, io.StringIO) else None


EXPECTED = """\
acme/widgets on github.com, 2026-10-08 08:00 to 2026-10-15 08:00 (first catchup here: the last 7 days; marker moves to 2026-10-15 08:00)

Waiting on you
  #41 review requested: feat: GitHub adapter (draft no, conflicts: unknown)
  #13 needs-human, updated 2026-10-14: pick a name
  #12 mentioned, updated 2026-10-14: mentioned you
  standing: 1 review requested, 2 assigned, 1 needs-human, 1 decision (not updated in the window)
Merged since
  #21  1 PR: #44 feat: read transport
  (no issue) #33 chore: release 2026.10.15.1
Decisions made
  #10  Ruling (2026-10-14): ship the GitHub adapter first.
Board movement
  P1 +1 -0   untiered +0 -1
Health
  main push run 900 (CI): FAILED (jobs: test)
  scheduled run 901 (CI): passed
  flaky: none
  tags v*: v1.0.0 (2026-10-13)
"""


def test_first_catchup_report_and_marker(env):
    code, text = run_catchup(env)
    assert code == 0
    assert text == EXPECTED
    marker = json.loads(env["marker"].read_text())
    assert marker == {
        "end": "2026-10-15T08:00:00Z",
        "host": "github.com",
        "project": "acme/widgets",
        "user": "me",
    }
    assert stat.S_IMODE(os.stat(env["marker"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env["marker"].parent).st_mode) == 0o700


def test_next_window_opens_five_minutes_before_the_marker(env):
    run_catchup(env)
    later = NOW + timedelta(days=1)
    code, text = run_catchup(env, now=later)
    assert code == 0
    assert text.startswith(
        "acme/widgets on github.com, 2026-10-15 07:55 to 2026-10-16 08:00"
        " (since your last catchup; marker moves to 2026-10-16 08:00)"
    )
    assert json.loads(env["marker"].read_text())["end"] == "2026-10-16T08:00:00Z"


def test_since_neither_reads_nor_moves_the_marker(env):
    run_catchup(env)
    before = env["marker"].read_text()
    code, text = run_catchup(env, "--since", "3d", now=NOW + timedelta(days=1))
    assert code == 0
    assert "2026-10-13 08:00 to 2026-10-16 08:00 (--since; the marker isn't read or moved)" in text
    assert env["marker"].read_text() == before


def test_no_mark_reads_but_keeps_the_marker(env):
    run_catchup(env)
    before = env["marker"].read_text()
    code, text = run_catchup(env, "--no-mark", now=NOW + timedelta(days=1))
    assert code == 0
    assert "(since your last catchup; --no-mark, so the marker stays)" in text
    assert env["marker"].read_text() == before


def test_a_failed_read_prints_failed_exits_1_and_keeps_the_marker(env):
    run_catchup(env)
    before = env["marker"].read_text()
    code, text = run_catchup(
        env, http=world(broken=["/search/issues"]), now=NOW + timedelta(days=1)
    )
    assert code == 1
    assert "  mentions: FAILED (HTTP 500 from /search/issues: boom with ***)" in text
    assert TOKEN not in text
    assert "(since your last catchup; a read FAILED, so the marker stays)" in text
    # The other sections still report.
    assert "Health\n  main push run 900 (CI): FAILED (jobs: test)" in text
    assert env["marker"].read_text() == before


def test_no_marker_is_written_when_the_first_run_fails(env):
    code, _ = run_catchup(env, http=world(broken=["/repos/acme/widgets/releases"]))
    assert code == 1
    assert not env["marker"].exists()


def test_a_failed_print_leaves_the_old_marker_and_no_temp_file(env):
    run_catchup(env)
    before = env["marker"].read_text()

    class BrokenOut(io.StringIO):
        def write(self, text):
            raise OSError("disk full")

    with pytest.raises(OSError):
        run_catchup(env, now=NOW + timedelta(days=1), out=BrokenOut())
    assert env["marker"].read_text() == before
    assert os.listdir(env["marker"].parent) == [env["marker"].name]


def test_identity_failure_reads_no_marker(env):
    run_catchup(env)
    before = env["marker"].read_text()
    code, text = run_catchup(env, http=world(broken=["/user"]), now=NOW + timedelta(days=1))
    assert code == 1
    assert "identity FAILED, so the marker wasn't read: the last 7 days" in text
    assert "mentions: FAILED (needs your identity, which failed)" in text
    assert text.rstrip().endswith("identity: FAILED (HTTP 500 from /user: boom with ***)")
    assert env["marker"].read_text() == before


def json_report(env, *args):
    code, text = run_catchup(env, "--json", *args)
    assert code == 0
    return json.loads(text)


@pytest.mark.parametrize(
    "basis, marker, says",
    [
        (
            "first",
            "move",
            "(first catchup here: the last 7 days; marker moves to 2026-10-15 08:00)",
        ),
        ("marker", "move", "(since your last catchup; marker moves to 2026-10-15 08:00)"),
        ("marker", "keep", "(since your last catchup; --no-mark, so the marker stays)"),
        ("first", "keep", "(first catchup here: the last 7 days; --no-mark, so the marker stays)"),
        ("marker", "held", "(since your last catchup; a read FAILED, so the marker stays)"),
        ("since", "keep", "(--since; the marker isn't read or moved)"),
    ],
)
def test_header_says_what_happens_to_the_marker(env, basis, marker, says):
    report = json_report(env)
    report["window"]["basis"] = basis
    report["marker"] = marker
    header = catchup.render(report, timezone.utc).splitlines()[0]
    assert header == f"acme/widgets on github.com, 2026-10-08 08:00 to 2026-10-15 08:00 {says}"


def waiting_lines(report):
    text = catchup.render(report, timezone.utc)
    return text.split("Waiting on you\n")[1].split("Merged since\n")[0].splitlines()


def test_an_empty_standing_backlog_is_said_as_none(env):
    report = json_report(env)
    report["waiting"]["standing"] = {}
    assert waiting_lines(report)[-1] == "  standing: none (not updated in the window)"
    report["waiting"]["items"] = []
    assert waiting_lines(report) == ["  nothing", "  standing: none (not updated in the window)"]


def test_standing_none_isnt_claimed_when_its_read_failed(env):
    report = json_report(env)
    report["waiting"]["items"] = []
    report["waiting"]["standing"] = {}
    report["waiting"]["failed"] = [{"part": "open_issues", "reason": "HTTP 502"}]
    assert waiting_lines(report) == ["  open_issues: FAILED (HTTP 502)"]


def test_all_lists_the_standing_backlog(env):
    code, text = run_catchup(env, "--all")
    assert code == 0
    assert "standing:" not in text
    # #14 is on two lists and appears once.
    assert text.count("#14 ") == 1
    assert "  #14 assigned, needs-human, updated 2026-09-02: issue 14" in text
    assert "#17" not in text  # a PR in the issues list isn't an issue


def test_json_has_the_same_data(env):
    code, text = run_catchup(env, "--json")
    assert code == 0
    report = json.loads(text)
    assert report["ok"] is True and report["user"] == "me"
    assert report["window"]["basis"] == "first"
    assert [i["ref"] for i in report["waiting"]["items"]] == ["#41", "#13", "#12"]
    assert report["waiting"]["items"][0]["conflicts"] == "unknown"
    assert report["merged"]["groups"][0]["issue"] == "#21"
    assert report["health"]["push_run"]["failed_jobs"] == ["test"]


def test_unset_labels_and_pattern_are_said_not_guessed(env, repo_dir):
    env["repo"] = repo_dir("---\n---\n")
    code, text = run_catchup(env)
    assert code == 0
    # One setup line under Waiting on you says what's missing and how to set it up.
    setup = [line for line in text.splitlines() if "setup:" in line]
    assert setup == [
        (
            "  (setup: labels.needs_human, labels.decision and ruling_pattern unset in "
            ".claude/forge-tracker.local.md, so no label marks needs-human or decision items and "
            "rulings aren't picked out. Set them, or set them to ~ to record that this repo has none; "
            "`forge-tracker labels suggest` proposes one from this repo's labels)"
        )
    ]
    assert "Decisions made\n  (ruling_pattern isn't set; see setup above)" in text
    assert "Board movement\n  untiered +1 -1" in text


def test_no_config_file_says_so_once(env, tmp_path):
    env["repo"] = str(tmp_path / "bare")
    os.mkdir(env["repo"])
    code, text = run_catchup(env)
    assert code == 0
    setup = [line for line in text.splitlines() if "setup:" in line]
    assert setup == [
        (
            "  (setup: no .claude/forge-tracker.local.md, so no label marks needs-human or decision "
            "items and rulings aren't picked out. `forge-tracker labels suggest` proposes one from "
            "this repo's labels)"
        )
    ]


def test_a_declined_setup_is_said_in_the_line(env, tmp_path):
    env["repo"] = str(tmp_path / "bare")
    os.mkdir(env["repo"])
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "labels", "decline"],
        run=FakeRun(),
        now=NOW,
        environ=env["environ"],
        out=out,
    )
    assert code == 0
    assert "setup declined for acme/widgets" in out.getvalue()
    code, text = run_catchup(env)
    assert code == 0
    assert "Setup was declined on 2026-10-15; `forge-tracker labels suggest` still" in text


def test_keys_set_to_none_silence_the_notes(env, repo_dir):
    env["repo"] = repo_dir(
        "---\nlabels:\n  needs_human: ~\n  decision: ~\nruling_pattern: ~\n---\n"
    )
    code, text = run_catchup(env)
    assert code == 0
    assert "setup:" not in text
    assert "ruling_pattern" not in text


def test_usage_errors(env, capsys):
    code, _ = run_catchup(env, "--since", "yesterday")
    assert code == 2
    assert "--since takes" in capsys.readouterr().err


def test_detection_failure_is_a_usage_error(env, capsys):
    run = FakeRun(origin="git@gitlab.com:group/project.git")
    code = cli.main(["--repo", env["repo"], "catchup"], http=world(), run=run, now=NOW)
    assert code == 2
    assert "only GitHub so far" in capsys.readouterr().err


def test_tracker_web_fixes_a_remote_detection_cant_read(env, capsys):
    """An SSH alias as origin fails detection; `git config tracker.web` makes it the repo."""
    alias = FakeRun(origin="work-gh:acme/widgets-mirror.git")
    code = cli.main(["--repo", env["repo"], "catchup"], http=world(), run=alias, now=NOW)
    assert code == 2
    assert "can't tell which forge work-gh runs" in capsys.readouterr().err
    out = io.StringIO()
    run = FakeRun(
        origin="work-gh:acme/widgets-mirror.git",
        git_config={"tracker.web": "https://github.com/acme/widgets"},
    )
    code = cli.main(
        ["--repo", env["repo"], "catchup", "--no-mark"],
        http=world(),
        run=run,
        now=NOW,
        environ=env["environ"],
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    assert out.getvalue().startswith("acme/widgets on github.com, ")
    assert ["gh", "auth", "token", "--hostname", "github.com"] in run.calls


# --- pieces ------------------------------------------------------------------------------------


def mk(number, title="", branch="", closing=()):
    t = parse_time("2026-10-14T00:00:00Z")
    return Change(
        number, title, branch, False, None, (), (), "no", None, t, t, closing_issues=closing
    )


@pytest.mark.parametrize(
    "c, numbers",
    [
        (mk(5, "fix: x (#12)", "fix/12-x"), [12]),
        (mk(5, "fix: x", "release-2026-10-14"), []),
        (mk(5, "chore: release 2026.10.15.1", "yeet/release-main"), []),
        (mk(5, "fix", "sd-26", (3,)), [3, 26]),
        (mk(5, "fix: see acme/other#9 and #5", "x"), []),
        (mk(5, "feat: v2 parser", "v2"), []),
        # lokkju/infra's real branches: versions, run ids and dates name no issue.
        (
            mk(279, "chore(homelab): GARM runner image 2.338.0", "garm-runner-2.338.0-38790455020"),
            [],
        ),
        (mk(156, "chore(cilium): 1.20.2", "cilium-1.20.2"), []),
        (mk(155, "chore(cilium): 1.19.8, 365-day Hubble certs", "cilium-1.19.8"), []),
        (mk(170, "docs(plans): spikes", "lokkju/plans-2026-10-08"), []),
        (
            mk(
                200,
                "chore(homelab): agent-runner image",
                "agent-runner-1.18.35-5e5bdad-38507457856",
            ),
            [],
        ),
        (mk(5, "fix: x (#0)", "fix-0"), []),
        (mk(5, "fix: x", "fix-42"), [42]),
    ],
)
def test_issue_numbers(c, numbers):
    assert catchup.issue_numbers(c) == numbers


@pytest.mark.parametrize(
    "value, expected",
    [
        ("3d", NOW - timedelta(days=3)),
        ("12h", NOW - timedelta(hours=12)),
        ("2w", NOW - timedelta(weeks=2)),
        ("90m", NOW - timedelta(minutes=90)),
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=timezone.utc)),
        ("2026-10-01T05:00:00+02:00", datetime(2026, 10, 1, 3, tzinfo=timezone.utc)),
    ],
)
def test_parse_since(value, expected):
    assert catchup.parse_since(value, NOW) == expected


def test_unreadable_marker_falls_back_to_seven_days():
    window = catchup.window_from_marker({"end": None, "x": 1}, NOW)
    assert window.basis == "first" and window.note == "the marker was unreadable"
    assert window.start == NOW - timedelta(days=7)


def test_marker_from_the_future_is_clamped():
    window = catchup.window_from_marker({"end": "2027-01-01T00:00:00Z"}, NOW)
    assert window.start == NOW


# --- recorded fixture --------------------------------------------------------------------------


REPLAY_CONFIG = """---
labels:
  needs_human: needs-human
  decision: decision
tiers:
  labels: [P0, P1, P2, P3]
ruling_pattern: '^Ruling'
changelog: plugins/session-decisions/CHANGELOG.md
report_tags: ["session-decisions--v*", "forge-release--v*", "registry:app"]
---
"""


def test_replay_of_a_recorded_github_catchup(tmp_path, repo_dir):
    """Real responses recorded from lokkju/skills (`--record`), replayed with no network. The
    golden text sits beside the recording."""
    http = ReplayHttp(FIXTURES / "github" / "catchup-lokkju-skills.json")
    now = parse_time(http.meta["recorded_at"])
    run = FakeRun(origin="https://github.com/lokkju/skills.git")
    run.answer(("rev-list",), stdout="0123abcd\n")
    run.answer(
        ("diff",),
        stdout=(
            "+++ b/plugins/session-decisions/CHANGELOG.md\n"
            "+## 2026.10.15.1\n"
            "+- **session-decisions:** an entry\n"
        ),
    )
    out = io.StringIO()
    code = cli.main(
        ["--repo", repo_dir(REPLAY_CONFIG), *http.meta["argv"]],
        http=http,
        run=run,
        now=now,
        environ={"HOME": str(tmp_path)},
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    golden = (FIXTURES / "github" / "catchup-lokkju-skills.txt").read_text()
    assert out.getvalue() == golden
    assert all(call[2]["Authorization"] == f"Bearer {TOKEN}" for call in http.calls)
    assert {call[0] for call in http.calls} <= {"GET", "POST"}
