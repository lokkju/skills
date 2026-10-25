"""The read transport: the only way an adapter reaches a forge.

Reads can't write. Every request goes through `check_read` before it's sent: GET is allowed, and
so is a POST to the forge's GraphQL endpoint whose document's operations are all queries, which
`graphql_operations` finds by tokenizing the document, not by matching strings. Anything else
raises before the network is touched. Writes go through a separate transport defined in
`writes.py`, which no adapter or report imports.

Lists are read to the end or fail: `get_all` and `graphql_all` follow pagination to the last
page, and a page cap that is hit raises ForgeError with the cap in the reason.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .model import ForgeError

USER_AGENT = "forge-tracker"
DEFAULT_PAGE_CAP = 50
PER_PAGE = 100
ERROR_LIMIT = 300

# Token shapes that must never reach output: GitHub (classic and fine-grained), GitLab, and any
# Authorization header value.
_TOKEN_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{10,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+(\s+\S+)?)"),
]


def redact(text: str, secrets: Sequence[str] = (), limit: int = ERROR_LIMIT) -> str:
    """Cut text to `limit` characters and replace anything token-shaped, or any of `secrets`,
    with `***`. Secrets are replaced before cutting, so a cut can't leave half a token."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pattern.sub("***", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


# --- GraphQL: find every operation's type without trusting string matches ---------------------

_PUNCT = set("!$&()[]{}:=@|")
_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")


class GraphQLRefused(ForgeError):
    pass


def _tokens(document: str) -> list[str]:
    """GraphQL's lexical grammar, enough to tell names and braces from strings and comments."""
    out: list[str] = []
    i, n = 0, len(document)
    while i < n:
        c = document[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            while i < n and document[i] not in "\r\n":
                i += 1
        elif document.startswith('"""', i):
            end = i + 3
            while True:
                end = document.find('"""', end)
                if end < 0:
                    raise GraphQLRefused("GraphQL document has an unterminated block string")
                if document[end - 1] != "\\":
                    break
                end += 3
            out.append("<string>")
            i = end + 3
        elif c == '"':
            i += 1
            while True:
                if i >= n or document[i] in "\r\n":
                    raise GraphQLRefused("GraphQL document has an unterminated string")
                if document[i] == "\\":
                    i += 2
                    continue
                if document[i] == '"':
                    i += 1
                    break
                i += 1
            out.append("<string>")
        elif document.startswith("...", i):
            out.append("...")
            i += 3
        elif c in _PUNCT:
            out.append(c)
            i += 1
        else:
            match = _NAME.match(document, i) or _NUMBER.match(document, i)
            if not match:
                raise GraphQLRefused(f"GraphQL document has an unexpected character {c!r}")
            out.append(match.group(0))
            i = match.end()
    return out


def graphql_operations(document: str) -> list[str]:
    """The type of each executable definition in the document: query, mutation, subscription or
    fragment. A shorthand `{ ... }` is a query. Raises GraphQLRefused when it can't parse."""
    tokens = _tokens(document)
    kinds: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "{":
            kind = "query"
        elif token in ("query", "mutation", "subscription", "fragment"):
            kind = token
            i += 1
            # Skip the name, variable definitions and directives up to the selection set.
            parens = 0
            while i < len(tokens) and not (tokens[i] == "{" and parens == 0):
                if tokens[i] == "(":
                    parens += 1
                elif tokens[i] == ")":
                    parens -= 1
                    if parens < 0:
                        raise GraphQLRefused("GraphQL document has unbalanced parentheses")
                elif tokens[i] == "}":
                    raise GraphQLRefused("GraphQL document has an unexpected '}'")
                i += 1
            if i >= len(tokens):
                raise GraphQLRefused(f"GraphQL {kind} has no selection set")
        else:
            raise GraphQLRefused(f"GraphQL document has {token!r} where a definition should start")
        # tokens[i] is the opening brace of the selection set; consume it balanced.
        depth = 0
        while i < len(tokens):
            if tokens[i] == "{":
                depth += 1
            elif tokens[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            raise GraphQLRefused("GraphQL document has unbalanced braces")
        kinds.append(kind)
        i += 1
    if not kinds:
        raise GraphQLRefused("GraphQL document is empty")
    return kinds


def check_read(method: str, url: str, body: Optional[bytes], graphql_url: Optional[str]) -> None:
    """Refuse anything that could write. Raised before any request is sent."""
    method = method.upper()
    if method == "GET":
        if body:
            raise ForgeError("refused: a read GET carries no body")
        return
    if method != "POST":
        raise ForgeError(f"refused: {method} isn't a read")
    if not graphql_url or url != graphql_url:
        raise ForgeError(f"refused: POST is allowed only to the GraphQL endpoint, not {url}")
    try:
        payload = json.loads(body or b"")
        document = payload["query"]
        if not isinstance(document, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise ForgeError("refused: a GraphQL POST needs a JSON body with a query string") from None
    kinds = graphql_operations(document)
    for kind in kinds:
        if kind not in ("query", "fragment"):
            raise ForgeError(f"refused: the GraphQL document contains a {kind}")
    if "query" not in kinds:
        raise ForgeError("refused: the GraphQL document has no query")


# --- HTTP ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lowercased names
    body: bytes


# (method, url, headers, body) -> Response. Network errors raise ForgeError.
Http = Callable[[str, str, Mapping[str, str], Optional[bytes]], Response]


def urllib_http(cafile: Optional[str] = None, timeout: float = 30) -> Http:
    context = ssl.create_default_context(cafile=cafile)

    def http(
        method: str, url: str, headers: Mapping[str, str], body: Optional[bytes] = None
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return Response(
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return Response(
                error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()
            )
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ForgeError(redact(f"{method} {url} failed: {reason}")) from None

    return http


_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


def next_link(headers: Mapping[str, str]) -> Optional[str]:
    for url, rel in _LINK.findall(headers.get("link", "")):
        if "next" in rel.split():
            return url
    return None


@dataclass
class ReadTransport:
    """REST GETs and query-only GraphQL POSTs against one forge API, with auth and pagination."""

    api_base: str
    http: Http
    token: Optional[str] = field(default=None, repr=False)
    graphql_url: Optional[str] = None
    page_cap: int = DEFAULT_PAGE_CAP
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        headers.update(self.extra_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _secrets(self) -> list[str]:
        return [self.token] if self.token else []

    def url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        url = path if path.startswith(("https://", "http://")) else self.api_base + path
        if params:
            query = urllib.parse.urlencode(
                [(k, v) for k, v in params.items() if v is not None], safe=":/,"
            )
            url += ("&" if "?" in url else "?") + query
        return url

    def send(self, method: str, url: str, body: Optional[bytes] = None) -> tuple[Any, Response]:
        check_read(method, url, body, self.graphql_url)
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.http(method, url, headers, body)
        try:
            parsed = json.loads(response.body) if response.body else None
        except ValueError:
            parsed = None
        if not 200 <= response.status < 300:
            message = parsed.get("message") if isinstance(parsed, dict) else None
            if not message:
                message = response.body.decode("utf-8", "replace")
            path = urllib.parse.urlsplit(url).path
            raise ForgeError(
                redact(f"HTTP {response.status} from {path}: {message}", self._secrets())
            )
        if parsed is None and response.body:
            raise ForgeError(f"{urllib.parse.urlsplit(url).path} returned something not JSON")
        return parsed, response

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.send("GET", self.url(path, params))[0]

    def get_all(
        self, path: str, params: Optional[Mapping[str, Any]] = None, key: Optional[str] = None
    ) -> list[Any]:
        """Every item across every page. `key` names the list inside an object body (GitHub's
        `workflow_runs`, `items`); without it the body itself is the list."""
        url: Optional[str] = self.url(path, dict({"per_page": PER_PAGE}, **(params or {})))
        items: list[Any] = []
        pages = 0
        while url:
            if pages == self.page_cap:
                raise ForgeError(
                    f"page cap of {self.page_cap} pages hit reading {urllib.parse.urlsplit(url).path}"
                )
            body, response = self.send("GET", url)
            pages += 1
            if key is not None:
                if not isinstance(body, dict) or not isinstance(body.get(key), list):
                    raise ForgeError(f"{path} returned no {key!r} list")
                if body.get("incomplete_results"):
                    raise ForgeError(f"{path} reported incomplete results")
                body = body[key]
            if not isinstance(body, list):
                raise ForgeError(f"{path} returned something other than a list")
            items.extend(body)
            url = next_link(response.headers) or _next_page(url, response.headers)
        return items

    def graphql(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Any:
        if not self.graphql_url:
            raise ForgeError("this forge has no GraphQL endpoint configured")
        body = json.dumps({"query": query, "variables": dict(variables or {})}).encode()
        data, _ = self.send("POST", self.graphql_url, body)
        if not isinstance(data, dict):
            raise ForgeError("GraphQL returned something other than an object")
        if data.get("errors"):
            first = data["errors"][0]
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise ForgeError(redact(f"GraphQL error: {message}", self._secrets()))
        return data.get("data")

    def graphql_all(
        self,
        query: str,
        variables: Mapping[str, Any],
        path: Sequence[str],
        stop: Optional[Callable[[Any], bool]] = None,
    ) -> list[Any]:
        """Every node of the connection at `path`, following `pageInfo` with `$cursor`.

        `stop(node)` ends the read early once it's true for a node, for connections ordered so
        that everything after that node is out of range (newest first, read back to a time). A
        stop is the end of the range, not a cap, so it isn't a failure.
        """
        cursor: Optional[str] = None
        nodes: list[Any] = []
        for _ in range(self.page_cap):
            data = self.graphql(query, dict(variables, cursor=cursor))
            connection = data
            for key in path:
                connection = connection.get(key) if isinstance(connection, dict) else None
            if not isinstance(connection, dict) or "nodes" not in connection:
                raise ForgeError(f"GraphQL returned no {'.'.join(path)} connection")
            for node in connection["nodes"]:
                if stop is not None and stop(node):
                    return nodes
                nodes.append(node)
            info = connection.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return nodes
            cursor = info.get("endCursor")
        raise ForgeError(f"page cap of {self.page_cap} pages hit reading {'.'.join(path)}")


def _next_page(url: str, headers: Mapping[str, str]) -> Optional[str]:
    """GitLab's X-Next-Page, for forges that send it instead of (or as well as) Link."""
    page = headers.get("x-next-page", "").strip()
    if not page:
        return None
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "page"]
    query.append(("page", page))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# --- auth -----------------------------------------------------------------------------------------


def gh_token(host: str, run: Callable[..., Any] = subprocess.run) -> str:
    """The token `gh` holds for `host`. It travels only in the Authorization header."""
    try:
        result = run(
            ["gh", "auth", "token", "--hostname", host],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        raise ForgeError("gh isn't on PATH; it supplies the GitHub token") from None
    except subprocess.TimeoutExpired:
        raise ForgeError("gh auth token timed out") from None
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        detail = redact(result.stderr or "", [token])
        raise ForgeError(f"gh has no token for {host} (run gh auth login): {detail}".rstrip(": "))
    return token


# --- recording --------------------------------------------------------------------------------


# REST fields no adapter reads that make up most of a recording's bulk: the repository object
# embedded in every Actions run, reaction counts, and the API's `*_url` link templates.
_BULK_KEYS = {"repository", "head_repository", "head_commit", "reactions", "_links"}
_KEPT_URLS = {"html_url", "issue_url", "url"}


def thin(value: Any) -> Any:
    """A REST body without the bulk fields above. GraphQL bodies are recorded whole."""
    if isinstance(value, list):
        return [thin(v) for v in value]
    if isinstance(value, dict):
        return {
            k: thin(v)
            for k, v in value.items()
            if k not in _BULK_KEYS and (not k.endswith("_url") or k in _KEPT_URLS)
        }
    return value


class RecordingHttp:
    """Wraps an Http and keeps every exchange, scrubbed, for writing out as a fixture.

    Only the Link and X-Next-Page headers are kept, every body goes through `redact`'s token
    patterns, and REST bodies are thinned (`thin`). The Authorization header is never recorded.
    """

    def __init__(self, inner: Http, secrets: Sequence[str] = ()):
        self.inner = inner
        self.secrets = list(secrets)
        self.exchanges: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: Optional[bytes] = None
    ) -> Response:
        response = self.inner(method, url, headers, body)
        text = response.body.decode("utf-8", "replace")
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "***")
        for pattern in _TOKEN_PATTERNS[:3]:
            text = pattern.sub("***", text)
        try:
            recorded_body: Any = json.loads(text) if text else None
        except ValueError:
            recorded_body = text
        if method == "GET":
            recorded_body = thin(recorded_body)
        self.exchanges.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body) if body else None,
                "status": response.status,
                "headers": {
                    k: v for k, v in response.headers.items() if k in ("link", "x-next-page")
                },
                "response": recorded_body,
            }
        )
        return response

    def dump(self, **meta: Any) -> str:
        """The fixture file: `meta` (when it was recorded, the command line) and the exchanges,
        sorted so that the threads' interleaving doesn't change the file."""
        exchanges = sorted(
            self.exchanges, key=lambda e: (e["url"], json.dumps(e["body"], sort_keys=True))
        )
        return json.dumps(dict(meta, exchanges=exchanges), indent=1, sort_keys=True) + "\n"
