"""The write layer: a short list of named operations, and the only module that can write.

Each operation takes a JSON payload and prints what it did. When the config sets
`write_wrapper:`, the operation runs the wrapper instead of calling the forge: `<wrapper> <op>`
with the same JSON on stdin, in the repo root. The wrapper does the write itself and exits 0, or
exits non-zero to refuse; forge-tracker never sends a write of its own while a wrapper is set.

The write transport lives here and nowhere else, so no adapter or report can reach it. Each forge
has an explicit allowlist of write calls (a method and a path under the project's own REST path):
the transport sends nothing else, never a GET and never to GraphQL. GitHub writes go out through
urllib with the token the reads use; GitLab writes go through `glab api` with an explicit
`--method`, so glab's login applies, by an Http defined here (the read side's glab Http sends GETs
only). Reads an operation needs first go through the read transport. Every operation, written,
refused, unsupported or failed, is appended to a 0600 log in the local cache.

So far the operations are the ones `/groom` proposes: `close`, `labels`, `milestone`, `unlink`,
`delete-branch` and `link-add`; `label-create`, which first-use setup runs for the labels the
user approved; and `link-remove`, which with `link-add` is how an agent records blocking
(`forge-tracker link add|remove`). `file` and `rule` (discovered work, recording a ruling) come
with the working rules.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from . import state
from .model import ForgeError, IssueRef, Link, iso
from .transport import USER_AGENT, Http, ReadTransport, Response, parse_included, redact

OPS = (
    "close",
    "labels",
    "milestone",
    "unlink",
    "delete-branch",
    "label-create",
    "link-add",
    "link-remove",
)
LINK_OPS = ("link-add", "link-remove")
WRITE_METHODS = ("POST", "PATCH", "DELETE")
# GitLab updates an issue (its state, labels and milestone) with PUT.
GITLAB_METHODS = ("POST", "PUT", "DELETE")

# The write calls each forge's operations make: (method, path pattern under the project's REST
# path). The transport refuses any other call before it reaches the network.
GITHUB_ROUTES = (
    ("POST", r"/issues/\d+/comments"),
    ("PATCH", r"/issues/\d+"),
    ("POST", r"/issues/\d+/labels"),
    ("DELETE", r"/issues/\d+/labels/[^/]+"),
    ("POST", r"/issues/\d+/dependencies/blocked_by"),
    ("DELETE", r"/issues/\d+/dependencies/blocked_by/\d+"),
    ("DELETE", r"/git/refs/heads/.+"),
    ("POST", r"/labels"),
)
GITLAB_ROUTES = (
    ("POST", r"/issues/\d+/notes"),
    ("PUT", r"/issues/\d+"),
    ("POST", r"/issues/\d+/links"),
    ("DELETE", r"/issues/\d+/links/\d+"),
    ("DELETE", r"/repository/branches/[^/]+"),
    ("POST", r"/labels"),
)
WRAPPER_TIMEOUT = 120
# GitHub's limits on a label.
LABEL_NAME_MAX, LABEL_DESCRIPTION_MAX = 50, 100


class WriteRefused(Exception):
    """A write that wasn't sent: a bad payload, a precondition that no longer holds, or a
    write-wrapper's refusal. The message says why."""


class WriteUnsupported(WriteRefused):
    """A write this forge, or this instance's tier, can't make at all. It shows as unsupported for
    that write only; the other operations still run."""


@dataclass(frozen=True)
class Result:
    op: str
    status: str  # written | refused | unsupported | failed
    detail: str
    via: str  # forge | wrapper

    @property
    def ok(self) -> bool:
        return self.status == "written"


# --- payloads -----------------------------------------------------------------------------------


def _int(payload: Mapping[str, Any], key: str, errors: list[str]) -> Optional[int]:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(f"{key} must be a positive issue number")
        return None
    return value


def _text(payload: Mapping[str, Any], key: str, errors: list[str], required: bool = True) -> Any:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be a non-empty string")
        return None
    return value


def _names(payload: Mapping[str, Any], key: str, errors: list[str]) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        errors.append(f"{key} must be a list of label names")
        return []
    return value


_KEYS = {
    "close": {"issue", "comment", "change"},
    "labels": {"issue", "add", "remove"},
    "milestone": {"issue", "milestone"},
    "unlink": {"issue", "blocker", "blocker_project", "label"},
    "delete-branch": {"branch", "sha"},
    "label-create": {"name", "color", "description"},
    "link-add": {"issue", "blocker", "blocker_project"},
    "link-remove": {"issue", "blocker", "blocker_project"},
}
_PROJECT = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# GitLab projects sit in groups and subgroups: group/sub/project.
_NESTED_PROJECT = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def validate(op: str, payload: Any, project: str, nested: bool = False) -> dict[str, Any]:
    """The payload checked for `op`, or WriteRefused naming every problem. A payload may name
    its `repo` (owner/repo); one naming another project is refused. With `nested` (GitLab), a
    `blocker_project` may be a group/sub/project path."""
    if op not in OPS:
        raise WriteRefused(f"{op!r} isn't a write operation ({', '.join(OPS)})")
    if not isinstance(payload, dict):
        raise WriteRefused("the payload must be a JSON object")
    errors: list[str] = []
    unknown = sorted(set(payload) - _KEYS[op] - {"repo"})
    if unknown:
        errors.append(f"unknown key {', '.join(unknown)}")
    if "repo" in payload and payload["repo"] != project:
        errors.append(f"the payload is for {payload['repo']!r}, not {project}")
    if op == "close":
        _int(payload, "issue", errors)
        _text(payload, "comment", errors)
        if "change" in payload:
            _int(payload, "change", errors)
    elif op == "labels":
        _int(payload, "issue", errors)
        add, remove = _names(payload, "add", errors), _names(payload, "remove", errors)
        if not add and not remove:
            errors.append("labels needs something to add or remove")
        if set(add) & set(remove):
            errors.append("a label can't be both added and removed")
    elif op == "milestone":
        _int(payload, "issue", errors)
        milestone = payload.get("milestone", "missing")
        if milestone == "missing":
            errors.append("milestone must be {number, title} or null (to clear it)")
        elif milestone is not None:
            if not isinstance(milestone, dict) or set(milestone) - {"number", "title"}:
                errors.append("milestone must be {number, title} or null (to clear it)")
            else:
                _int(milestone, "number", errors)
    elif op == "unlink":
        _int(payload, "issue", errors)
        has_blocker, has_label = "blocker" in payload, "label" in payload
        if has_blocker == has_label:
            errors.append("unlink takes either blocker (a link) or label (the blocked label)")
        elif has_blocker:
            _int(payload, "blocker", errors)
            _text(payload, "blocker_project", errors, required=False)
        else:
            _text(payload, "label", errors)
    elif op == "delete-branch":
        _text(payload, "branch", errors)
        _text(payload, "sha", errors)
    elif op == "label-create":
        name = _text(payload, "name", errors)
        if name is not None and (name != name.strip() or len(name) > LABEL_NAME_MAX):
            errors.append(f"name must have no outer spaces and at most {LABEL_NAME_MAX} characters")
        color = payload.get("color")
        if not isinstance(color, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", color):
            errors.append("color must be six hex digits, like d93f0b")
        description = payload.get("description")
        if description is not None and (
            not isinstance(description, str) or len(description) > LABEL_DESCRIPTION_MAX
        ):
            errors.append(f"description must be a string of at most {LABEL_DESCRIPTION_MAX}")
    elif op in LINK_OPS:
        issue, blocker = _int(payload, "issue", errors), _int(payload, "blocker", errors)
        other = payload.get("blocker_project")
        shape = _NESTED_PROJECT if nested else _PROJECT
        if other is not None and (not isinstance(other, str) or not shape.fullmatch(other)):
            errors.append(f"blocker_project must be {'group/project' if nested else 'owner/repo'}")
        elif issue is not None and issue == blocker and other in (None, project):
            errors.append(f"#{issue} can't block itself")
    if errors:
        raise WriteRefused("; ".join(errors))
    return dict(payload)


def blocker_ref(payload: Mapping[str, Any], project: str) -> str:
    """The blocker a link payload names, written the way the forge writes it."""
    other = payload.get("blocker_project")
    if other and other != project:
        return f"{other}#{payload['blocker']}"
    return f"#{payload['blocker']}"


# --- the write transport ------------------------------------------------------------------------


@dataclass
class WriteTransport:
    """Write calls under one project's REST path: one of `methods` and, when `routes` is set, only
    a call it lists. Nothing else is sent."""

    api_base: str
    repo_path: str
    http: Http
    token: Optional[str] = field(default=None, repr=False)
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    methods: Sequence[str] = WRITE_METHODS
    routes: Sequence[tuple[str, str]] = ()
    auth_scheme: str = "Bearer"

    def refusal(self, method: str, path: str) -> Optional[str]:
        """Why (method, path) isn't a write this transport sends; None when it is."""
        if method not in self.methods:
            return f"the write transport doesn't send {method}"
        if not path.startswith(self.repo_path + "/"):
            return f"{path} is outside {self.repo_path}"
        rest = path[len(self.repo_path) :]
        if self.routes and not any(
            method == m and re.fullmatch(pattern, rest) for m, pattern in self.routes
        ):
            return f"{method} {path} isn't one of the write calls forge-tracker makes"
        return None

    def send(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        method = method.upper()
        problem = self.refusal(method, path)
        if problem:
            raise ForgeError(f"refused: {problem}")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        headers.update(self.extra_headers)
        if self.token:
            headers["Authorization"] = f"{self.auth_scheme} {self.token}"
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        response = self.http(method, self.api_base + path, headers, raw)
        try:
            parsed = json.loads(response.body) if response.body else None
        except ValueError:
            parsed = None
        if not 200 <= response.status < 300:
            message = None
            if isinstance(parsed, dict):
                message = parsed.get("message") or parsed.get("error")
                if message is not None and not isinstance(message, str):
                    message = json.dumps(message)  # GitLab's {"message": {"title": [...]}}
            if not message:
                message = response.body.decode("utf-8", "replace")
            secrets = [self.token] if self.token else []
            raise WriteFailed(
                response.status, redact(f"HTTP {response.status} from {path}: {message}", secrets)
            )
        return parsed


class WriteFailed(ForgeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _quote(value: str, safe: str = "") -> str:
    return urllib.parse.quote(value, safe=safe)


def glab_write_http(
    host: str,
    default_base: Optional[str] = None,
    run: Callable[..., Any] = subprocess.run,
    timeout: float = 60,
) -> Http:
    """An Http that sends each write through `glab api --method <M>`, a JSON body on stdin, so
    glab's own login and host settings apply and no token passes through forge-tracker. A URL
    under `default_base` goes to glab as a path, as on the read side. It sends GITLAB_METHODS
    only; the WriteTransport in front of it has already checked the call against its routes."""
    prefix = (default_base or "").rstrip("/") + "/"

    def http(
        method: str, url: str, headers: Mapping[str, str], body: Optional[bytes] = None
    ) -> Response:
        method = method.upper()
        if method not in GITLAB_METHODS:
            raise ForgeError(f"refused: glab writes don't send {method}")
        endpoint = url[len(prefix) :] if default_base and url.startswith(prefix) else url
        argv = ["glab", "api", "--hostname", host, "--include", "--method", method, endpoint]
        kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout}
        if body is not None:
            argv += ["--header", "Content-Type: application/json", "--input", "-"]
            kwargs["input"] = body
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            result = run(argv, **kwargs)
        except FileNotFoundError:
            raise ForgeError(
                "glab isn't on PATH; it writes to GitLab (install it and run glab auth login)"
            ) from None
        except subprocess.TimeoutExpired:
            raise ForgeError(f"glab api timed out after {timeout:.0f}s") from None
        stdout = (
            result.stdout if isinstance(result.stdout, bytes) else (result.stdout or "").encode()
        )
        stderr = (
            result.stderr
            if isinstance(result.stderr, str)
            else (result.stderr or b"").decode("utf-8", "replace")
        )
        parsed = parse_included(stdout)
        if parsed is None:
            detail = redact(stderr) or f"exit {result.returncode}"
            path = urllib.parse.urlsplit(url).path
            raise ForgeError(f"glab api {method} {path} failed: {detail}")
        return parsed

    return http


class GitHubWrites:
    """The operations against GitHub's REST API. Reads they need first (is the issue still
    open, where is the branch now) go through the read transport."""

    def __init__(self, reader: ReadTransport, writer: WriteTransport, project: str):
        self.r = reader
        self.w = writer
        self.project = project
        self.repo_path = f"/repos/{project}"

    def _issue(self, number: int) -> dict[str, Any]:
        raw = self.r.get(f"{self.repo_path}/issues/{number}")
        if not isinstance(raw, dict):
            raise ForgeError(f"#{number} returned no issue")
        return raw

    def close(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        if self._issue(number).get("state") == "closed":
            raise WriteRefused(f"#{number} is already closed; nothing written")
        self.w.send("POST", f"{self.repo_path}/issues/{number}/comments", {"body": p["comment"]})
        self.w.send(
            "PATCH",
            f"{self.repo_path}/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )
        return f"commented on and closed #{number}"

    def labels(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        done = []
        if p.get("add"):
            self.w.send(
                "POST", f"{self.repo_path}/issues/{number}/labels", {"labels": list(p["add"])}
            )
            done.append("added " + ", ".join(p["add"]))
        removed, absent = [], []
        for name in p.get("remove") or []:
            try:
                self.w.send("DELETE", f"{self.repo_path}/issues/{number}/labels/{_quote(name)}")
                removed.append(name)
            except WriteFailed as error:
                if error.status != 404:
                    raise
                absent.append(name)
        if removed:
            done.append("removed " + ", ".join(removed))
        if absent:
            done.append(", ".join(absent) + " already off")
        return f"#{number}: " + "; ".join(done)

    def milestone(self, p: Mapping[str, Any]) -> str:
        number, milestone = p["issue"], p["milestone"]
        self.w.send(
            "PATCH",
            f"{self.repo_path}/issues/{number}",
            {"milestone": milestone["number"] if milestone else None},
        )
        if milestone:
            return f"#{number}: milestone set to {milestone.get('title') or milestone['number']}"
        return f"#{number}: milestone cleared"

    def unlink(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        if "label" in p:
            try:
                self.w.send(
                    "DELETE", f"{self.repo_path}/issues/{number}/labels/{_quote(p['label'])}"
                )
            except WriteFailed as error:
                if error.status != 404:
                    raise
                raise WriteRefused(
                    f"#{number} no longer has {p['label']}; nothing written"
                ) from None
            return f"#{number}: removed {p['label']}"
        project = p.get("blocker_project") or self.project
        blocker = self.r.get(f"/repos/{project}/issues/{p['blocker']}")
        if not isinstance(blocker, dict) or not blocker.get("id"):
            raise ForgeError(f"{project}#{p['blocker']} returned no id")
        if blocker.get("state") != "closed":
            raise WriteRefused(f"{blocker_ref(p, self.project)} is open again; the link stays")
        self.w.send(
            "DELETE",
            f"{self.repo_path}/issues/{number}/dependencies/blocked_by/{int(blocker['id'])}",
        )
        ref = f"{p['blocker_project']}#{p['blocker']}" if p.get("blocker_project") else None
        return f"#{number}: no longer blocked by {ref or '#' + str(p['blocker'])}"

    def _blocker_id(self, p: Mapping[str, Any]) -> int:
        project = p.get("blocker_project") or self.project
        blocker = self.r.get(f"/repos/{project}/issues/{p['blocker']}")
        if not isinstance(blocker, dict) or not blocker.get("id"):
            raise ForgeError(f"{blocker_ref(p, self.project)} returned no id")
        return int(blocker["id"])

    def link_add(self, p: Mapping[str, Any]) -> str:
        """`POST /issues/{n}/dependencies/blocked_by` with the blocker's id. The Writer has
        already checked that the link is new and both sides are open issues."""
        number = p["issue"]
        self.w.send(
            "POST",
            f"{self.repo_path}/issues/{number}/dependencies/blocked_by",
            {"issue_id": self._blocker_id(p)},
        )
        return f"#{number}: now blocked by {blocker_ref(p, self.project)}"

    def link_remove(self, p: Mapping[str, Any]) -> str:
        """`DELETE /issues/{n}/dependencies/blocked_by/{id}`, whatever the blocker's state.
        `unlink` is the `/groom` fix, which removes a link only once its blocker closed."""
        number = p["issue"]
        self.w.send(
            "DELETE",
            f"{self.repo_path}/issues/{number}/dependencies/blocked_by/{self._blocker_id(p)}",
        )
        return f"#{number}: no longer blocked by {blocker_ref(p, self.project)}"

    def delete_branch(self, p: Mapping[str, Any]) -> str:
        name = p["branch"]
        branch = self.r.get(f"{self.repo_path}/branches/{_quote(name, '/')}")
        head = ((branch or {}).get("commit") or {}).get("sha") if isinstance(branch, dict) else None
        if head != p["sha"]:
            raise WriteRefused(
                f"{name} moved to {(head or '?')[:7]} since {p['sha'][:7]} was checked; kept"
            )
        if branch.get("protected"):
            raise WriteRefused(f"{name} is protected; kept")
        self.w.send("DELETE", f"{self.repo_path}/git/refs/heads/{_quote(name, '/')}")
        return f"deleted branch {name} (was {p['sha'][:7]})"

    def label_create(self, p: Mapping[str, Any]) -> str:
        body = {"name": p["name"], "color": p["color"].lower()}
        if p.get("description"):
            body["description"] = p["description"]
        self.w.send("POST", f"{self.repo_path}/labels", body)
        return f"created label {p['name']} (#{body['color']})"


def _done(number: int, done: list[str]) -> str:
    return f"#{number}: " + "; ".join(done)


class GitLabWrites:
    """The operations against GitLab's REST v4, sent through `glab api`. An issue's state, labels
    and milestone change with one PUT; blocking is an issue link. With the `relates_to+blocked`
    convention (`blocking:` in the config, for tiers without `blocks` links) a link-add writes a
    `relates_to` link and the blocked label, and a link-remove takes the label off with the
    issue's last link."""

    def __init__(
        self,
        reader: ReadTransport,
        writer: WriteTransport,
        project: str,
        convention: bool = False,
        blocked_label: str = "blocked",
    ):
        self.r = reader
        self.w = writer
        self.project = project
        self.repo_path = "/projects/" + _quote(project)
        self.convention = convention
        self.blocked_label = blocked_label
        self._project_id: Optional[int] = None

    def _issue(self, number: int) -> dict[str, Any]:
        raw = self.r.get(f"{self.repo_path}/issues/{number}")
        if not isinstance(raw, dict) or "state" not in raw:
            raise ForgeError(f"#{number} returned no issue")
        return raw

    def _labels_of(self, number: int) -> list[str]:
        raw = self._issue(number).get("labels") or []
        return [v if isinstance(v, str) else (v or {}).get("name") or "" for v in raw]

    def _put(self, number: int, body: Mapping[str, Any]) -> None:
        self.w.send("PUT", f"{self.repo_path}/issues/{number}", body)

    def close(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        if self._issue(number).get("state") == "closed":
            raise WriteRefused(f"#{number} is already closed; nothing written")
        self.w.send("POST", f"{self.repo_path}/issues/{number}/notes", {"body": p["comment"]})
        self._put(number, {"state_event": "close"})
        return f"commented on and closed #{number}"

    def labels(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        add, remove = list(p.get("add") or []), list(p.get("remove") or [])
        commas = [n for n in add + remove if "," in n]
        if commas:
            raise WriteRefused(
                f"GitLab takes labels as a comma-separated list, so {commas[0]!r} can't be "
                "sent; nothing written"
            )
        current = self._labels_of(number)
        removed = [n for n in remove if n in current]
        absent = [n for n in remove if n not in current]
        body: dict[str, str] = {}
        if add:
            body["add_labels"] = ",".join(add)
        if removed:
            body["remove_labels"] = ",".join(removed)
        if body:
            self._put(number, body)
        done = []
        if add:
            done.append("added " + ", ".join(add))
        if removed:
            done.append("removed " + ", ".join(removed))
        if absent:
            done.append(", ".join(absent) + " already off")
        return _done(number, done)

    def milestone(self, p: Mapping[str, Any]) -> str:
        """`milestone_id` is the milestone's global id (the adapter reads it as the number);
        0 clears it."""
        number, milestone = p["issue"], p["milestone"]
        self._put(number, {"milestone_id": milestone["number"] if milestone else 0})
        if milestone:
            return f"#{number}: milestone set to {milestone.get('title') or milestone['number']}"
        return f"#{number}: milestone cleared"

    def _own_project_id(self) -> Optional[int]:
        if self._project_id is None:
            raw = self.r.get(self.repo_path)
            if isinstance(raw, dict) and raw.get("id") is not None:
                self._project_id = int(raw["id"])
        return self._project_id

    def _link_project(self, raw: Mapping[str, Any]) -> Optional[str]:
        """None when a linked issue is in this project, else its project path."""
        full = (raw.get("references") or {}).get("full") or ""
        if "#" in full:
            path = full.rsplit("#", 1)[0]
            return None if path in ("", self.project) else path
        project_id = raw.get("project_id")
        if project_id is None or project_id == self._own_project_id():
            return None
        return f"project {project_id}"

    def _links(self, number: int) -> list[dict[str, Any]]:
        raw = self.r.get_all(f"{self.repo_path}/issues/{number}/links")
        return [link for link in raw if isinstance(link, dict)]

    def _counts(self, link: Mapping[str, Any]) -> bool:
        """Whether a link on the blocked issue records blocking here: `is_blocked_by`, and
        `relates_to` under the convention."""
        kinds = ("is_blocked_by", "relates_to") if self.convention else ("is_blocked_by",)
        return link.get("link_type") in kinds

    def _find(self, number: int, p: Mapping[str, Any], kinds: Sequence[str]) -> Optional[dict]:
        """The link on #number to the payload's blocker whose type is one of `kinds`."""
        other = p.get("blocker_project")
        other = None if other in (None, self.project) else other
        for link in self._links(number):
            if (
                link.get("iid") == p["blocker"]
                and link.get("link_type") in kinds
                and self._link_project(link) == other
            ):
                return link
        return None

    def _delete_link(self, number: int, link: Mapping[str, Any]) -> None:
        link_id = link.get("issue_link_id")
        if link_id is None:
            raise ForgeError(f"the link from #{number} to #{link.get('iid')} has no issue_link_id")
        self.w.send("DELETE", f"{self.repo_path}/issues/{number}/links/{int(link_id)}")

    def _post_link(self, number: int, p: Mapping[str, Any], link_type: str) -> Any:
        return self.w.send(
            "POST",
            f"{self.repo_path}/issues/{number}/links",
            {
                "target_project_id": p.get("blocker_project") or self.project,
                "target_issue_iid": p["blocker"],
                "link_type": link_type,
            },
        )

    def unlink(self, p: Mapping[str, Any]) -> str:
        number = p["issue"]
        if "label" in p:
            if p["label"] not in self._labels_of(number):
                raise WriteRefused(f"#{number} no longer has {p['label']}; nothing written")
            self._put(number, {"remove_labels": p["label"]})
            return f"#{number}: removed {p['label']}"
        kinds = ("is_blocked_by", "relates_to") if self.convention else ("is_blocked_by",)
        link = self._find(number, p, kinds)
        if link is None:
            raise WriteRefused(
                f"#{number} isn't linked to {blocker_ref(p, self.project)} any more; nothing "
                "written"
            )
        if link.get("state") != "closed":
            raise WriteRefused(f"{blocker_ref(p, self.project)} is open again; the link stays")
        self._delete_link(number, link)
        return f"#{number}: no longer blocked by {blocker_ref(p, self.project)}"

    def link_add(self, p: Mapping[str, Any]) -> str:
        """A native `is_blocked_by` link on the blocked issue, or under the convention a
        `relates_to` link (kept when it's already there) and the blocked label."""
        number, blocker = p["issue"], blocker_ref(p, self.project)
        if self.convention:
            done = []
            if self._find(number, p, ("relates_to",)) is None:
                self._post_link(number, p, "relates_to")
                done.append("a relates_to link")
            if self.blocked_label not in self._labels_of(number):
                self._put(number, {"add_labels": self.blocked_label})
                done.append(f"the {self.blocked_label} label")
            how = " and ".join(done) or "nothing new"
            return f"#{number}: now blocked by {blocker} ({how}, the configured convention)"
        try:
            made = self._post_link(number, p, "is_blocked_by")
        except WriteFailed as error:
            if error.status == 403 and "licen" in str(error).lower():
                raise WriteUnsupported(self._no_blocks()) from None
            raise
        made_type = made.get("link_type") if isinstance(made, dict) else None
        if made_type not in (None, "is_blocked_by", "blocks"):
            # A GitLab without blocking links makes a relates_to link instead; take it back.
            stray = self._find(number, p, (made_type,))
            if stray is not None:
                self._delete_link(number, stray)
            undone = "; the relates_to link it made instead was removed" if stray else ""
            raise WriteUnsupported(self._no_blocks() + undone)
        return f"#{number}: now blocked by {blocker}"

    def _no_blocks(self) -> str:
        return (
            "this GitLab has no blocking links (a paid-tier feature); `blocking: "
            "relates_to+blocked` in the config records blocking as a relates_to link and the "
            f"{self.blocked_label} label"
        )

    def link_remove(self, p: Mapping[str, Any]) -> str:
        """Delete the link, whatever the blocker's state. Under the convention the blocked label
        comes off with the issue's last blocking link."""
        number, blocker = p["issue"], blocker_ref(p, self.project)
        kinds = ("is_blocked_by", "relates_to") if self.convention else ("is_blocked_by",)
        link = self._find(number, p, kinds)
        if link is None:
            raise WriteRefused(f"#{number} isn't blocked by {blocker}; nothing written")
        self._delete_link(number, link)
        done = f"#{number}: no longer blocked by {blocker}"
        if not self.convention:
            return done
        left = [
            other
            for other in self._links(number)
            if self._counts(other) and other.get("issue_link_id") != link.get("issue_link_id")
        ]
        if not left and self.blocked_label in self._labels_of(number):
            self._put(number, {"remove_labels": self.blocked_label})
            return f"{done}; removed {self.blocked_label}, its last blocker"
        return done

    def delete_branch(self, p: Mapping[str, Any]) -> str:
        name = p["branch"]
        path = f"{self.repo_path}/repository/branches/{_quote(name)}"
        branch = self.r.get(path)
        head = ((branch or {}).get("commit") or {}).get("id") if isinstance(branch, dict) else None
        if head != p["sha"]:
            raise WriteRefused(
                f"{name} moved to {(head or '?')[:7]} since {p['sha'][:7]} was checked; kept"
            )
        if branch.get("protected"):
            raise WriteRefused(f"{name} is protected; kept")
        self.w.send("DELETE", path)
        return f"deleted branch {name} (was {p['sha'][:7]})"

    def label_create(self, p: Mapping[str, Any]) -> str:
        color = p["color"].lower()
        body = {"name": p["name"], "color": f"#{color}"}
        if p.get("description"):
            body["description"] = p["description"]
        self.w.send("POST", f"{self.repo_path}/labels", body)
        return f"created label {p['name']} (#{color})"


# --- running an operation -----------------------------------------------------------------------


@dataclass
class Writer:
    """Runs named operations, through the write-wrapper when one is configured, and logs each."""

    project: str
    repo_dir: str
    log_path: str
    # GitHubWrites or GitLabWrites: one method per operation, named after it.
    forge: Optional[Any]
    wrapper: Optional[str] = None
    run: Callable[..., Any] = subprocess.run
    now: Callable[[], datetime] = datetime.now
    # The names of the repo's labels, read through the read transport. `label-create` checks it
    # before anything is sent, with or without a wrapper, and is refused when it isn't set.
    existing_labels: Optional[Callable[[], Sequence[str]]] = None
    # One issue by number (and project), and the native links blocking one of this project's
    # issues, read through the read transport. `link-add` and `link-remove` check them before
    # anything is sent, with or without a wrapper, and are refused when they aren't set.
    issue_ref: Optional[Callable[[int, Optional[str]], IssueRef]] = None
    blocked_by: Optional[Callable[[int], Sequence[Link]]] = None
    # Why this forge has no writer of its own, said when there's no wrapper.
    unsupported: Optional[str] = None
    # Whether a blocker_project may be a group/sub/project path (GitLab).
    nested_projects: bool = False

    def _new_label(self, name: str) -> None:
        if self.existing_labels is None:
            raise WriteRefused("can't check the repo's existing labels; nothing written")
        clash = [n for n in self.existing_labels() if n.lower() == name.lower()]
        if clash:
            raise WriteRefused(
                f"a label named {clash[0]!r} already exists (names compare without case); "
                "nothing written"
            )

    def _link(self, op: str, p: Mapping[str, Any]) -> None:
        """Refuse a link-remove whose link isn't there, and a link-add whose link already is or
        whose either side is a pull request or closed, before anything is sent."""
        if self.issue_ref is None or self.blocked_by is None:
            raise WriteRefused("can't check the issue's existing links; nothing written")
        number, blocker = p["issue"], blocker_ref(p, self.project)
        other = p.get("blocker_project")
        other = None if other in (None, self.project) else other
        linked = any(
            link.blocker == p["blocker"] and link.blocker_project == other
            for link in self.blocked_by(number)
        )
        if op == "link-remove":
            if not linked:
                raise WriteRefused(f"#{number} isn't blocked by {blocker}; nothing written")
            return
        if linked:
            raise WriteRefused(f"#{number} is already blocked by {blocker}; nothing written")
        sides = (
            (self.issue_ref(number, None), f"#{number}"),
            (self.issue_ref(p["blocker"], other), blocker),
        )
        for ref, name in sides:
            if ref.is_change:
                raise WriteRefused(f"{name} is a pull request; a blocking link joins two issues")
            if ref.state == "closed":
                raise WriteRefused(f"{name} is closed; a link to or from it blocks nothing")

    def apply(self, op: str, payload: Any) -> Result:
        via = "wrapper" if self.wrapper else "forge"
        try:
            checked = validate(op, payload, self.project, self.nested_projects)
            checked.setdefault("repo", self.project)
            if op == "label-create":
                self._new_label(checked["name"])
            if op in LINK_OPS:
                self._link(op, checked)
            if self.wrapper:
                detail = self._wrapped(op, checked)
            else:
                if self.forge is None:
                    raise WriteRefused(self.unsupported or "no forge writer is set up")
                detail = getattr(self.forge, op.replace("-", "_"))(checked)
            result = Result(op, "written", detail, via)
        except WriteUnsupported as error:
            result = Result(op, "unsupported", str(error), via)
        except WriteRefused as error:
            result = Result(op, "refused", str(error), via)
        except ForgeError as error:
            result = Result(op, "failed", str(error), via)
        problem = self._log(result, payload)
        if problem:
            result = Result(
                result.op, result.status, f"{result.detail} (not logged: {problem})", via
            )
        return result

    def _wrapped(self, op: str, payload: Mapping[str, Any]) -> str:
        argv = shlex.split(self.wrapper or "") + [op]
        try:
            done = self.run(
                argv,
                input=json.dumps(payload),
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=WRAPPER_TIMEOUT,
            )
        except FileNotFoundError:
            raise WriteRefused(f"write-wrapper {argv[0]} wasn't found") from None
        except subprocess.TimeoutExpired:
            raise WriteRefused(f"write-wrapper timed out after {WRAPPER_TIMEOUT}s") from None
        if done.returncode != 0:
            reason = redact(done.stderr or "") or f"exit {done.returncode}"
            raise WriteRefused(f"write-wrapper refused: {reason}")
        said = redact((done.stdout or "").strip().splitlines()[0] if done.stdout.strip() else "")
        return f"write-wrapper did it{': ' + said if said else ''}"

    def _log(self, result: Result, payload: Any) -> Optional[str]:
        """Append the outcome to the write log; the reason when that fails, else None."""
        entry = {
            "at": iso(self.now()),
            "op": result.op,
            "status": result.status,
            "via": result.via,
            "detail": result.detail,
            "payload": payload,
        }
        try:
            state.ensure_dir(os.path.dirname(self.log_path))
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as error:
            return error.strerror or str(error)
        return None


def log_path(remote_key: str, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(state.cache_root(environ), "writes", f"{remote_key}.jsonl")


def render(result: Result, label: Optional[str] = None) -> str:
    name = label or result.op
    if result.status == "written":
        return f"{name}: {result.detail}"
    return f"{name}: {result.status.upper()} ({result.detail})"
