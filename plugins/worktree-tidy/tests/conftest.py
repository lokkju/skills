import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

FAKE_GH = textwrap.dedent(
    """\
    #!{python}
    # A stand-in for `gh api graphql`: answers from the JSON file in $FAKE_GH_STATE and logs
    # each call's arguments to $FAKE_GH_LOG.
    import json, os, sys

    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    state = json.load(open(os.environ["FAKE_GH_STATE"]))
    if state.get("fail"):
        print(state["fail"], file=sys.stderr)
        sys.exit(1)
    fields = dict(a.split("=", 1) for a in args[args.index("api") + 2 :][1::2])
    repo = {{}}
    i = 0
    while f"b{{i}}" in fields:
        info = state["branches"].get(fields[f"b{{i}}"], {{}})
        repo[f"p{{i}}"] = {{
            "nodes": [
                {{
                    "number": p["number"],
                    "state": p["state"],
                    "url": "https://github.com/o/r/pull/%d" % p["number"],
                    "headRefOid": p["head"],
                    "isCrossRepository": p.get("cross", False),
                }}
                for p in info.get("prs", [])
            ]
        }}
        repo[f"r{{i}}"] = {{"name": fields[f"b{{i}}"]}} if info.get("remote") else None
        i += 1
    print(json.dumps({{"data": {{"repository": repo}}}}))
    """
)


def git(path, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No user or system git config, no real cache, and no real gh."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        '[protocol "file"]\n\tallow = always\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no real transcripts
    for name in (
        "WORKTREE_TIDY_SESSION_HOURS",
        "WORKTREE_TIDY_ROOTS",
        "WORKTREE_TIDY_OFFLINE",
        "WORKTREE_TIDY_BUDGET",
        "WORKTREE_TIDY_TTL",
        "WORKTREE_TIDY_DETACHED_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeGh:
    def __init__(self, base: Path, monkeypatch):
        self.path = base / "bin" / "gh"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(FAKE_GH.format(python=sys.executable))
        self.path.chmod(0o755)
        self.state_file = base / "gh-state.json"
        self.log = base / "gh-log.jsonl"
        self.log.write_text("")
        self.state = {"branches": {}}
        self.save()
        monkeypatch.setenv("FAKE_GH_STATE", str(self.state_file))
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log))
        monkeypatch.setenv("WORKTREE_TIDY_GH", str(self.path))

    def save(self):
        self.state_file.write_text(json.dumps(self.state))

    def branch(self, name, prs=(), remote=False):
        self.state["branches"][name] = {"prs": list(prs), "remote": remote}
        self.save()

    def fail(self, message="HTTP 502"):
        self.state["fail"] = message
        self.save()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return FakeGh(tmp_path, monkeypatch)


class Repo:
    """A clone of a local bare origin, on main with one commit, tied to GitHub repo o/<name>."""

    def __init__(self, base: Path, name: str = "repo"):
        self.origin = base / f"{name}-origin.git"
        self.path = base / name
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.path)],
            check=True,
            capture_output=True,
        )
        (self.path / "README").write_text("hi\n")
        git(self.path, "add", "README")
        git(self.path, "commit", "-q", "-m", "init")
        git(self.path, "push", "-q", "origin", "main")
        git(self.path, "config", "worktree-tidy.repo", f"o/{name}")

    def worktree(self, rel, branch=None, detach=False):
        path = self.path / rel
        args = ["worktree", "add", "-q"]
        if detach:
            args += ["--detach", str(path), "main"]
        else:
            args += ["-b", branch, str(path), "main"]
        git(self.path, *args)
        return path

    def commit(self, wt, name="change"):
        (Path(wt) / name).write_text(name + "\n")
        git(wt, "add", name)
        git(wt, "commit", "-q", "-m", name)
        return git(wt, "rev-parse", "HEAD")

    def age(self, wt, days):
        """Back-date a worktree's admin files, as if nobody had touched it for `days`."""
        admin = Path(git(wt, "rev-parse", "--absolute-git-dir"))
        stamp = time.time() - days * 86400
        for name in ("HEAD", "index", "logs/HEAD"):
            if (admin / name).exists():
                os.utime(admin / name, (stamp, stamp))


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)
