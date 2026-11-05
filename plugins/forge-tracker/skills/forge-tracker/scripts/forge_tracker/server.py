"""forge-tracker as an MCP server over stdio: the CLI's reports and writes as tools.

The tools call the same functions the CLI does (cli.py); nothing here runs the CLI. The
transport is the subset of MCP a tools-only server needs: newline-delimited JSON-RPC 2.0 on
stdin and stdout, `initialize`, `ping`, `tools/list` and `tools/call`, with notifications
read and ignored. Logs and stray prints go to stderr, so stdout carries only protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Optional, TextIO

from . import catchup, config, detect, groom, labelsetup, workspace
from . import next as next_
from .cli import (
    WRITE_OPS,
    CatchupRun,
    Context,
    apply_write,
    catchup_one,
    catchup_workspace,
    groom_approved,
    groom_report,
    json_default,
    link_payload,
    make_writer,
    next_report,
    open_forge,
    print_then_mark,
    render_smoke,
    render_write,
    save_slate,
    slate_line,
    smoke_checks,
    workspace_at,
)
from .model import ConfigError, ForgeError, attempt

NAME = "forge-tracker"
VERSION = "1"
# Newest first. A client asking for one of these gets it back; any other request gets the
# newest, and the client decides whether it can go on.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# The stateless revision: no initialize, the version and client capabilities in each request's
# `_meta`, and `server/discover` in place of the handshake.
STATELESS_VERSION = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
# The tool list never changes while the server runs.
CACHE_TTL_MS = 3_600_000
SERVER_INFO = {"name": NAME, "title": "forge-tracker", "version": VERSION}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
UNSUPPORTED_VERSION = -32022

INSTRUCTIONS = (
    "forge-tracker reads a repo's forge (GitHub, GitLab, Forgejo or Gitea) and reports on it. "
    "The read tools (catchup, next_report, next_facts, save_slate, groom, labels_suggest, smoke) "
    "change nothing on the forge; catchup moves its local marker unless no_mark is set, and "
    "save_slate and labels_decline write only the local cache. The write tools (groom_apply, "
    "link, write, labels_create) change the forge; call them only for what the user approved. "
    "Every tool takes `repo`, a local checkout (default: the project directory)."
)


class ToolError(Exception):
    """A call that couldn't run: a bad argument or a setup problem (the CLI's exit 2)."""


@dataclass
class Outcome:
    """A tool's answer: its text, its structured result (None for none), and the catchup runs
    whose markers move only once the answer has been written.

    Claude Code shows the model a result's structuredContent in place of its text when it has
    one, so a report comes back as structured JSON only when `json` asks for it; otherwise the
    model reads the rendered report, the same text the CLI prints and the commands describe."""

    text: str
    data: Any = None
    error: bool = False
    runs: list[CatchupRun] = field(default_factory=list)


# --- tool definitions ---------------------------------------------------------------------------

REPO = {
    "type": "string",
    "description": "the git checkout to read (default: the project directory)",
}
FETCH = {
    "type": "boolean",
    "default": True,
    "description": "run `git fetch --quiet origin` in the repo first, as the commands do",
}
JSON = {
    "type": "boolean",
    "default": False,
    "description": "return the report as structured JSON (also as the text) instead of the "
    "rendered report",
}
READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "openWorldHint": True}


def _schema(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "catchup",
        "title": "Catch up",
        "description": (
            "What changed on the forge and what waits on the user since their last catchup "
            "(7 days the first time): the rendered report. Moves the catchup marker once the "
            "report is returned, unless no_mark or since is set or a read failed. Give several "
            "`repos`, or `workspace` (a folder of checkouts), for one report over all of them."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "several checkouts, read as one workspace report",
                },
                "workspace": {
                    "type": "string",
                    "description": "a folder of checkouts (its repos: list, else its child repos)",
                },
                "since": {
                    "type": "string",
                    "description": "look back 3d, 12h, 2w, 90m or to an ISO time; marker untouched",
                },
                "no_mark": {
                    "type": "boolean",
                    "default": False,
                    "description": "read the marker but don't move it (always set it when the "
                    "user didn't ask for the catchup)",
                },
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "list the standing backlog in full, not as counts",
                },
                "fetch": FETCH,
                "json": JSON,
            }
        ),
        "annotations": {"title": "Catch up", "readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "next_report",
        "title": "Next: report",
        "description": (
            "What to work on: the user's queue, agent lanes and what's blocked, with the saved "
            "slate while the facts hold. Ends `slate: needed` when the facts need ranking "
            "(next_facts, then save_slate, then this again)."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "rerank": {
                    "type": "boolean",
                    "default": False,
                    "description": "ask for a new slate even if one holds",
                },
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "re-read the facts even if the fingerprint holds",
                },
                "fetch": FETCH,
                "json": JSON,
            }
        ),
        "annotations": {"title": "Next: report", **READ},
    },
    {
        "name": "next_facts",
        "title": "Next: facts",
        "description": (
            "The facts and classification /next ranks from, as JSON, with each item's body "
            "unless bodies is false."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "re-read the facts even if the fingerprint holds",
                },
                "bodies": {
                    "type": "boolean",
                    "default": True,
                    "description": "include issue bodies",
                },
            }
        ),
        "annotations": {"title": "Next: facts", **READ},
    },
    {
        "name": "save_slate",
        "title": "Next: save slate",
        "description": (
            "Store a slate (queue, lanes, overrules, person_only, note, recommend) against the "
            "cached facts. Writes only the local cache. A refusal names every problem."
        ),
        "inputSchema": _schema(
            {"repo": REPO, "slate": {"type": "object", "description": "the slate"}},
            ("slate",),
        ),
        "annotations": {"title": "Next: save slate", **READ, "openWorldHint": False},
    },
    {
        "name": "groom",
        "title": "Groom: proposals",
        "description": (
            "Where the tracker drifted, with numbered proposals and their ids and evidence, the "
            "unclear bucket and a list for the user. Writes nothing."
        ),
        "inputSchema": _schema({"repo": REPO, "fetch": FETCH, "json": JSON}),
        "annotations": {"title": "Groom: proposals", **READ},
    },
    {
        "name": "labels_suggest",
        "title": "Labels: suggest",
        "description": (
            "Read the repo's labels and propose a .claude/forge-tracker.local.md config, plus "
            "the labels it would create. Writes nothing."
        ),
        "inputSchema": _schema({"repo": REPO, "json": JSON}),
        "annotations": {"title": "Labels: suggest", **READ},
    },
    {
        "name": "smoke",
        "title": "Smoke check",
        "description": "Every read once against the forge, with counts and any FAILED part.",
        "inputSchema": _schema({"repo": REPO, "json": JSON}),
        "annotations": {"title": "Smoke check", **READ},
    },
    {
        "name": "labels_decline",
        "title": "Labels: decline setup",
        "description": (
            "Remember, in the local cache only, that the user declined label setup here, so "
            "the commands stop offering it."
        ),
        "inputSchema": _schema({"repo": REPO}),
        "annotations": {
            "title": "Labels: decline setup",
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "groom_apply",
        "title": "Groom: apply (writes)",
        "description": (
            "Write the groom proposals the user approved, by id, after re-running the checks. "
            "An id that isn't a current proposal refuses the whole list and writes nothing."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "approved": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "proposal ids, as the groom report prints them",
                },
            },
            ("approved",),
        ),
        "annotations": {"title": "Groom: apply", **WRITE, "destructiveHint": True},
    },
    {
        "name": "link",
        "title": "Link: add or remove (writes)",
        "description": (
            "Add or remove a native blocking link: issue waits on blocked_by. Refused, writing "
            "nothing, for a link that already exists (add) or doesn't (remove), a self-link, "
            "or a pull request or closed issue on either side (add)."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "action": {"type": "string", "enum": ["add", "remove"]},
                "issue": {"type": ["string", "integer"], "description": "the blocked issue: 18"},
                "blocked_by": {
                    "type": ["string", "integer"],
                    "description": "the blocker: 17, #17, or owner/repo#17",
                },
            },
            ("action", "issue", "blocked_by"),
        ),
        "annotations": {"title": "Link", **WRITE, "destructiveHint": True},
    },
    {
        "name": "write",
        "title": "Write operation (writes)",
        "description": (
            "Run one named write operation with its payload, through the write-wrapper when "
            "the config sets one."
        ),
        "inputSchema": _schema(
            {
                "repo": REPO,
                "op": {"type": "string", "enum": list(WRITE_OPS)},
                "payload": {"type": "object"},
            },
            ("op", "payload"),
        ),
        "annotations": {"title": "Write operation", **WRITE, "destructiveHint": True},
    },
    {
        "name": "labels_create",
        "title": "Labels: create (writes)",
        "description": (
            "Create the labels the user approved (a default name such as needs-human, or "
            "role=name), and return the config lines that replace their ~."
        ),
        "inputSchema": _schema(
            {"repo": REPO, "names": {"type": "array", "items": {"type": "string"}}},
            ("names",),
        ),
        "annotations": {"title": "Labels: create", **WRITE, "destructiveHint": False},
    },
]

WRITE_TOOLS = ("groom_apply", "link", "write", "labels_create")


# --- the tools ------------------------------------------------------------------------------------


@dataclass
class Env:
    """What the tools run against; tests swap in fakes."""

    http: Any = None
    run: Callable[..., Any] = subprocess.run
    now: Optional[Callable[[], datetime]] = None
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    tz: Optional[tzinfo] = None

    def time(self) -> datetime:
        return self.now() if self.now else datetime.now(timezone.utc)

    def default_repo(self) -> str:
        return self.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    def path(self, value: Any) -> str:
        if value is None or value == "":
            return os.path.abspath(self.default_repo())
        if not isinstance(value, str):
            raise ToolError("repo must be a path")
        return os.path.abspath(os.path.join(self.default_repo(), os.path.expanduser(value)))


def _bool(args: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be true or false")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=json_default) + "\n"


def _plain(value: Any) -> Any:
    """The value as JSON would carry it (datetimes as ISO strings)."""
    return json.loads(json.dumps(value, default=json_default))


def _fetch(env: Env, path: str) -> Optional[str]:
    """`git fetch --quiet origin`; a note when it fails (a workspace folder isn't a repo, so it
    fails there by design), None when it worked."""
    try:
        result = env.run(
            ["git", "-C", path, "fetch", "--quiet", "origin"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"git fetch failed: {error}"
    if result.returncode != 0:
        return f"git fetch failed: {(result.stderr or '').strip() or result.returncode}"
    return None


def _open(env: Env, path: str) -> Context:
    try:
        return open_forge(path, env.http, env.run, environ=env.environ)
    except ConfigError as error:
        ws = None
        try:
            ws = workspace_at(path, env.run, False)
        except ConfigError:
            pass
        if ws is not None and ws.repos:
            names = ", ".join(os.path.relpath(p, path) for p in ws.repos)
            raise ToolError(
                f"{path} isn't a git repo; it's a workspace of {len(ws.repos)} ({names}). "
                "This tool reads one repo: pass repo. Only catchup reads a whole workspace."
            ) from None
        raise ToolError(str(error)) from None
    except ForgeError as error:
        raise ToolError(str(error)) from None


def _report(report: dict[str, Any], text: str, as_json: bool, notes: list[str]) -> Outcome:
    shown = _json(report) if as_json else text
    if notes:
        shown = "".join(f"note: {n}\n" for n in notes) + shown
    return Outcome(shown, _plain(report) if as_json else None)


def tool_catchup(env: Env, args: Mapping[str, Any]) -> Outcome:
    now = env.time()
    since = args.get("since")
    if since is not None:
        if not isinstance(since, str):
            raise ToolError("since must be a string")
        try:
            catchup.parse_since(since, now)
        except ValueError as error:
            raise ToolError(str(error)) from None
    ns = argparse.Namespace(
        since=since or None, no_mark=_bool(args, "no_mark"), all=_bool(args, "all")
    )
    as_json = _bool(args, "json")
    repos = args.get("repos") or []
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ToolError("repos must be a list of paths")
    if args.get("workspace") and (repos or args.get("repo")):
        raise ToolError("pass workspace, or repo or repos, not both")
    paths = list(dict.fromkeys(env.path(r) for r in repos)) or [env.path(args.get("repo"))]
    ws: Optional[workspace.Workspace] = None
    if args.get("workspace"):
        try:
            ws = workspace_at(env.path(args["workspace"]), env.run, True)
        except ConfigError as error:
            raise ToolError(str(error)) from None
    elif len(paths) > 1:
        ws = workspace.chosen(paths)
    notes: list[str] = []
    if ws is None:
        if _bool(args, "fetch", True):
            note = _fetch(env, paths[0])
            if note:
                notes.append(note)
        try:
            ctx = open_forge(paths[0], env.http, env.run, environ=env.environ)
        except ConfigError as error:
            try:
                ws = workspace_at(paths[0], env.run, False)
            except ConfigError as inner:
                raise ToolError(str(inner)) from None
            if ws is None:
                raise ToolError(str(error)) from None
            notes = []  # a fetch in a workspace folder fails by design
        except ForgeError as error:
            raise ToolError(str(error)) from None
        else:
            result = catchup_one(ns, ctx, now, env.environ, env.run)
            outcome = _report(result.report, catchup.render(result.report, env.tz), as_json, notes)
            outcome.runs = [result]
            return outcome
    if not ws.repos:
        raise ToolError(
            f"{ws.root} holds no git repo with an origin remote, and its {config.PATH} lists "
            "none in repos:"
        )
    if _bool(args, "fetch", True):
        for path in ws.repos:
            note = _fetch(env, path)
            if note:
                notes.append(f"{path}: {note}")
    report, runs = catchup_workspace(ns, ws, env.http, now, env.environ, env.run)
    outcome = _report(report, workspace.render(report, env.tz), as_json, notes)
    outcome.runs = runs
    return outcome


def _fetched(env: Env, args: Mapping[str, Any], path: str) -> list[str]:
    if not _bool(args, "fetch", True):
        return []
    note = _fetch(env, path)
    return [note] if note else []


def tool_next_report(env: Env, args: Mapping[str, Any]) -> Outcome:
    path = env.path(args.get("repo"))
    notes = _fetched(env, args, path)
    ctx = _open(env, path)
    now = env.time()
    report = next_report(
        ctx, now, env.environ, False, _bool(args, "rerank"), _bool(args, "refresh")
    )
    return _report(report, next_.render(report, now, env.tz), _bool(args, "json"), notes)


def tool_next_facts(env: Env, args: Mapping[str, Any]) -> Outcome:
    ctx = _open(env, env.path(args.get("repo")))
    report = next_report(
        ctx,
        env.time(),
        env.environ,
        True,
        refresh=_bool(args, "refresh"),
        bodies=_bool(args, "bodies", True),
    )
    return Outcome(json.dumps(report, default=json_default), _plain(report))


def tool_save_slate(env: Env, args: Mapping[str, Any]) -> Outcome:
    slate = args.get("slate")
    if isinstance(slate, str):
        try:
            slate = json.loads(slate)
        except ValueError as error:
            raise ToolError(f"the slate isn't JSON: {error}") from None
    try:
        saved = save_slate(env.path(args.get("repo")), slate, env.run, env.time(), env.environ)
    except ConfigError as error:
        raise ToolError(str(error)) from None
    except next_.SlateError as error:
        raise ToolError(f"slate refused: {error}") from None
    return Outcome(slate_line(saved))


def tool_groom(env: Env, args: Mapping[str, Any]) -> Outcome:
    path = env.path(args.get("repo"))
    notes = _fetched(env, args, path)
    report = groom_report(_open(env, path), env.time())
    return _report(report, groom.render(report), _bool(args, "json"), notes)


def tool_labels_suggest(env: Env, args: Mapping[str, Any]) -> Outcome:
    ctx = _open(env, env.path(args.get("repo")))
    read = attempt(ctx.adapter.labels)
    if not read.ok:
        return Outcome(f"labels: FAILED ({read.error})\n")
    data = labelsetup.report(read.data, ctx.config, ctx.remote, env.time())
    if _bool(args, "json"):
        return Outcome(_json(data), _plain(data))
    return Outcome(labelsetup.render(data))


def tool_labels_decline(env: Env, args: Mapping[str, Any]) -> Outcome:
    path = env.path(args.get("repo"))
    try:
        remote, _, _ = detect.locate(path, config.load(path).forge, env.run)
    except ConfigError as error:
        raise ToolError(str(error)) from None
    where = labelsetup.record_decline(remote, env.environ, env.time())
    return Outcome(
        f"setup declined for {remote.project}: the reports still say what's unset, and the "
        f"commands won't offer setup again (delete {where} to undo)\n",
        {"declined": remote.project, "path": where},
    )


def tool_smoke(env: Env, args: Mapping[str, Any]) -> Outcome:
    ctx = _open(env, env.path(args.get("repo")))
    checks = smoke_checks(ctx, env.time())
    parts = [
        {
            "name": name,
            "ok": read.ok,
            "unsupported": bool(read.unsupported),
            "value": read.data if read.ok else None,
            "error": None if read.ok else read.error,
        }
        for name, read in checks
    ]
    ok = all(p["ok"] or p["unsupported"] for p in parts)
    data = {"ok": ok, "project": ctx.remote.project, "forge": ctx.forge, "checks": parts}
    if _bool(args, "json"):
        return Outcome(_json(data), _plain(data))
    return Outcome(render_smoke(ctx, checks))


def _result(result: Any, line: str) -> dict[str, Any]:
    return {
        "op": result.op,
        "status": result.status,
        "detail": result.detail,
        "via": result.via,
        "line": line,
    }


def tool_groom_apply(env: Env, args: Mapping[str, Any]) -> Outcome:
    approved = args.get("approved")
    if not isinstance(approved, list):
        raise ToolError("approved must be a list of proposal ids")
    ctx = _open(env, env.path(args.get("repo")))
    now = env.time()
    try:
        chosen = groom_approved(ctx, {"approved": approved}, now)
    except groom.ApplyError as error:
        raise ToolError(f"nothing written: {error}") from None
    writer = make_writer(ctx, now, env.environ, env.run)
    lines, results = [], []
    for proposal in chosen:
        result = writer.apply(proposal["op"], proposal["payload"])
        line = render_write(result, proposal["id"])
        lines.append(line)
        results.append({"id": proposal["id"], **_result(result, line)})
    ok = all(r["status"] == "written" for r in results)
    return Outcome("\n".join(lines) + "\n", {"ok": ok, "results": results})


def tool_link(env: Env, args: Mapping[str, Any]) -> Outcome:
    action = args.get("action")
    if action not in ("add", "remove"):
        raise ToolError("action must be add or remove")
    payload = link_payload(str(args.get("issue", "")), str(args.get("blocked_by", "")))
    if payload is None:
        raise ToolError(
            "link takes an issue number (18 or #18) and blocked_by an issue (17, #17 or "
            "owner/repo#17)"
        )
    ctx = _open(env, env.path(args.get("repo")))
    result, line = apply_write(ctx, env.time(), env.environ, env.run, f"link-{action}", payload)
    return Outcome(line + "\n", _result(result, line), error=result.status == "refused")


def tool_write(env: Env, args: Mapping[str, Any]) -> Outcome:
    op = args.get("op")
    if not isinstance(op, str):
        raise ToolError("op must be the name of a write operation")
    ctx = _open(env, env.path(args.get("repo")))
    result, line = apply_write(ctx, env.time(), env.environ, env.run, op, args.get("payload"))
    return Outcome(line + "\n", _result(result, line), error=result.status == "refused")


def tool_labels_create(env: Env, args: Mapping[str, Any]) -> Outcome:
    names = args.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ToolError("names must be a non-empty list of label names")
    ctx = _open(env, env.path(args.get("repo")))
    read = attempt(ctx.adapter.labels)
    if not read.ok:
        return Outcome(
            f"labels: FAILED ({read.error}); nothing created\n",
            {"ok": False, "error": read.error},
            error=True,
        )
    try:
        plan = labelsetup.plan_create(names, read.data, ctx.config)
    except labelsetup.CreateError as error:
        raise ToolError(f"nothing created: {error}") from None
    now = env.time()
    data = labelsetup.create(plan, make_writer(ctx, now, env.environ, env.run).apply)
    return Outcome(labelsetup.render_create(data), _plain(data))


HANDLERS: dict[str, Callable[[Env, Mapping[str, Any]], Outcome]] = {
    "catchup": tool_catchup,
    "next_report": tool_next_report,
    "next_facts": tool_next_facts,
    "save_slate": tool_save_slate,
    "groom": tool_groom,
    "labels_suggest": tool_labels_suggest,
    "smoke": tool_smoke,
    "labels_decline": tool_labels_decline,
    "groom_apply": tool_groom_apply,
    "link": tool_link,
    "write": tool_write,
    "labels_create": tool_labels_create,
}
assert set(HANDLERS) == {t["name"] for t in TOOLS}


# --- the protocol ---------------------------------------------------------------------------------


class Server:
    def __init__(self, env: Optional[Env] = None):
        self.env = env or Env()
        self.protocol: Optional[str] = None

    def handle(self, message: Any) -> tuple[Optional[dict[str, Any]], list[CatchupRun]]:
        """One incoming message: the response to send (None for a notification) and the catchup
        runs whose markers move once it is sent."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message"), []
        method = message.get("method")
        if "id" not in message:
            return None, []  # notifications/initialized, notifications/cancelled, ...
        mid = message["id"]
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None, []  # a response to nothing we sent
            return _error(mid, INVALID_REQUEST, "no method"), []
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object"), []
        meta = params.get("_meta")
        if method == "server/discover" or (isinstance(meta, dict) and META_VERSION in meta):
            return self.stateless(mid, method, params)
        if method == "initialize":
            return _ok(mid, self.initialize(params)), []
        if method == "ping":
            return _ok(mid, {}), []
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS}), []
        if method == "tools/call":
            return self.call(mid, params)
        return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}"), []

    def stateless(
        self, mid: Any, method: str, params: Mapping[str, Any]
    ) -> tuple[Optional[dict[str, Any]], list[CatchupRun]]:
        """A 2026-07-28 request: no handshake; each request names its protocol version in
        `_meta`, and each result says it's complete and who answered."""
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        info = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
        if method == "server/discover":
            return _ok(
                mid,
                {
                    "resultType": "complete",
                    "supportedVersions": [STATELESS_VERSION, *PROTOCOL_VERSIONS],
                    "capabilities": {"tools": {}},
                    "instructions": INSTRUCTIONS,
                    "ttlMs": CACHE_TTL_MS,
                    "cacheScope": "public",
                    "_meta": info,
                },
            ), []
        version = meta.get(META_VERSION)
        if version != STATELESS_VERSION:
            return _error(
                mid,
                UNSUPPORTED_VERSION,
                f"unsupported protocol version: {version}",
                {"supported": [STATELESS_VERSION], "requested": version},
            ), []
        if META_CAPABILITIES not in meta:
            return _error(mid, INVALID_PARAMS, f"_meta lacks {META_CAPABILITIES}"), []
        if method == "tools/list":
            result: dict[str, Any] = {"tools": TOOLS, "ttlMs": CACHE_TTL_MS, "cacheScope": "public"}
            response, runs = _ok(mid, result), []
        elif method == "tools/call":
            response, runs = self.call(mid, params)
        else:
            return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}"), []
        if "result" in response:
            response["result"]["resultType"] = "complete"
            response["result"]["_meta"] = info
        return response, runs

    def initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        self.protocol = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def call(self, mid: Any, params: Mapping[str, Any]) -> tuple[dict[str, Any], list[CatchupRun]]:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(mid, INVALID_PARAMS, f"unknown tool: {name}"), []
        if not isinstance(args, dict):
            return _error(mid, INVALID_PARAMS, "arguments must be an object"), []
        props = next(t for t in TOOLS if t["name"] == name)["inputSchema"]
        unknown = sorted(set(args) - set(props["properties"]))
        missing = [k for k in props.get("required", []) if k not in args]
        if unknown or missing:
            problems = [f"unknown argument {k}" for k in unknown]
            problems += [f"missing argument {k}" for k in missing]
            return _ok(mid, _tool_result(Outcome("; ".join(problems), error=True))), []
        try:
            outcome = handler(self.env, args)
        except ToolError as error:
            outcome = Outcome(f"forge-tracker: {error}\n", error=True)
        except Exception as error:  # noqa: BLE001 - a tool's bug is the call's error, not ours
            outcome = Outcome(f"forge-tracker: {type(error).__name__}: {error}\n", error=True)
        return _ok(mid, _tool_result(outcome)), outcome.runs


def _tool_result(outcome: Outcome) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": outcome.text}]}
    if isinstance(outcome.data, dict):
        result["structuredContent"] = outcome.data
    if outcome.error:
        result["isError"] = True
    return result


def _ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": error}


def serve(stdin: TextIO, stdout: TextIO, env: Optional[Env] = None) -> int:
    """Read messages until stdin closes. Each response is one line on stdout."""
    server = Server(env)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError as error:
            response: Optional[dict[str, Any]] = _error(None, PARSE_ERROR, f"parse error: {error}")
            runs: list[CatchupRun] = []
        else:
            if isinstance(message, list):
                response, runs = _error(None, INVALID_REQUEST, "batches aren't supported"), []
            else:
                response, runs = server.handle(message)
        if response is None:
            continue
        text = json.dumps(response, default=json_default, ensure_ascii=False) + "\n"
        # The markers move only once the report has gone out, as the CLI does after printing.
        print_then_mark(text, runs, stdout)
    return 0


def main() -> int:
    stdout = sys.stdout
    # Anything a library prints goes to stderr; stdout is the protocol's alone.
    sys.stdout = sys.stderr
    return serve(sys.stdin, stdout)
