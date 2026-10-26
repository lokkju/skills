"""/catchup over a workspace: a folder that isn't a git repo but holds several. The repos are real
git repos in a temp folder (git runs for real, with the user's own config shut out); `gh` and the
forge are fakes, or a recorded fixture."""

import io
import json
import re
import subprocess
import urllib.parse
from datetime import timezone

import pytest
from conftest import FIXTURES, TOKEN, ReplayHttp, World, response
from forge_tracker import cli
from forge_tracker.model import parse_time
from test_catchup import CONFIG, NOW, gql_conn, world

QUIET = "acme/gadgets"


@pytest.fixture(autouse=True)
def no_user_git_config(monkeypatch, tmp_path):
    """Real git, but none of the user's tracker.* keys or other settings leak in."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # The temp folder may sit inside some git repo; the workspace mustn't be read as part of it.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


class Run:
    """git for real; `gh auth token` answered."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")
        if argv[:1] != ["git"]:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.run(argv, check=False, **kwargs)


def git_repo(path, origin=None, config_text=None, **keys):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    for key, value in keys.items():
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    if config_text is not None:
        (path / ".claude").mkdir()
        (path / ".claude" / "forge-tracker.local.md").write_text(config_text)
    return path


def quiet_world(project=QUIET, broken=()):
    """A repo where nothing happened: no issues, comments, changes or merges; main passed."""
    base = f"/repos/{project}"
    w = World()
    w.route("/user", {"login": "me", "type": "User"})
    w.route(base, {"default_branch": "main"})
    w.route(f"{base}/issues", [])
    w.route(f"{base}/issues/comments", [])
    w.route("/search/issues", {"incomplete_results": False, "items": []})
    w.op("OpenChanges", gql_conn([]))
    w.op("MergedChanges", gql_conn([]))

    def runs(url):
        if "event=push" in url:
            run = {
                "id": 77,
                "name": "CI",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
                "created_at": "2026-10-14T07:00:00Z",
                "html_url": f"https://github.com/{project}/actions/runs/77",
            }
            return response({"workflow_runs": [run]})
        return response({"workflow_runs": []})

    w.route(f"{base}/actions/runs", runs)
    for path in broken:
        w.route(path, {"message": "boom"}, status=500)
    return w


class Forge:
    """Sends each request to the fake (or recording) of the repo it's about. `/user` goes to the
    first one, since it's the same account everywhere."""

    def __init__(self, by_name):
        self.by_name = by_name

    def __call__(self, method, url, headers, body=None):
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/graphql":
            name = json.loads(body)["variables"]["name"]
        elif parts.path.startswith("/repos/"):
            name = parts.path.split("/")[3]
        elif parts.path == "/search/issues":
            query = urllib.parse.parse_qs(parts.query)["q"][0]
            name = re.search(r"repo:[\w.-]+/([\w.-]+)", query).group(1)
        else:
            name = next(iter(self.by_name))
        return self.by_name[name](method, url, headers, body)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    git_repo(root / "widgets", "git@github.com:acme/widgets.git", CONFIG)
    git_repo(root / "gadgets", f"https://github.com/{QUIET}.git")
    git_repo(root / "scratch")  # a git repo with no origin: left out, and said so
    git_repo(root / ".hidden", "https://github.com/acme/hidden.git")
    (root / "notes").mkdir()  # not a repo: ignored
    cache = tmp_path / "cache"
    return {
        "root": root,
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(cache)},
        "markers": cache / "forge-tracker" / "catchup",
    }


def catchup(ws, *args, http=None, repo=None, now=NOW):
    out = io.StringIO()
    run = Run()
    http = http or Forge({"widgets": world(), "gadgets": quiet_world()})
    code = cli.main(
        ["--repo", str(repo or ws["root"]), "catchup", *args],
        http=http,
        run=run,
        now=now,
        environ=ws["environ"],
        out=out,
        tz=timezone.utc,
    )
    return code, out.getvalue(), run


SETUP = (
    "  (setup: no .claude/forge-tracker.local.md, so no label marks needs-human or decision items "
    "and rulings aren't picked out. `forge-tracker labels suggest` proposes one from this repo's "
    "labels) in acme/gadgets\n"
)


def test_a_workspace_is_one_report_with_waiting_on_you_first(ws):
    code, text, _ = catchup(ws)
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == (
        f"Workspace {ws['root']}: 2 repos (each repo's marker moves when all its reads succeeded)"
    )
    waiting = text[text.index("Waiting on you\n") : text.index("\n\nacme/")]
    assert waiting.splitlines()[1:4] == [
        "  acme/widgets#41 review requested: feat: GitHub adapter (draft no, conflicts: unknown)",
        "  acme/widgets#13 needs-human, updated 2026-10-14: pick a name",
        "  acme/widgets#12 mentioned, updated 2026-10-14: mentioned you",
    ]
    assert (
        "  standing: acme/widgets 1 review requested, 2 assigned, 1 needs-human, 1 decision "
        "(not updated in the window)"
    ) in waiting
    assert waiting.endswith(SETUP.rstrip("\n"))
    # The quiet repo is one line; the busy one has its sections, without its own Waiting on you.
    assert "\nacme/gadgets: nothing since 2026-10-08 08:00; main push: passed\n" in text
    busy = text[text.index("\nacme/widgets on github.com") :]
    assert "Waiting on you" not in busy
    assert "Merged since\n  #21  1 PR: #44 feat: read transport" in busy
    assert "main push run 900 (CI): FAILED (jobs: test)" in busy
    assert text.endswith(
        "Not read: scratch (no origin remote); set an origin remote, or "
        "`git config tracker.web <url>`, to include one\n"
    )
    assert "hidden" not in text and "notes" not in text
    # Each repo keeps its own marker.
    assert sorted(p.name for p in ws["markers"].iterdir()) == [
        "github.com_acme__gadgets_me.json",
        "github.com_acme__widgets_me.json",
    ]


def test_the_shared_setup_line_is_said_once_for_every_repo_it_applies_to(ws, tmp_path):
    git_repo(ws["root"] / "sprockets", "https://github.com/acme/sprockets.git")
    http = Forge(
        {
            "widgets": world(),
            "gadgets": quiet_world(),
            "sprockets": quiet_world("acme/sprockets"),
        }
    )
    code, text, _ = catchup(ws, "--no-mark", http=http)
    assert code == 0
    assert text.count("(setup:") == 1
    assert SETUP.replace("in acme/gadgets", "in acme/gadgets and acme/sprockets") in text
    assert "ruling_pattern isn't set" not in text
    assert not ws["markers"].exists()
    assert "(--no-mark, so every marker stays)" in text.splitlines()[0]


def test_a_note_every_repo_shares_says_all_of_them(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    worlds = {}
    for name in ("a", "b", "c"):
        git_repo(root / name, f"https://github.com/acme/{name}.git")
        worlds[name] = quiet_world(f"acme/{name}")
    out = io.StringIO()
    code = cli.main(
        ["--repo", str(root), "catchup", "--no-mark"],
        http=Forge(worlds),
        run=Run(),
        now=NOW,
        environ={"HOME": str(tmp_path)},
        out=out,
    )
    assert code == 0
    assert "labels) in all 3 repos\n" in out.getvalue()


def test_a_failed_repo_keeps_only_its_own_marker_and_exits_1(ws):
    broken = quiet_world(broken=(f"/repos/{QUIET}/issues",))
    code, text, _ = catchup(ws, http=Forge({"widgets": world(), "gadgets": broken}))
    assert code == 1
    assert "a read FAILED, so the marker stays for acme/gadgets" in text.splitlines()[0]
    assert "  acme/gadgets open_issues: FAILED (" in text
    assert "standing: none in the repos that read it (not acme/gadgets)" not in text
    assert "\nacme/gadgets on github.com" in text  # not collapsed: a part failed
    assert [p.name for p in ws["markers"].iterdir()] == ["github.com_acme__widgets_me.json"]


def test_a_repo_that_cant_be_opened_is_a_failed_line(ws):
    git_repo(ws["root"] / "lab", "git@gitlab.com:group/lab.git")
    code, text, _ = catchup(ws)
    assert code == 1
    assert "\nlab: FAILED (gitlab.com is gitlab, and forge-tracker reads only GitHub so far" in text
    assert len(list(ws["markers"].iterdir())) == 2


def test_a_tracker_web_child_counts_without_an_origin(ws):
    git_repo(ws["root"] / "mirror", **{"tracker.web": "https://github.com/acme/mirror"})
    http = Forge(
        {"widgets": world(), "gadgets": quiet_world(), "mirror": quiet_world("acme/mirror")}
    )
    code, text, _ = catchup(ws, "--no-mark", http=http)
    assert code == 0
    assert "\nacme/mirror: nothing since " in text


def test_repos_list_at_the_root_picks_the_repos(ws):
    (ws["root"] / ".claude").mkdir()
    (ws["root"] / ".claude" / "forge-tracker.local.md").write_text(
        "---\nrepos: [gadgets, missing]\n---\n"
    )
    code, text, _ = catchup(ws, "--no-mark")
    assert code == 1
    assert text.splitlines()[0].startswith(f"Workspace {ws['root']}: 2 repos")
    assert "acme/widgets" not in text
    assert "\nmissing: FAILED (" in text


def test_workspace_flag_and_json(ws, tmp_path):
    out = io.StringIO()
    code = cli.main(
        ["catchup", "--workspace", str(ws["root"]), "--json", "--since", "2d"],
        http=Forge({"widgets": world(), "gadgets": quiet_world()}),
        run=Run(),
        now=NOW,
        environ=ws["environ"],
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    data = json.loads(out.getvalue())
    assert data["marker"] == "since" and data["ok"] is True
    assert [r["name"] for r in data["repos"]] == ["acme/gadgets", "acme/widgets"]
    assert data["notes"][0]["repos"] == ["acme/gadgets"]
    assert data["skipped"] == [{"dir": "scratch", "reason": "no origin remote"}]
    assert not ws["markers"].exists()


def test_a_repo_still_reads_as_a_repo(ws):
    code, text, _ = catchup(ws, "--no-mark", repo=ws["root"] / "widgets")
    assert code == 0
    assert text.startswith("acme/widgets on github.com, ")


def test_other_commands_name_the_repos_and_exit_2(ws, capsys):
    code = cli.main(["--repo", str(ws["root"]), "next"], http=World(), run=Run(), now=NOW)
    assert code == 2
    err = capsys.readouterr().err
    assert "it's a workspace of 2 (gadgets, widgets)" in err
    assert "pass --repo <dir>" in err


def test_an_empty_folder_is_still_a_usage_error(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = cli.main(["--repo", str(empty), "catchup"], http=World(), run=Run(), now=NOW)
    assert code == 2
    assert "has no origin remote (or isn't a git repo)" in capsys.readouterr().err


def test_several_repo_flags_read_just_those_repos_as_one_report(ws, tmp_path):
    # The session's repos, wherever they sit: one in the workspace, one outside it.
    elsewhere = git_repo(tmp_path / "elsewhere" / "mirror", "https://github.com/acme/mirror.git")
    out = io.StringIO()
    code = cli.main(
        [
            "--repo",
            str(ws["root"] / "gadgets"),
            "--repo",
            str(elsewhere),
            "--repo",
            str(ws["root"] / "gadgets"),  # said twice, read once
            "catchup",
        ],
        http=Forge({"gadgets": quiet_world(), "mirror": quiet_world("acme/mirror")}),
        run=Run(),
        now=NOW,
        environ=ws["environ"],
        out=out,
        tz=timezone.utc,
    )
    assert code == 0
    text = out.getvalue()
    assert text.splitlines()[0] == (
        f"Workspace {tmp_path}: 2 repos (each repo's marker moves when all its reads succeeded)"
    )
    assert "\nacme/gadgets: nothing since " in text
    assert "\nacme/mirror: nothing since " in text
    assert "acme/widgets" not in text and "Not read:" not in text
    assert sorted(p.name for p in ws["markers"].iterdir()) == [
        "github.com_acme__gadgets_me.json",
        "github.com_acme__mirror_me.json",
    ]


def test_a_chosen_repo_that_isnt_one_is_a_failed_line(ws, tmp_path):
    out = io.StringIO()
    code = cli.main(
        ["--repo", str(ws["root"] / "gadgets"), "--repo", str(ws["root"] / "notes"), "catchup"],
        http=Forge({"gadgets": quiet_world()}),
        run=Run(),
        now=NOW,
        environ=ws["environ"],
        out=out,
        tz=timezone.utc,
    )
    assert code == 1
    assert "\nnotes: FAILED (" in out.getvalue()
    assert [p.name for p in ws["markers"].iterdir()] == ["github.com_acme__gadgets_me.json"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["next"], "`next` reads one repo: pass --repo once"),
        (["groom"], "`groom` reads one repo: pass --repo once"),
        (["catchup", "--workspace", "."], "pass --workspace or several --repo, not both"),
    ],
)
def test_several_repo_flags_are_for_catchup_alone(ws, capsys, argv, message):
    repos = ["--repo", str(ws["root"] / "gadgets"), "--repo", str(ws["root"] / "widgets")]
    code = cli.main([*repos, *argv], http=World(), run=Run(), now=NOW)
    assert code == 2
    assert message in capsys.readouterr().err


def test_record_takes_one_repo(ws, capsys):
    code = cli.main(
        ["--repo", str(ws["root"]), "--record", str(ws["root"] / "r.json"), "catchup"],
        http=World(),
        run=Run(),
        now=NOW,
    )
    assert code == 2
    assert "--record takes one repo" in capsys.readouterr().err


def test_replay_of_a_recorded_repo_in_a_workspace(tmp_path):
    """lokkju/skills as recorded (`--record`), beside a quiet fake repo, replayed with no
    network: its PR #38 (green, the owner's own, no review requested) waits on the owner."""
    http = ReplayHttp(FIXTURES / "github" / "catchup-lokkju-skills.json")
    root = tmp_path / "ws"
    root.mkdir()
    config_text = (
        "---\nlabels:\n  needs_human: needs-human\n  decision: decision\n"
        "tiers:\n  labels: [P0, P1, P2, P3]\nruling_pattern: '^Ruling'\n"
        'report_tags: ["session-decisions--v*", "forge-release--v*", "registry:app"]\n---\n'
    )
    git_repo(root / "skills", "https://github.com/lokkju/skills.git", config_text)
    # Fully set up, so no setup line: a label at ~ would get the offer to create it.
    quiet_config = (
        "---\nlabels:\n  needs_human: needs-human\n  decision: decision\nruling_pattern: ~\n---\n"
    )
    git_repo(root / "gadgets", f"https://github.com/{QUIET}.git", quiet_config)
    out = io.StringIO()
    code = cli.main(
        ["--repo", str(root), *http.meta["argv"]],
        http=Forge({"skills": http, "gadgets": quiet_world()}),
        run=Run(),
        now=parse_time(http.meta["recorded_at"]),
        environ={"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")},
        out=out,
        tz=timezone.utc,
    )
    text = out.getvalue()
    assert code == 0
    assert text.splitlines()[:5] == [
        f"Workspace {root}: 2 repos (--since; no marker is read or moved)",
        "",
        "Waiting on you",
        (
            "  lokkju/skills#38 yours to merge: docs(forge-tracker): draft the design "
            "(draft no, conflicts: no)"
        ),
        "  standing: none (not updated in the window)",
    ]
    assert "(setup:" not in text
    assert "\nacme/gadgets: nothing since 2026-10-14 04:19; main push: passed\n" in text
    skills = text[text.index("\nlokkju/skills on github.com") :]
    assert "  #38  Rulings on the design questions (2026-10-15):" in skills
    assert "  main push run 38679945924 (CI): passed" in skills
