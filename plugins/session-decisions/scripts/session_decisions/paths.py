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


def session_pointer_path(env: Mapping[str, str], sid: str) -> Path:
    """Per-session pointer, so two installs (say a marketplace copy and a --plugin-dir copy)
    running at once each keep their own data directory."""
    return state_home(env) / "pointers" / sid


def data_dir(env: Mapping[str, str], override: str | None = None, sid: str | None = None) -> Path:
    for value in (override, env.get("CLAUDE_PLUGIN_DATA")):
        if value:
            return Path(value)
    candidates = [session_pointer_path(env, sid)] if _safe(sid) else []
    for pointer in candidates + [pointer_path(env)]:
        try:
            recorded = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if recorded:
            return Path(recorded)
    return state_home(env)


def _write_if_changed(pointer: Path, value: str) -> None:
    try:
        if pointer.read_text(encoding="utf-8").strip() == value:
            return
    except OSError:
        pass
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(value, encoding="utf-8")


def write_pointer(env: Mapping[str, str], data: Path, sid: str | None = None) -> None:
    if _safe(sid):
        _write_if_changed(session_pointer_path(env, sid), str(data))
    _write_if_changed(pointer_path(env), str(data))
