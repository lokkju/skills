import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/forge-tracker/scripts"))

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


class RouteHttp:
    """Serves a fixture world written from a forge's documented response shapes (for forges with
    no live target to record from yet): `{"base": ..., "routes": [...]}`, each route a `path`
    under the base, an optional `query` whose pairs the request must carry, and a `response` or
    `pages` (served by the `page` parameter, with `Link` and `X-Next-Page` to the next). The first
    matching route wins, so specific ones come first. Only GETs are served."""

    def __init__(self, path):
        data = json.loads(Path(path).read_text())
        self.base = data["base"].rstrip("/")
        self.routes = data["routes"]
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers)))
        assert method == "GET" and body is None, f"a fixture world serves GETs only: {method}"
        assert url.startswith(self.base + "/"), f"{url} isn't under {self.base}"
        parts = urllib.parse.urlsplit(url[len(self.base) :])
        query = dict(urllib.parse.parse_qsl(parts.query))
        for route in self.routes:
            if route["path"] != parts.path:
                continue
            if any(query.get(k) != str(v) for k, v in (route.get("query") or {}).items()):
                continue
            if "pages" not in route:
                return response(route["response"], route.get("status", 200))
            page = int(query.get("page", 1))
            pages = route["pages"]
            headers_out = {}
            if page < len(pages):
                rest = [(k, v) for k, v in query.items() if k != "page"] + [("page", page + 1)]
                next_url = f"{self.base}{parts.path}?{urllib.parse.urlencode(rest)}"
                headers_out = {"Link": f'<{next_url}>; rel="next"', "X-Next-Page": str(page + 1)}
            return response(pages[page - 1], headers=headers_out)
        raise AssertionError(f"no route for GET {parts.path}?{parts.query}")


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

    def __init__(self, origin="https://github.com/acme/widgets.git", token=TOKEN, git_config=None):
        self.answers = {
            ("gh", "auth", "token"): (0, token + "\n", ""),
            # `git config --get` exits 1 for an unset key.
            ("config", "--get"): (1, "", ""),
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n", ""),
        }
        for key, value in (git_config or {}).items():
            self.answers[("config", "--get", key)] = (0, value + "\n", "")
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
        rest = tuple(argv[3:]) if argv[:1] == ["git"] else tuple(argv)
        # The longest matching prefix wins, so a specific answer overrides a default one.
        matches = [p for p in self.answers if rest[: len(p)] == p]
        if not matches:
            raise AssertionError(
                f"unexpected {'git call' if argv[:1] == ['git'] else 'command'} {argv}"
            )
        code, out, err = self.answers[max(matches, key=len)]
        return subprocess.CompletedProcess(argv, code, out, err)


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
