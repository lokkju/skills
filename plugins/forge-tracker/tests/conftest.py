import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest
from forge_tracker.transport import ReadTransport, Response

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "ghp_FAKEfakeFAKEfake0123456789"


def response(body, status=200, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, raw)


class ReplayHttp:
    """Serves a recorded fixture: each request must match a recorded one. GETs match on method
    and URL; GraphQL POSTs match on the operation name and variables, so reformatting a query
    doesn't invalidate the recording."""

    def __init__(self, path):
        data = json.loads(Path(path).read_text())
        self.meta = {k: v for k, v in data.items() if k != "exchanges"}
        self.exchanges = {}
        for e in data["exchanges"]:
            self.exchanges[self._key(e["method"], e["url"], e["body"])] = e
        self.calls = []

    @staticmethod
    def _key(method, url, body):
        if body is None:
            return (method, url, None)
        name = re.search(r"query\s+(\w+)", body["query"]).group(1)
        return (method, url, name, json.dumps(body.get("variables"), sort_keys=True))

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers)))
        key = self._key(method, url, json.loads(body) if body else None)
        if key not in self.exchanges:
            raise AssertionError(f"no recorded exchange for {key}")
        e = self.exchanges[key]
        return response(e["response"], e["status"], e["headers"])


class World:
    """A fake GitHub API: REST routes by path (query ignored unless a route asks for it) and
    GraphQL by operation name. Every call is kept in `calls`."""

    def __init__(self):
        self.rest = {}
        self.graphql = {}
        self.calls = []

    def route(self, path, body, status=200, headers=None):
        self.rest[path] = (
            (lambda url: response(body, status, headers)) if not callable(body) else body
        )
        return self

    def op(self, name, data):
        self.graphql[name] = data
        return self

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/graphql":
            payload = json.loads(body)
            name = re.search(r"(?:query)\s+(\w+)", payload["query"]).group(1)
            handler = self.graphql.get(name)
            if handler is None:
                raise AssertionError(f"no GraphQL handler for {name}")
            data = handler(payload["variables"]) if callable(handler) else handler
            return response({"data": data} if "errors" not in (data or {}) else data)
        handler = self.rest.get(parts.path)
        if handler is None:
            raise AssertionError(f"no route for {method} {parts.path}")
        return handler(url)


class FakeRun:
    """subprocess.run for gh and git: canned answers keyed by argv prefix."""

    def __init__(self, origin="https://github.com/acme/widgets.git", token=TOKEN):
        self.answers = {
            ("gh", "auth", "token"): (0, token + "\n", ""),
        }
        self.origin = origin
        self.calls = []
        self.kwargs = []

    def answer(self, prefix, returncode=0, stdout="", stderr=""):
        self.answers[tuple(prefix)] = (returncode, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if argv[:1] == ["git"] and argv[3:5] == ["remote", "get-url"]:
            if self.origin is None:
                return subprocess.CompletedProcess(argv, 2, "", "fatal: not a git repository")
            return subprocess.CompletedProcess(argv, 0, self.origin + "\n", "")
        if argv[:1] == ["git"]:
            rest = tuple(argv[3:])
            for prefix, (code, out, err) in self.answers.items():
                if rest[: len(prefix)] == prefix:
                    return subprocess.CompletedProcess(argv, code, out, err)
            raise AssertionError(f"unexpected git call {argv}")
        for prefix, (code, out, err) in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, err)
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def transport_for():
    def make(http, page_cap=50):
        return ReadTransport(
            api_base="https://api.github.com",
            http=http,
            token=TOKEN,
            graphql_url="https://api.github.com/graphql",
            page_cap=page_cap,
        )

    return make


@pytest.fixture
def repo_dir(tmp_path):
    def make(config_text=None):
        directory = tmp_path / "repo"
        directory.mkdir(exist_ok=True)
        if config_text is not None:
            (directory / ".claude").mkdir(exist_ok=True)
            (directory / ".claude" / "forge-tracker.local.md").write_text(config_text)
        return str(directory)

    return make
