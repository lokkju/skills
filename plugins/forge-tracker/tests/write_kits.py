"""One write kit per forge for the write half of the contract suite (test_contract.py). Each sets
up the same normalized scenario (an issue's state and labels, its blocking links, a branch) in
that forge's own read shapes, builds the write layer the CLI would (cli.make_writer), and records
every write call the layer sends as (method, path, body) instead of sending it. Nothing here can
reach a forge: GitHub's writes land in the fake Http, GitLab's in a fake `glab api` run."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone

from conftest import TOKEN, World, response
from forge_tracker import cli
from forge_tracker.config import Config
from forge_tracker.detect import Remote
from forge_tracker.forges import gitlab
from forge_tracker.transport import ReadTransport
from kits import GitHubKit, GitLabKit

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
WRITES = ("POST", "PUT", "PATCH", "DELETE")


class WriteWorld(World):
    """A fake forge for the write layer: GETs are served by the World's routes, and every write
    is kept in `sent` and answered from `answers` by (method, path), else by `fallback`, else 200
    with an empty object."""

    def __init__(self):
        super().__init__()
        self.sent = []
        self.answers = {}
        self.fallback = None

    def answer(self, method, path):
        if (method, path) in self.answers:
            return self.answers[(method, path)]
        if self.fallback is not None:
            found = self.fallback(method, path)
            if found is not None:
                return found
        return 200, {}

    def __call__(self, method, url, headers, body=None):
        path = urllib.parse.urlsplit(url).path
        if method in WRITES and path != "/graphql":
            self.sent.append((method, path, json.loads(body) if body else None))
            status, payload = self.answer(method, path)
            return response(payload, status)
        return super().__call__(method, url, headers, body)

    def glab(self, argv, **kwargs):
        """subprocess.run for `glab api`: each call is a write (the reads use the Http)."""
        assert argv[:2] == ["glab", "api"], f"unexpected command {argv}"
        method = argv[argv.index("--method") + 1]
        endpoint = argv[argv.index("--method") + 2]
        path = urllib.parse.urlsplit(endpoint).path if "://" in endpoint else "/" + endpoint
        body = kwargs.get("input")
        self.sent.append((method, path, json.loads(body) if body is not None else None))
        status, payload = self.answer(method, path)
        out = f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\r\n{json.dumps(payload)}"
        return subprocess.CompletedProcess(argv, 0 if status < 300 else 1, out.encode(), b"")


def _context(tmp_path, forge, host, adapter, http, token=None, **config):
    return cli.Context(
        repo_dir=str(tmp_path),
        remote=Remote(host, "acme/widgets"),
        config=Config(**config),
        forge=forge,
        adapter=adapter,
        recorder=None,
        http=http,
        token=token,
    )


def _environ(tmp_path):
    return {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}


class GitHubWriteKit(GitHubKit):
    W = "/repos/acme/widgets"  # the project's REST path, as the write calls show it

    def world(self):
        world = WriteWorld()
        world.labels_on = {}

        def absent_label(method, path):
            # GitHub answers 404 for a label the issue doesn't carry.
            found = re.fullmatch(rf"{self.W}/issues/(\d+)/labels/(.+)", path)
            if method == "DELETE" and found:
                labels = world.labels_on.get(int(found.group(1)))
                if labels is not None and urllib.parse.unquote(found.group(2)) not in labels:
                    return 404, {"message": "Label does not exist"}
            return None

        world.fallback = absent_label
        return world

    def writer(self, world, tmp_path, **config):
        ctx = _context(
            tmp_path, "github", "github.com", self.adapter(world), world, TOKEN, **config
        )
        return cli.make_writer(ctx, NOW, _environ(tmp_path), world.glab)

    def issue(self, world, number, state="open", labels=(), project="acme/widgets"):
        world.route(
            f"/repos/{project}/issues/{number}",
            {
                "id": (9000 if project == "acme/widgets" else 7000) + number,
                "number": number,
                "state": state,
                "labels": [{"name": n} for n in labels],
                "closed_at": None if state == "open" else "2026-10-15T00:00:00Z",
            },
        )
        if project == "acme/widgets":
            world.labels_on[number] = set(labels)

    def blocked(self, world, number, blockers):
        """`blockers`: (number, state, project or None) of each issue blocking #number."""
        world.route(
            f"{self.W}/issues/{number}/dependencies/blocked_by",
            [
                {
                    "id": (9000 if project is None else 7000) + blocker,
                    "number": blocker,
                    "state": state,
                    "closed_at": None,
                    "repository_url": f"https://api.github.com/repos/{project or 'acme/widgets'}",
                }
                for blocker, state, project in blockers
            ],
        )
        for blocker, state, project in blockers:
            self.issue(world, blocker, state, project=project or "acme/widgets")

    def branch(self, world, name, sha, protected=False):
        world.route(
            f"{self.W}/branches/{urllib.parse.quote(name, safe='/')}",
            {"name": name, "commit": {"sha": sha}, "protected": protected},
        )

    def repo_labels(self, world, names):
        world.route(f"{self.W}/labels", [{"name": n} for n in names])

    def fail(self, world, method, path, status, message):
        world.answers[(method, self.W + path)] = (status, {"message": message})


class GitLabWriteKit(GitLabKit):
    W = "/projects/acme%2Fwidgets"
    secret = "glpat-AbCdEfGhIjKlMnOpQrSt"

    def world(self):
        world = WriteWorld()
        world.route(self.P, {"id": 42, "default_branch": "main"})
        return world

    def adapter(self, world, page_cap=50, blocking=None):
        t = ReadTransport(api_base="https://gitlab.com/api/v4", http=world, page_cap=page_cap)
        return gitlab.GitLab(t, "acme/widgets", blocking=blocking)

    def writer(self, world, tmp_path, **config):
        adapter = self.adapter(world, blocking=config.get("blocking"))
        ctx = _context(tmp_path, "gitlab", "gitlab.com", adapter, world, **config)
        return cli.make_writer(ctx, NOW, _environ(tmp_path), world.glab)

    def issue(self, world, number, state="open", labels=(), project="acme/widgets"):
        mine = project == "acme/widgets"
        world.route(
            f"/api/v4/projects/{urllib.parse.quote(project, safe='')}/issues/{number}",
            {
                "id": (9000 if mine else 7000) + number,
                "iid": number,
                "project_id": 42 if mine else 77,
                "state": "opened" if state == "open" else "closed",
                "labels": list(labels),
                "closed_at": None if state == "open" else "2026-10-15T00:00:00Z",
            },
        )

    def blocked(self, world, number, blockers, link_type="is_blocked_by"):
        world.route(
            f"{self.P}/issues/{number}/links",
            [
                {
                    "iid": blocker,
                    "issue_link_id": 500 + blocker,
                    "project_id": 42 if project is None else 77,
                    "references": {"full": f"{project or 'acme/widgets'}#{blocker}"},
                    "state": "opened" if state == "open" else "closed",
                    "link_type": link_type,
                }
                for blocker, state, project in blockers
            ],
        )
        for blocker, state, project in blockers:
            self.issue(world, blocker, state, project=project or "acme/widgets")

    def branch(self, world, name, sha, protected=False):
        world.route(
            f"{self.P}/repository/branches/{urllib.parse.quote(name, safe='')}",
            {"name": name, "commit": {"id": sha}, "protected": protected},
        )

    def repo_labels(self, world, names):
        world.route(f"{self.P}/labels", [{"name": n} for n in names])

    def fail(self, world, method, path, status, message):
        world.answers[(method, self.W + path)] = (status, {"message": message})


WRITE_KITS = [GitHubWriteKit(), GitLabWriteKit()]
