import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import SCRIPTS, Repo, git
from worktree_tidy import cli, forge, scan


def run_scan(roots, cwd=None, budget=30, **kw):
    kw.setdefault("gh", os.environ.get("WORKTREE_TIDY_GH", "gh"))
    roots = [str(r) for r in roots]
    return scan.scan(roots, str(cwd or roots[0]), budget, **kw)


def kinds(result):
    return {Path(i.path).name: i.kind for _, i in result.stale()}


def looks(result):
    return {Path(i.path).name: i.why for r in result.repos for i in r.look}


def merged(gh, branch, head, number=1, remote=False, state="MERGED"):
    gh.branch(branch, [{"number": number, "state": state, "head": head}], remote=remote)


def test_merged_clean_branch_gone_is_stale(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    result = run_scan([repo.path])
    assert kinds(result) == {"feat": "merged"}
    item = result.repos[0].stale[0]
    assert item.branch == "feat" and item.why == "PR #1 merged"


def test_closed_unmerged_is_stale(repo, gh):
    wt = repo.worktree(".wt/gone", "gone")
    merged(gh, "gone", repo.commit(wt), state="CLOSED")
    assert kinds(run_scan([repo.path])) == {"gone": "closed"}


def test_merged_but_dirty_is_a_look(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    (wt / "scratch.txt").write_text("unsaved\n")
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert "1 changed or untracked path" in looks(result)["feat"]


def test_merged_with_commits_after_the_pr_is_a_look(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt, "one"))
    repo.commit(wt, "two")
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert "commits the PR doesn't" in looks(result)["feat"]


def test_merged_older_checkout_than_pr_head_is_stale(repo, gh):
    """The PR picked up commits after this checkout's (pushed from elsewhere): nothing here is
    lost by removing it."""
    wt = repo.worktree(".wt/feat", "feat")
    first = repo.commit(wt, "one")
    later = repo.commit(wt, "two")
    git(wt, "reset", "-q", "--hard", first)
    merged(gh, "feat", later)
    assert kinds(run_scan([repo.path])) == {"feat": "merged"}


def test_unknown_pr_head_is_a_look(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    repo.commit(wt)
    merged(gh, "feat", "0" * 40)
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert "couldn't confirm" in looks(result)["feat"]


def test_merged_with_branch_still_on_remote_is_stale(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt), remote=True)
    result = run_scan([repo.path])
    assert kinds(result) == {"feat": "merged"}
    assert result.repos[0].stale[0].why == "PR #1 merged; branch still on the remote"


def test_merged_on_remote_but_dirty_is_a_look(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt), remote=True)
    (wt / "scratch.txt").write_text("unsaved\n")
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert looks(result)["feat"].startswith("PR #1 merged; branch still on the remote; 1 changed")


def test_closed_with_branch_still_on_remote_is_a_look(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt), remote=True, state="CLOSED")
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert looks(result)["feat"] == "PR #1 closed; branch still on the remote"


def fake_proc(base, *entries):
    """A stand-in for /proc: (pid, cwd, comm) entries; a cwd of None makes an unreadable one."""
    root = base / "proc"
    root.mkdir(exist_ok=True)
    (root / "self").mkdir(exist_ok=True)
    for pid, cwd, comm in entries:
        d = root / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        if cwd is not None:
            (d / "cwd").symlink_to(os.path.realpath(cwd))
    return root


def transcript(projects, path, name, hours_ago=0.0, agent=False):
    """A Claude Code transcript for `path`, last written `hours_ago`."""
    if agent:
        sub = projects / "-somewhere-else" / "parent-session" / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"{name}.meta.json").write_text(json.dumps({"worktreePath": str(path)}))
        file = sub / f"{name}.jsonl"
    else:
        folder = projects / scan.inuse.encode(str(path))
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / f"{name}.jsonl"
    file.write_text("{}\n")
    stamp = time.time() - hours_ago * 3600
    os.utime(file, (stamp, stamp))


def test_encode_matches_claude_code():
    assert (
        scan.inuse.encode("/home/u/p/skills/.claude/worktrees/x")
        == "-home-u-p-skills--claude-worktrees-x"
    )


def test_process_in_a_merged_worktree_makes_it_in_use(repo, gh, tmp_path):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    (wt / "sub").mkdir()
    proc = fake_proc(
        tmp_path,
        (4242, wt / "sub", "vim"),  # a subdirectory counts
        (4243, None, "hidden"),  # unreadable: skipped
        (os.getpid(), wt, "python3"),  # the scanner itself: ignored
        (4244, tmp_path, "bash"),  # elsewhere
    )
    result = run_scan([repo.path], proc_root=str(proc))
    assert kinds(result) == {} and looks(result) == {}
    (item,) = result.repos[0].in_use
    assert item.kind == "in use" and item.branch == "feat"
    assert item.why == "pid 4242 (vim) running in it"
    assert result.in_use_note is None
    assert gh.calls() == []  # nothing left to ask GitHub about
    assert "  in use: .wt/feat [feat]: pid 4242 (vim) running in it" in cli.render(result)
    assert "1 in use" in cli.summary(result)
    assert cli.commands(result) == []


def test_in_use_beats_needs_a_look(repo, gh, tmp_path):
    wt = repo.worktree(".claude/worktrees/scratch", detach=True)
    repo.commit(wt)
    repo.age(wt, 10)
    orphan = repo.path / ".wt" / "leftover"
    orphan.mkdir(parents=True)
    proc = fake_proc(tmp_path, (7, wt, "node"), (8, orphan, "zsh"))
    result = run_scan([repo.path], proc_root=str(proc))
    assert looks(result) == {}
    assert {Path(i.path).name: i.why for i in result.repos[0].in_use} == {
        "scratch": "pid 7 (node) running in it",
        "leftover": "pid 8 (zsh) running in it",
    }


def test_recent_session_transcript_makes_it_in_use(repo, gh, tmp_path):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    projects = tmp_path / "claude" / "projects"
    transcript(projects, wt, "1111-session", hours_ago=2)
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert result.repos[0].in_use[0].why == "session 1111-session active 2h ago"


def test_old_session_transcript_is_ignored_unless_the_window_covers_it(repo, gh, tmp_path):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    transcript(tmp_path / "claude" / "projects", wt, "old", hours_ago=30)
    assert kinds(run_scan([repo.path])) == {"feat": "merged"}
    result = run_scan([repo.path], session_hours=48)
    assert kinds(result) == {}
    assert result.repos[0].in_use[0].why == "session old active 1d ago"


def test_recent_agent_transcript_makes_it_in_use(repo, gh, tmp_path):
    wt = repo.worktree(".claude/worktrees/agent-abc", "worktree-agent-abc")
    merged(gh, "worktree-agent-abc", repo.commit(wt))
    projects = tmp_path / "elsewhere"
    transcript(projects, wt, "agent-abc", hours_ago=0.5, agent=True)
    transcript(projects, repo.path / "nope", "agent-other", agent=True)
    result = run_scan([repo.path], projects=str(projects))
    assert kinds(result) == {}
    assert result.repos[0].in_use[0].why == "subagent agent-abc active 30m ago"


def test_process_check_that_cant_run_says_so(repo, gh, tmp_path, monkeypatch):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    monkeypatch.setattr(scan.inuse, "_from_lsof", lambda *a: None)
    result = run_scan([repo.path], proc_root=str(tmp_path / "no-proc"))
    assert kinds(result) == {"feat": "merged"}
    assert result.in_use_note == (
        f"couldn't look for processes using worktrees (no {tmp_path / 'no-proc'}, lsof failed)"
    )
    assert "may still have a program running in it" in cli.render(result)


def test_processes_without_lsof(tmp_path):
    procs, note = scan.inuse.processes(str(tmp_path / "none"), lsof=str(tmp_path / "no-lsof"))
    assert procs is None and note.startswith("couldn't look for processes")


def test_processes_from_lsof(tmp_path):
    lsof = tmp_path / "lsof"
    lsof.write_text(
        "#!/bin/sh\nprintf 'p10\\ncvim\\nfcwd\\nn/work/a\\np11\\ncbash\\nfcwd\\nn/work/b\\n'\n"
    )
    lsof.chmod(0o755)
    procs, note = scan.inuse.processes(str(tmp_path / "none"), skip={11}, lsof=str(lsof))
    assert note is None
    assert [(p.pid, p.command, p.cwd) for p in procs] == [(10, "vim", "/work/a")]


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs procfs")
def test_real_process_in_a_worktree(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    child = subprocess.Popen(["sleep", "30"], cwd=wt)
    try:
        result = run_scan([repo.path])
    finally:
        child.kill()
        child.wait()
    assert kinds(result) == {}
    assert result.repos[0].in_use[0].why == f"pid {child.pid} (sleep) running in it"


def test_open_pr_or_no_pr_is_left_alone(repo, gh):
    a = repo.worktree(".wt/a", "a")
    head = repo.commit(a)
    gh.branch(
        "a",
        [
            {"number": 2, "state": "OPEN", "head": head},
            {"number": 1, "state": "MERGED", "head": head},
        ],
    )
    repo.worktree(".wt/b", "b")
    result = run_scan([repo.path])
    assert kinds(result) == {} and looks(result) == {}


def test_cross_repo_prs_are_ignored(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    gh.branch("feat", [{"number": 9, "state": "MERGED", "head": repo.commit(wt), "cross": True}])
    assert kinds(run_scan([repo.path])) == {}


def test_gh_failure_never_reads_as_gone(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    gh.fail("HTTP 502: Bad Gateway")
    result = run_scan([repo.path])
    assert kinds(result) == {}
    report = result.repos[0]
    assert report.forge_note == "couldn't check PRs: gh failed: HTTP 502: Bad Gateway"
    assert [Path(p).name for p in report.unchecked] == ["feat"]


def test_gh_missing(repo, tmp_path):
    repo.worktree(".wt/feat", "feat")
    result = run_scan([repo.path], gh=str(tmp_path / "no-such-gh"))
    assert result.repos[0].forge_note == "couldn't check PRs: gh isn't on PATH"


def test_offline_skips_gh(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    result = run_scan([repo.path], offline=True)
    assert kinds(result) == {} and gh.calls() == []
    assert result.repos[0].forge_note == "PRs not checked (offline)"


def test_non_github_origin_is_not_checked(repo, gh):
    git(repo.path, "config", "--unset", "worktree-tidy.repo")
    repo.worktree(".wt/feat", "feat")
    result = run_scan([repo.path])
    assert gh.calls() == []
    assert result.repos[0].forge_note == "PRs not checked (origin isn't on GitHub)"


def test_answers_are_cached(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    run_scan([repo.path])
    run_scan([repo.path])
    assert len(gh.calls()) == 1
    run_scan([repo.path], refresh=True)
    assert len(gh.calls()) == 2
    run_scan([repo.path], ttl=0.000001)
    assert len(gh.calls()) == 3


def test_one_query_covers_every_branch(repo, gh):
    for name in ("a", "b", "c"):
        repo.worktree(f".wt/{name}", name)
    run_scan([repo.path])
    (call,) = gh.calls()
    values = [a for a in call if a.startswith(("b0=", "b1=", "b2="))]
    assert sorted(values) == ["b0=a", "b1=b", "b2=c"]


def test_detached_old_clean_on_a_ref_is_stale(repo, gh):
    wt = repo.worktree(".claude/worktrees/scratch", detach=True)
    repo.age(wt, 10)
    result = run_scan([repo.path])
    assert kinds(result) == {"scratch": "detached"}
    assert "unused 10 days" in result.repos[0].stale[0].why
    assert gh.calls() == []  # nothing to ask GitHub


def test_detached_recent_is_left_alone(repo, gh):
    repo.worktree(".claude/worktrees/scratch", detach=True)
    assert kinds(run_scan([repo.path])) == {}


def test_detached_days_threshold(repo, gh):
    wt = repo.worktree(".claude/worktrees/scratch", detach=True)
    repo.age(wt, 3)
    assert kinds(run_scan([repo.path])) == {}
    assert kinds(run_scan([repo.path], days=2)) == {"scratch": "detached"}


def test_detached_on_an_unreferenced_commit_is_a_look(repo, gh):
    wt = repo.worktree(".claude/worktrees/scratch", detach=True)
    repo.commit(wt)
    repo.age(wt, 10)
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert "which no branch has" in looks(result)["scratch"]


def test_detached_dirty_is_a_look(repo, gh):
    wt = repo.worktree(".claude/worktrees/scratch", detach=True)
    (wt / "README").write_text("edited\n")
    repo.age(wt, 10)
    result = run_scan([repo.path])
    assert kinds(result) == {}
    assert "changed" in looks(result)["scratch"]


def test_prunable(repo, gh):
    wt = repo.worktree(".wt/lost", "lost")
    shutil.rmtree(wt)
    result = run_scan([repo.path])
    assert kinds(result) == {"lost": "prunable"}
    assert cli.commands(result)[-1] == f"git -C {repo.path} worktree prune"


def test_orphan_directory_is_a_look(repo, gh):
    (repo.path / ".wt" / "leftover").mkdir(parents=True)
    (repo.path / ".claude" / "worktrees" / "old").mkdir(parents=True)
    (repo.path / ".claude" / "worktrees" / "old" / "notes.txt").write_text("x\n")
    result = run_scan([repo.path])
    assert looks(result) == {
        "leftover": "an empty directory under .wt that git doesn't list",
        "old": "a directory under .claude/worktrees that git doesn't list",
    }
    assert cli.commands(result) == []


def test_locked_and_current_worktrees_are_left_alone(repo, gh):
    locked = repo.worktree(".wt/locked", "locked")
    git(repo.path, "worktree", "lock", str(locked))
    here = repo.worktree(".wt/here", "here")
    for name, wt in (("locked", locked), ("here", here)):
        merged(gh, name, repo.commit(wt, name))
    result = run_scan([repo.path], cwd=here)
    assert kinds(result) == {}
    assert {Path(i.path).name: i.why for i in result.repos[0].in_use} == {
        "locked": "locked",
        "here": "this session's working directory",
    }
    git(repo.path, "worktree", "lock", "--reason", "claude agent x (pid 1)", str(here))
    result = run_scan([repo.path], cwd=locked)
    assert {Path(i.path).name: i.why for i in result.repos[0].in_use} == {
        "locked": "this session's working directory; locked",
        "here": "locked: claude agent x (pid 1)",
    }


def test_main_checkout_is_never_listed(repo, gh):
    merged(gh, "main", git(repo.path, "rev-parse", "HEAD"))
    result = run_scan([repo.path])
    assert result.repos[0].linked == 0 and kinds(result) == {}


def test_scope_from_inside_a_worktree_is_the_whole_repo(repo, gh):
    feat = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(feat))
    here = repo.worktree(".wt/here", "here")
    result = run_scan([here], cwd=here)
    assert [r.path for r in result.repos] == [str(repo.path)]
    assert kinds(result) == {"feat": "merged"}


def test_workspace_folder_scans_each_child_repo(tmp_path, gh):
    ws = tmp_path / "ws"
    ws.mkdir()
    one, two = Repo(ws, "one"), Repo(ws, "two")
    (ws / "notes").mkdir()
    hidden = ws / ".hidden"
    hidden.mkdir()
    (hidden / ".git").mkdir()
    a = one.worktree(".wt/a", "a")
    merged(gh, "a", one.commit(a))
    b = two.worktree(".claude/worktrees/b", detach=True)
    two.age(b, 30)
    result = run_scan([ws])
    assert [r.name for r in result.repos] == ["one", "two"]  # bare origins and .hidden skipped
    assert kinds(result) == {"a": "merged", "b": "detached"}
    assert "in one, two" in cli.summary(result)


def test_repos_are_counted_once(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    result = run_scan([repo.path, wt])
    assert len(result.repos) == 1


def test_time_budget_leaves_the_rest_unclassified(repo, gh, monkeypatch):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    real = scan.gitio.dirty

    def slow(*args, **kw):
        raise scan.OutOfTime

    monkeypatch.setattr(scan.gitio, "dirty", slow)
    result = run_scan([repo.path])
    monkeypatch.setattr(scan.gitio, "dirty", real)
    assert result.out_of_time and kinds(result) == {}
    assert [Path(p).name for p in result.repos[0].unchecked] == ["feat"]
    assert "time budget ran out" in cli.render(result)


def test_zero_budget_finds_nothing(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    result = run_scan([repo.path], budget=0.001)
    assert result.out_of_time and kinds(result) == {}


def run_lines(lines):
    script = "set -e\n" + "\n".join(line for line in lines if not line.startswith("#"))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def test_printed_commands_remove_what_they_list(repo, gh):
    feat = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(feat))
    scratch = repo.worktree(".claude/worktrees/scratch", detach=True)
    repo.age(scratch, 10)
    keep = repo.worktree(".wt/keep", "keep")
    result = run_scan([repo.path])
    lines = cli.commands(result)
    assert lines[0] == "# repo"
    done = run_lines(lines)
    assert done.returncode == 0, done.stderr
    assert not feat.exists() and not scratch.exists() and keep.exists()
    branches = git(repo.path, "branch", "--format=%(refname:short)").split()
    assert "feat" not in branches and "keep" in branches


def test_printed_commands_refuse_a_worktree_dirtied_since(repo, gh):
    feat = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(feat))
    lines = cli.commands(run_scan([repo.path]))
    (feat / "new-work.txt").write_text("not saved anywhere\n")
    done = run_lines(lines)
    assert done.returncode == 0
    assert f"worktree-tidy: left {feat} (not clean, or git refused)" in done.stderr
    assert (feat / "new-work.txt").exists()
    assert "feat" in git(repo.path, "branch", "--format=%(refname:short)").split()


def test_submodule_worktree_gets_a_guarded_force(tmp_path, gh):
    lib = Repo(tmp_path, "lib")
    repo = Repo(tmp_path, "app")
    git(repo.path, "submodule", "add", "-q", str(lib.origin), "lib")
    git(repo.path, "commit", "-q", "-m", "add lib")
    wt = repo.worktree(".wt/feat", "feat")
    git(wt, "submodule", "update", "-q", "--init")
    merged(gh, "feat", repo.commit(wt))
    refused = git(repo.path, "worktree", "remove", str(wt), check=False)
    assert wt.exists(), refused  # git won't remove it without --force
    result = run_scan([repo.path])
    assert kinds(result) == {"feat": "merged"}
    (line,) = [c for c in cli.commands(result) if "worktree remove" in c]
    assert "worktree remove --force" in line and line.startswith('{ [ -z "$(git -C')
    assert run_lines(cli.commands(result)).returncode == 0
    assert not wt.exists()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/lokkju/skills.git", "github.com/lokkju/skills"),
        ("https://github.com/lokkju/skills", "github.com/lokkju/skills"),
        ("git@github.com:lokkju/skills.git", "github.com/lokkju/skills"),
        ("ssh://git@github.com/lokkju/skills.git", "github.com/lokkju/skills"),
        ("ssh://git@ssh.github.com:443/lokkju/skills.git", "github.com/lokkju/skills"),
        ("https://user@github.example.com/team/app", "github.example.com/team/app"),
        ("https://gitlab.com/a/b.git", None),
        ("/srv/git/app.git", None),
        ("git@github.com:just-one.git", None),
    ],
)
def test_parse_slug(url, expected):
    slug = forge.parse_slug(url)
    assert (f"{slug.host}/{slug}" if slug else None) == expected


def test_parse_slug_override():
    assert str(forge.parse_slug("git@alias:x/y", "a/b")) == "a/b"
    assert forge.parse_slug("", "ghe.example.com/a/b").host == "ghe.example.com"


def hook(cwd, env=None):
    event = json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd), "source": "startup"})
    return subprocess.run(
        [str(SCRIPTS / "worktree-tidy"), "hook"],
        input=event,
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_hook_reports_stale_worktrees(repo, gh, tmp_path):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt))
    done = hook(repo.path)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    note = out["systemMessage"]
    assert note.startswith("worktree-tidy: 1 stale worktree (1 merged) in repo.")
    assert "/worktree-tidy lists them" in note
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "don't run the removal commands yourself" in context
    listed = Path(note.split("(last list: ")[1].rstrip(")."))
    assert str(listed).startswith(str(tmp_path / "data" / "reports"))
    assert "worktree remove" in listed.read_text()


def test_hook_is_silent_when_nothing_is_stale(repo, gh):
    repo.worktree(".wt/feat", "feat")
    done = hook(repo.path)
    assert done.returncode == 0 and done.stdout == ""


def test_hook_says_when_prs_couldnt_be_checked(repo, gh):
    repo.worktree(".wt/feat", "feat")
    gh.fail()
    out = json.loads(hook(repo.path).stdout)
    assert out["systemMessage"] == "worktree-tidy: couldn't check PRs in repo."


def test_hook_outside_any_repo_is_silent(tmp_path, gh):
    empty = tmp_path / "empty"
    empty.mkdir()
    done = hook(empty)
    assert done.returncode == 0 and done.stdout == ""


def test_hook_survives_garbage_input(tmp_path):
    done = subprocess.run(
        [str(SCRIPTS / "worktree-tidy"), "hook"],
        input="not json",
        capture_output=True,
        check=False,
        text=True,
        cwd=tmp_path,
    )
    assert done.returncode == 0


def test_hook_roots_from_env(tmp_path, gh):
    ws = tmp_path / "ws"
    ws.mkdir()
    one = Repo(ws, "one")
    wt = one.worktree(".claude/worktrees/x", detach=True)
    one.age(wt, 30)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = json.loads(hook(elsewhere, {"WORKTREE_TIDY_ROOTS": str(ws)}).stdout)
    assert "1 stale worktree (1 detached) in one" in out["systemMessage"]


def test_report_cli(repo, gh):
    wt = repo.worktree(".wt/feat", "feat")
    merged(gh, "feat", repo.commit(wt), number=42)
    done = subprocess.run(
        [str(SCRIPTS / "worktree-tidy"), "report", "--root", str(repo.path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "  stale merged: .wt/feat [feat]: PR #42 merged" in done.stdout
    assert "removal is your call" in done.stdout
    data = json.loads(
        subprocess.run(
            [str(SCRIPTS / "worktree-tidy"), "report", "--json", "--root", str(repo.path)],
            capture_output=True,
            check=False,
            text=True,
        ).stdout
    )
    assert data["repos"][0]["stale"][0]["kind"] == "merged"
    assert any("worktree remove" in c for c in data["commands"])
