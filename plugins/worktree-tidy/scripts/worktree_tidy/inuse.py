"""Whether something else is using a worktree: a process whose working directory is inside it,
or a Claude Code session or agent whose transcript for it changed recently. Read-only.

The process table and the agent transcripts are read once per scan (`Activity.gather`); each
worktree is then a lookup.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_SESSION_HOURS = 24.0
PROC = "/proc"


@dataclass
class Process:
    pid: int
    command: str
    cwd: str


def claude_projects() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def encode(path: str) -> str:
    """The directory name Claude Code keeps a project's transcripts under."""
    return path.replace("/", "-").replace(".", "-")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _from_proc(root: str, skip: set[int]) -> Optional[list[Process]]:
    try:
        names = os.listdir(root)
    except OSError:
        return None
    pids = [int(n) for n in names if n.isdigit()]
    if not pids:
        return None  # not a procfs (a macOS /proc doesn't exist; an empty one tells nothing)
    found = []
    for pid in pids:
        if pid in skip:
            continue
        try:
            cwd = os.readlink(os.path.join(root, str(pid), "cwd"))
        except OSError:
            continue  # another user's process, or one that just exited
        if cwd.endswith(" (deleted)"):
            continue
        command = _read(os.path.join(root, str(pid), "comm")) or "?"
        found.append(Process(pid, command, cwd))
    return found


def _from_lsof(skip: set[int], lsof: str, timeout: float) -> Optional[list[Process]]:
    try:
        done = subprocess.run(
            [lsof, "-n", "-P", "-w", "-d", "cwd", "-Fpcn"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not done.stdout.strip():
        return None
    found, pid, command = [], None, "?"
    for line in done.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, command = int(value), "?"
        elif tag == "c":
            command = value
        elif tag == "n" and pid is not None and pid not in skip:
            found.append(Process(pid, command, value))
    return found


def processes(
    proc_root: str = PROC,
    skip: Optional[set[int]] = None,
    lsof: str = "lsof",
    timeout: float = 5.0,
) -> tuple[Optional[list[Process]], Optional[str]]:
    """Every readable process's working directory, from procfs or else lsof. The list is None,
    with a note saying so, when neither works."""
    skip = skip if skip is not None else {os.getpid()}
    found = _from_proc(proc_root, skip)
    if found is None:
        found = _from_lsof(skip, lsof, timeout)
    if found is None:
        return None, f"couldn't look for processes using worktrees (no {proc_root}, lsof failed)"
    return found, None


def _agents(projects: str, since: float) -> dict[str, list[tuple[str, float]]]:
    """Agent worktrees from subagent transcripts changed since `since`: their recorded worktree
    path (real path) to (agent id, mtime). Subagent transcripts sit under the parent session's
    project, not the worktree's, with the worktree path in a .meta.json beside them."""
    found: dict[str, list[tuple[str, float]]] = {}
    try:
        projects_list = os.listdir(projects)
    except OSError:
        return found
    for project in projects_list:
        base = os.path.join(projects, project)
        try:
            sessions = os.listdir(base)
        except OSError:
            continue
        for session in sessions:
            sub = os.path.join(base, session, "subagents")
            try:
                names = os.listdir(sub)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    mtime = os.stat(os.path.join(sub, name)).st_mtime
                except OSError:
                    continue
                if mtime < since:
                    continue
                stem = name[: -len(".jsonl")]
                try:
                    meta = json.loads(_read(os.path.join(sub, stem + ".meta.json")) or "{}")
                except ValueError:
                    continue
                path = meta.get("worktreePath") if isinstance(meta, dict) else None
                if isinstance(path, str) and path:
                    found.setdefault(os.path.realpath(path), []).append((stem, mtime))
    return found


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


@dataclass
class Activity:
    procs: Optional[list[Process]]
    projects: str
    window: float  # seconds
    now: float
    agents: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    note: Optional[str] = None  # why the process check couldn't run

    @classmethod
    def gather(
        cls,
        proc_root: str = PROC,
        projects: Optional[str] = None,
        session_hours: float = DEFAULT_SESSION_HOURS,
        skip: Optional[set[int]] = None,
        lsof: str = "lsof",
        now: Optional[float] = None,
        timeout: float = 5.0,
    ) -> Activity:
        now = time.time() if now is None else now
        projects = projects or claude_projects()
        window = session_hours * 3600
        procs, note = processes(proc_root, skip, lsof, timeout)
        return cls(procs, projects, window, now, _agents(projects, now - window), note)

    def _sessions(self, path: str) -> list[tuple[str, float]]:
        found = []
        for variant in {path, os.path.realpath(path)}:
            base = os.path.join(self.projects, encode(variant))
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    mtime = os.stat(os.path.join(base, name)).st_mtime
                except OSError:
                    continue
                if self.now - mtime <= self.window:
                    found.append((name[: -len(".jsonl")], mtime))
        return found

    def evidence(self, path: str) -> Optional[str]:
        """What is using the worktree at `path`, or None when nothing seems to be."""
        real = os.path.realpath(path)
        parts = []
        if self.procs:
            hits = sorted(
                {(p.pid, p.command) for p in self.procs if _inside(p.cwd, real)},
            )
            if hits:
                shown = ", ".join(f"pid {pid} ({cmd})" for pid, cmd in hits[:3])
                more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
                parts.append(f"{shown}{more} running in it")
        sessions = sorted(set(self._sessions(path)), key=lambda s: -s[1])
        if sessions:
            sid, mtime = sessions[0]
            more = f" (+{len(sessions) - 1} more)" if len(sessions) > 1 else ""
            parts.append(f"session {sid} active {ago(self.now - mtime)}{more}")
        agents = sorted(self.agents.get(real, []), key=lambda s: -s[1])
        if agents:
            aid, mtime = agents[0]
            parts.append(f"subagent {aid} active {ago(self.now - mtime)}")
        return "; ".join(parts) or None
