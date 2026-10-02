"""Command line for the hooks, the statusline segment and the fallback ledger."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import IO, Mapping

from . import hooks, paths, store
from .subject import LETTER, Draft, problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decisions", description="session-decisions queue")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook-pretooluse")
    sub.add_parser("hook-sessionstart")
    sub.add_parser("status").add_argument("--data")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session")
    common.add_argument("--data")
    listing = sub.add_parser("list", parents=[common])
    listing.add_argument("--all", action="store_true")
    add = sub.add_parser("add", parents=[common])
    add.add_argument("--kind", required=True, type=str.upper, choices=sorted(LETTER))
    add.add_argument("--text", required=True)
    add.add_argument("--description", default="")
    close = sub.add_parser("close", parents=[common])
    close.add_argument("label")
    close.add_argument("--ruling", required=True)
    return parser


def _read_json(stdin: IO[str]) -> dict | None:
    try:
        data = json.loads(stdin.read() or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _age(created: str | None, now: datetime) -> str:
    if not created:
        return ""
    try:
        then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return f"waiting {minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"waiting {hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"waiting {days}d {hours}h"


def _hook(cmd: str, env: Mapping[str, str], stdin: IO[str], stdout: IO[str]) -> int:
    try:
        payload = _read_json(stdin)
        if payload is not None:
            handler = hooks.pre_tool_use if cmd == "hook-pretooluse" else hooks.session_start
            result = handler(payload, env)
            if result is not None:
                stdout.write(json.dumps(result))
    except Exception:  # a broken hook must never block the session
        pass
    return 0


def _status(data_override: str | None, env: Mapping[str, str], stdin: IO[str], stdout: IO[str]) -> int:
    try:
        payload = _read_json(stdin) or {}
        sid = paths.session_id({}, payload.get("session_id"))
        if sid is not None:
            state = store.SessionState(paths.data_dir(env, data_override), sid).load()
            count = sum(1 for item in store.all_items(paths.tasks_dir(env, sid), state) if item.is_open)
            if count:
                stdout.write(f"{count} open")
    except Exception:  # the statusline must never show an error
        pass
    return 0


def main(argv: list[str], env: Mapping[str, str], stdin: IO[str], stdout: IO[str], stderr: IO[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.cmd in ("hook-pretooluse", "hook-sessionstart"):
        return _hook(args.cmd, env, stdin, stdout)
    if args.cmd == "status":
        return _status(args.data, env, stdin, stdout)

    sid = paths.session_id(env, args.session)
    if sid is None:
        stderr.write("decisions: can't tell which session this is; pass --session <session id>\n")
        return 2
    state_file = store.SessionState(paths.data_dir(env, args.data, sid), sid)
    tasks = paths.tasks_dir(env, sid)

    if args.cmd == "list":
        items = store.all_items(tasks, state_file.load())
        shown = items if args.all else [item for item in items if item.is_open]
        if not shown:
            stdout.write("No open decisions or actions.\n")
            return 0
        now = datetime.now(timezone.utc)
        for item in shown:
            tags = ["ledger"] if item.source == "ledger" else []
            tags.append(_age(item.created, now) if item.is_open else item.status)
            tags = [tag for tag in tags if tag]
            stdout.write(item.subject + (f"  ({', '.join(tags)})" if tags else "") + "\n")
            if item.ruling:
                stdout.write(f"    {item.ruling}\n")
        return 0

    if args.cmd == "add":
        draft = Draft(args.kind, args.text.strip())
        found = problems(draft)
        if found:
            stderr.write("decisions: " + "; ".join(found) + "\n")
            return 2
        with state_file.locked() as state:
            item = store.ledger_add(state, draft, args.description, store.all_items(tasks, state))
        stdout.write(item.subject + "\n")
        return 0

    label = args.label.upper()
    with state_file.locked() as state:
        closed = store.ledger_close(state, label, args.ruling.strip())
    if not closed:
        stderr.write(f"decisions: {label} isn't in the fallback ledger. If it's a task-list item, "
                     "mark it completed with TaskUpdate and add the ruling to its description.\n")
        return 1
    stdout.write(f"{label} closed\n")
    return 0
