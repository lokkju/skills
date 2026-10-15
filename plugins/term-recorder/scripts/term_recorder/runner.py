"""Runs a command in a tmux pane on a private server and drives it with steps.

The server has its own socket and a generated config (no ~/.tmux.conf, no status line), and the
command gets only the environment it's given. Frames are captured whenever the screen changes and
kept with their times for the asciicast.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TextIO

from .steps import Step, parse_size

SESSION = "rec"

# What agg's usual fonts draw badly: Claude Code's result elbow (U+23BF; U+2514 looks the same)
# and no-break spaces.
CAST_FIXES = (("⎿", "└"), (" ", " "))


class StepFailed(RuntimeError):
    """A step that didn't happen: a wait that timed out, text not on screen to click."""


@dataclass
class Options:
    cols: int = 100
    rows: int = 30
    cwd: Path = field(default_factory=Path.cwd)
    env: dict[str, str] = field(default_factory=dict)
    term: str = "screen-256color"
    out: Path = field(default_factory=Path.cwd)
    busy_timescale: float = 8.0
    frame_interval: float = 0.1


class Tmux:
    def __init__(self, socket: str) -> None:
        self.socket = socket

    def __call__(self, *args: str, env: Optional[dict[str, str]] = None) -> str:
        done = subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if done.returncode != 0:
            verb = next((a for a in args if not a.startswith("-") and "/" not in a), args[0])
            raise RuntimeError(f"tmux {verb}: {done.stderr.strip() or done.returncode}")
        return done.stdout


class Recorder:
    def __init__(self, command: list[str], options: Options, log: TextIO) -> None:
        self.command = command
        self.o = options
        self.log = log
        self.socket = f"term-record-{os.getpid()}"
        self.tmux = Tmux(self.socket)
        # (cast time, "o" for a screen or "r" for a resize, the screen or "COLSxROWS")
        self.frames: list[tuple[float, str, str]] = []
        self._clock = 0.0
        self.cols0, self.rows0 = options.cols, options.rows
        self.recording = True
        self.busy = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conf: Optional[str] = None

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        fd, self._conf = tempfile.mkstemp(prefix="term-record-", suffix=".conf")
        with os.fdopen(fd, "w") as conf:
            conf.write(
                f'set -g default-terminal "{self.o.term}"\n'
                "set -g status off\n"
                # Claude Code warns on screen when focus events are off.
                "set -g focus-events on\n"
                "set -g mouse on\n"
                "set -g remain-on-exit on\n"
                "set -g history-limit 0\n"
            )
        # The server, and so the pane, inherits exactly this environment.
        env = dict(self.o.env)
        env.setdefault("TERM", self.o.term)
        self.tmux(
            "-f",
            self._conf,
            "new-session",
            "-d",
            "-s",
            SESSION,
            "-x",
            str(self.o.cols),
            "-y",
            str(self.o.rows),
            "-c",
            str(self.o.cwd),
            "--",
            *self.command,
            env=env,
        )
        # Set at startup instead, window-size manual crashes tmux 3.4's server.
        self.tmux("set", "-g", "window-size", "manual")
        self.tmux("resize-window", "-t", SESSION, "-x", str(self.o.cols), "-y", str(self.o.rows))
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._thread.start()

    def stop(self, keep: bool = False) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        if not keep:
            try:
                self.tmux("kill-server")
            except RuntimeError:
                pass
        if self._conf:
            Path(self._conf).unlink(missing_ok=True)

    # --- the screen ----------------------------------------------------------------------------

    def screen(self, escapes: bool = False) -> str:
        args = ["capture-pane", "-p", "-t", SESSION]
        if escapes:
            args.insert(1, "-e")
        return self.tmux(*args)

    def exited(self) -> Optional[str]:
        """How the command ended, or None while it runs."""
        dead, status = self.tmux(
            "display-message", "-p", "-t", SESSION, "#{pane_dead} #{pane_dead_status}"
        ).split(" ", 1)
        if dead.strip() != "1":
            return None
        return status.strip() or "?"

    def _capture(self) -> None:
        last = None
        prev = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                self._clock += (now - prev) / (self.o.busy_timescale if self.busy else 1.0)
            prev = now
            if self.recording:
                try:
                    frame = self.screen(escapes=True)
                except RuntimeError:
                    frame = None
                if frame is not None and frame != last:
                    with self._lock:
                        self.frames.append((self._clock, "o", frame))
                    last = frame
            self._stop.wait(self.o.frame_interval)

    def cast(self) -> str:
        """The frames as an asciicast v2 file; a resize is an "r" event."""
        header = {"version": 2, "width": self.cols0, "height": self.rows0}
        lines = [json.dumps(header)]
        with self._lock:
            frames = list(self.frames)
        for at, kind, data in frames:
            if kind == "o":
                for bad, good in CAST_FIXES:
                    data = data.replace(bad, good)
                data = "\x1b[H\x1b[2J" + data.rstrip("\n").replace("\n", "\r\n")
            lines.append(json.dumps([round(at, 3), kind, data], ensure_ascii=False))
        return "\n".join(lines) + "\n"

    # --- steps ---------------------------------------------------------------------------------

    def run(self, steps: list[Step]) -> None:
        if any(s.action == "record" or (s.then and s.then.action == "record") for s in steps):
            self.recording = False
        for step in steps:
            self.do(step)

    def do(self, step: Step) -> None:
        handler: Callable[[Step], None] = getattr(self, f"_{step.action}")
        handler(step)

    def _poll(self, step: Step, ok: Callable[[str], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while True:
            screen = self.screen()
            if ok(screen):
                return
            ended = self.exited()
            if ended is not None and not ok(self.screen()):
                raise StepFailed(f"{step.where}: {what}, but the command exited (status {ended})")
            if time.monotonic() > deadline:
                raise StepFailed(f"{step.where}: timed out after {timeout:g}s waiting {what}")
            time.sleep(0.2)

    def _wait(self, step: Step) -> None:
        pattern = re.compile(step.args[0], re.MULTILINE)
        self._poll(
            step,
            lambda s: bool(pattern.search(s)),
            step.number(1, 90),
            f"for /{step.args[0]}/",
        )

    def _gone(self, step: Step) -> None:
        pattern = re.compile(step.args[0], re.MULTILINE)
        self._poll(
            step,
            lambda s: not pattern.search(s),
            step.number(1, 90),
            f"for /{step.args[0]}/ to go",
        )

    def _idle(self, step: Step) -> None:
        pattern = re.compile(step.args[0], re.MULTILINE)
        quiet_for, timeout = step.number(1, 3), step.number(2, 240)
        deadline = time.monotonic() + timeout
        quiet_since: Optional[float] = None
        self.busy = True
        try:
            while True:
                now = time.monotonic()
                if pattern.search(self.screen()):
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= quiet_for:
                    return
                if now > deadline:
                    raise StepFailed(
                        f"{step.where}: still busy (/{step.args[0]}/) after {timeout:g}s"
                    )
                time.sleep(0.25)
        finally:
            self.busy = False

    def _keys(self, step: Step) -> None:
        self.tmux("send-keys", "-t", SESSION, *step.args)

    def _type(self, step: Step) -> None:
        delay = step.number(1, 0.03)
        if delay == 0:
            self.tmux("send-keys", "-t", SESSION, "-l", step.args[0])
            return
        for char in step.args[0]:
            self.tmux("send-keys", "-t", SESSION, "-l", char)
            time.sleep(delay)

    def _paste(self, step: Step) -> None:
        path = step.base / step.args[0]
        if not path.is_file():
            raise StepFailed(f"{step.where}: no file {path}")
        buffer = f"term-record-{os.getpid()}"
        self.tmux("load-buffer", "-b", buffer, str(path))
        self.tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", SESSION)

    def _click(self, step: Step) -> None:
        for row, line in enumerate(self.screen().splitlines(), 1):
            index = line.find(step.args[0])
            if index >= 0:
                # One column into the text, in SGR mouse coordinates (1-based).
                col = index + 2
                self.tmux("send-keys", "-t", SESSION, "-l", f"\x1b[<0;{col};{row}M")
                time.sleep(0.05)
                self.tmux("send-keys", "-t", SESSION, "-l", f"\x1b[<0;{col};{row}m")
                return
        raise StepFailed(f"{step.where}: no {step.args[0]!r} on screen")

    def _sleep(self, step: Step) -> None:
        time.sleep(step.number(0, 0))

    def _snap(self, step: Step) -> None:
        self.snap(step.args[0])

    def snap(self, name: str) -> Path:
        screen = self.screen()
        path = self.o.out / f"{name}.txt"
        path.write_text(screen, encoding="utf-8")
        self.log.write(f"--- {name} ({path}) ---\n{screen.rstrip()}\n")
        self.log.flush()
        return path

    def _resize(self, step: Step) -> None:
        size = parse_size(step.args[0])
        assert size is not None
        self.o.cols, self.o.rows = size
        self.tmux("resize-window", "-t", SESSION, "-x", str(size[0]), "-y", str(size[1]))
        with self._lock:
            self.frames.append((self._clock, "r", f"{size[0]}x{size[1]}"))

    def _record(self, step: Step) -> None:
        self.recording = step.args[0] == "on"

    def _when(self, step: Step) -> None:
        assert step.then is not None
        if re.search(step.args[0], self.screen(), re.MULTILINE):
            self.do(step.then)
