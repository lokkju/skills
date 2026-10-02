"""Where things live: the session ID, the task-list directory, and the plugin's data directory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe(value: str | None) -> str | None:
    if value and _SAFE_ID.match(value) and value not in (".", ".."):
        return value
    return None


def session_id(env: Mapping[str, str], override: str | None = None) -> str | None:
    """The first usable session ID: the explicit one, then the environment."""
    for value in (override, env.get("CLAUDE_SESSION_ID"), env.get("CLAUDE_CODE_SESSION_ID")):
        safe = _safe(value)
        if safe:
            return safe
    return None


def config_dir(env: Mapping[str, str]) -> Path:
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def tasks_dir(env: Mapping[str, str], sid: str) -> Path:
    """The task list Claude Code keeps for this session (read only; the format is undocumented)."""
    list_id = _safe(env.get("CLAUDE_CODE_TASK_LIST_ID")) or sid
    return config_dir(env) / "tasks" / list_id


def state_home(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "session-decisions"


def pointer_path(env: Mapping[str, str]) -> Path:
    """A file the SessionStart hook writes so scripts run without CLAUDE_PLUGIN_DATA find the data."""
    return state_home(env) / "data-dir"


def data_dir(env: Mapping[str, str], override: str | None = None) -> Path:
    for value in (override, env.get("CLAUDE_PLUGIN_DATA")):
        if value:
            return Path(value)
    try:
        recorded = pointer_path(env).read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    return Path(recorded) if recorded else state_home(env)


def write_pointer(env: Mapping[str, str], data: Path) -> None:
    pointer = pointer_path(env)
    try:
        if pointer.read_text(encoding="utf-8").strip() == str(data):
            return
    except OSError:
        pass
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(data), encoding="utf-8")
