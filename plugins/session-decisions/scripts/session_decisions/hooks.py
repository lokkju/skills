"""PreToolUse (TaskCreate) and SessionStart hook logic.

Each function returns the hook's JSON output as a dict, or None for no output.
The CLI wrapper catches every exception, so a failure here never blocks the session.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Mapping

from . import paths, store
from .subject import format_subject, parse_draft, problems


def pre_tool_use(payload: dict, env: Mapping[str, str]) -> dict | None:
    if payload.get("tool_name") != "TaskCreate":
        return None
    tool_input = payload.get("tool_input") or {}
    draft = parse_draft(str(tool_input.get("subject", "")))
    if draft is None:
        return None
    found = problems(draft)
    if found:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "session-decisions: " + "; ".join(found),
        }}
    sid = paths.session_id(env, payload.get("session_id"))
    if sid is None:
        return None
    state_file = store.SessionState(paths.data_dir(env), sid)
    with state_file.locked() as state:
        label = store.assign_label(state, draft.kind, store.all_items(paths.tasks_dir(env, sid), state))
    updated = dict(tool_input)
    updated["subject"] = format_subject(label[0], int(label[1:]), draft)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": updated,
    }}


def install_shim(data: Path, env: Mapping[str, str]) -> None:
    """Write <data>/bin/decisions-status, a stable path a statusline script can call.

    The plugin's own directory changes with every version; the data directory doesn't.
    """
    root = env.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return
    script = Path(root) / "scripts" / "decisions.py"
    body = (
        "#!/bin/sh\n"
        "# Written by the session-decisions SessionStart hook for the installed plugin version.\n"
        f"exec python3 {shlex.quote(str(script))} status --data {shlex.quote(str(data))} 2>/dev/null\n"
    )
    bin_dir = data / "bin"
    target = bin_dir / "decisions-status"
    try:
        if target.read_text(encoding="utf-8") == body:
            return
    except OSError:
        pass
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = bin_dir / ".decisions-status.tmp"
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, target)


def session_start(payload: dict, env: Mapping[str, str]) -> dict | None:
    sid = paths.session_id(env, payload.get("session_id"))
    data = paths.data_dir(env, sid=sid)
    try:
        install_shim(data, env)
        if env.get("CLAUDE_PLUGIN_DATA"):
            paths.write_pointer(env, data, sid)
    except OSError:
        pass
    if payload.get("source") not in ("compact", "resume") or sid is None:
        return None
    state = store.SessionState(data, sid).load()
    open_items = [item for item in store.all_items(paths.tasks_dir(env, sid), state) if item.is_open]
    if not open_items:
        return None
    lines = ["Open items in this session's decision queue, still waiting on the user:"]
    for item in open_items:
        where = " (fallback ledger)" if item.source == "ledger" else ""
        lines.append(f"- {item.subject}{where}")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}
