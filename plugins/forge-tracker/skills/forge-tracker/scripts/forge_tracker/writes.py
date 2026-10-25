"""The write layer: a short list of named operations, and the only module that can write.

Each operation takes a JSON payload and prints what it did. When the config sets
`write_wrapper:`, the operation runs the wrapper instead of calling the forge: `<wrapper> <op>`
with the same JSON on stdin, in the repo root. The wrapper does the write itself and exits 0, or
exits non-zero to refuse; forge-tracker never sends a write of its own while a wrapper is set.

The write transport lives here and nowhere else, so no adapter or report can reach it. It sends
only POST, PATCH and DELETE, only under the project's own REST path, and never to GraphQL.
Every operation, written, refused or failed, is appended to a 0600 log in the local cache.

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
from .transport import USER_AGENT, Http, ReadTransport, redact

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
WRAPPER_TIMEOUT = 120
# GitHub's limits on a label.
LABEL_NAME_MAX, LABEL_DESCRIPTION_MAX = 50, 100


class WriteRefused(Exception):
    """A write that wasn't sent: a bad payload, a precondition that no longer holds, or a
    write-wrapper's refusal. The message says why."""


@dataclass(frozen=True)
class Result:
    op: str
    status: str  # written | refused | failed
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


def validate(op: str, payload: Any, project: str) -> dict[str, Any]:
    """The payload checked for `op`, or WriteRefused naming every problem. A payload may name
    its `repo` (owner/repo); one naming another project is refused."""
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
        if other is not None and (not isinstance(other, str) or not _PROJECT.fullmatch(other)):
            errors.append("blocker_project must be owner/repo")
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
    """POST, PATCH and DELETE under one project's REST path. Nothing else is sent."""

    api_base: str
    repo_path: str
    http: Http
    token: Optional[str] = field(default=None, repr=False)
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def send(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        method = method.upper()
        if method not in WRITE_METHODS:
            raise ForgeError(f"refused: the write transport doesn't send {method}")
        if not path.startswith(self.repo_path + "/"):
            raise ForgeError(f"refused: {path} is outside {self.repo_path}")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        headers.update(self.extra_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
            message = parsed.get("message") if isinstance(parsed, dict) else None
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
            raise WriteRefused(f"{project}#{p['blocker']} is open again; the link stays")
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


# --- running an operation -----------------------------------------------------------------------


@dataclass
class Writer:
    """Runs named operations, through the write-wrapper when one is configured, and logs each."""

    project: str
    repo_dir: str
    log_path: str
    forge: Optional[GitHubWrites]
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
            checked = validate(op, payload, self.project)
            checked.setdefault("repo", self.project)
            if op == "label-create":
                self._new_label(checked["name"])
            if op in LINK_OPS:
                self._link(op, checked)
            if self.wrapper:
                detail = self._wrapped(op, checked)
            else:
                if self.forge is None:
                    raise WriteRefused("no forge writer is set up")
                detail = getattr(self.forge, op.replace("-", "_"))(checked)
            result = Result(op, "written", detail, via)
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
