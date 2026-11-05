"""forge-tracker as an MCP server over stdio: the CLI's reports and writes as tools.

The tools call the same functions the CLI does (cli.py); nothing here runs the CLI. The
protocol is mcp_stdio.py's, shared with term-recorder. Logs and stray prints go to stderr, so
stdout carries only protocol.
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

from . import catchup, config, detect, groom, labelsetup, mcp_stdio, workspace
from . import next as next_
from .cli import (
    WRITE_OPS,
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
from .mcp_stdio import Outcome, Spec, ToolError, boolean, schema
from .model import ConfigError, ForgeError, attempt

NAME = "forge-tracker"
VERSION = "1"

INSTRUCTIONS = (
    "forge-tracker reads a repo's forge (GitHub, GitLab, Forgejo or Gitea) and reports on it. "
    "The read tools (catchup, next_report, next_facts, save_slate, groom, labels_suggest, smoke) "
    "change nothing on the forge; catchup moves its local marker unless no_mark is set, and "
    "save_slate and labels_decline write only the local cache. The write tools (groom_apply, "
    "link, write, labels_create) change the forge; call them only for what the user approved. "
    "Every tool takes `repo`, a local checkout (default: the project directory)."
)


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


_schema = schema


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


_bool = boolean


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
            outcome.pending = [result]
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
    outcome.pending = list(runs)
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


# --- the server ----------------------------------------------------------------------------------

SPEC = Spec(NAME, VERSION, "forge-tracker", INSTRUCTIONS, TOOLS, HANDLERS)


def serve(stdin: TextIO, stdout: TextIO, env: Optional[Env] = None) -> int:
    """Read messages until stdin closes. Each response is one line on stdout; a catchup's
    markers move only once its report has gone out, as the CLI does after printing."""
    return mcp_stdio.serve(SPEC, stdin, stdout, env or Env(), print_then_mark, json_default)


def main() -> int:
    stdout = sys.stdout
    # Anything a library prints goes to stderr; stdout is the protocol's alone.
    sys.stdout = sys.stderr
    return serve(sys.stdin, stdout)
