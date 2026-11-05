"""One recording from start to finish: what the CLI and the MCP server both run.

A Job says what to run and how; execute() starts the tmux server, runs the steps, snaps the
final screen, writes the cast and GIF, and returns a Result instead of printing, so each front
end words it its own way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO

from . import steps as steplib
from .runner import Options, Recorder, StepFailed

# What the command gets from this environment unless --pass adds more. TERM comes from --term.
BASE_ENV = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")

AGG_ARGS = ["--idle-time-limit", "2", "--last-frame-duration", "4", "--font-size", "14"]

# The bundled example step files, as ${EXAMPLES} in any step file.
EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples"


def environment(
    passed: list[str], extra: Mapping[str, str], source: Mapping[str, str] = os.environ
) -> dict[str, str]:
    """BASE_ENV and PASSED from SOURCE, then EXTRA on top."""
    env = {name: source[name] for name in BASE_ENV if name in source}
    env.update({name: source[name] for name in passed if name in source})
    env.update(extra)
    return env


def variables(given: Mapping[str, str]) -> dict[str, str]:
    """Step-file substitutions: ${EXAMPLES}, then the caller's (which win)."""
    return {"EXAMPLES": str(EXAMPLES), **given}


@dataclass
class Job:
    command: list[str]
    steps: list[steplib.Step]
    cols: int = 100
    rows: int = 30
    cwd: Path = field(default_factory=Path.cwd)
    out: Optional[Path] = None  # None: a new temporary directory
    cast: Optional[Path] = None
    gif: Optional[Path] = None
    agg_args: Optional[list[str]] = None  # None: AGG_ARGS
    env: dict[str, str] = field(default_factory=dict)
    term: str = "screen-256color"
    busy_timescale: float = 8.0
    keep: bool = False


@dataclass
class Snap:
    name: str
    path: Path
    screen: str


@dataclass
class Result:
    """How it went. status: 0 every step ran, 1 a step failed (or agg did), 2 a setup problem.
    messages are the notes the CLI prints to stderr, in order."""

    status: int
    out: Optional[Path] = None
    snaps: list[Snap] = field(default_factory=list)
    failure: Optional[str] = None  # the step that failed, or the setup problem
    screen: Optional[str] = None  # the screen when a step failed
    messages: list[str] = field(default_factory=list)
    cast: Optional[Path] = None
    gif: Optional[Path] = None
    socket: Optional[str] = None  # the tmux server, when keep left it running


def preflight(job: Job) -> Optional[str]:
    """Why the job can't start, or None."""
    if not job.command:
        return "no command; put it after --"
    if shutil.which("tmux") is None:
        return "tmux isn't on PATH"
    if job.gif and shutil.which("agg") is None:
        return "--gif needs agg (https://github.com/asciinema/agg) on PATH"
    return None


def execute(job: Job, log: TextIO) -> Result:
    """Run JOB. Each snap is also written to LOG as the CLI prints it."""
    problem = preflight(job)
    if problem:
        return Result(2, failure=problem, messages=[f"term-record: {problem}"])
    out = job.out or Path(tempfile.mkdtemp(prefix="term-record-"))
    out.mkdir(parents=True, exist_ok=True)
    options = Options(
        cols=job.cols,
        rows=job.rows,
        cwd=job.cwd.resolve(),
        env=job.env,
        term=job.term,
        out=out,
        busy_timescale=job.busy_timescale,
    )
    rec = Recorder(job.command, options, log)
    result = Result(0, out=out)
    try:
        rec.start()
        rec.run(job.steps)
    except StepFailed as err:
        result.status, result.failure = 1, str(err)
        result.messages.append(f"term-record: {err}")
        try:
            result.screen = rec.screen().rstrip()
            result.messages.append(result.screen)
        except RuntimeError:
            pass
    except RuntimeError as err:
        result.status, result.failure = 2, str(err)
        result.messages.append(f"term-record: {err}")
    except KeyboardInterrupt:
        result.status = 130
    finally:
        try:
            if result.status != 2:
                rec.snap("final")
        except RuntimeError:
            pass
        rec.stop(keep=job.keep)
    result.snaps = [Snap(name, path, screen) for name, path, screen in rec.snaps]

    if job.keep and result.status != 2:
        result.socket = rec.socket
        result.messages.append(
            f"term-record: still running; attach with: tmux -L {rec.socket} attach"
        )
    cast_text = rec.cast()
    if job.cast:
        job.cast.write_text(cast_text, encoding="utf-8")
        result.cast = job.cast
    if job.gif:
        cast_path = job.cast or out / "recording.cast"
        if not job.cast:
            cast_path.write_text(cast_text, encoding="utf-8")
        result.cast = cast_path
        agg = ["agg", *(job.agg_args if job.agg_args is not None else AGG_ARGS)]
        # agg's own output stays off stdout, which the MCP server keeps for protocol.
        done = subprocess.run(
            [*agg, str(cast_path), str(job.gif)],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()[-1:]
            note = f"agg failed ({done.returncode})" + (f": {detail[0]}" if detail else "")
            result.messages.append(f"term-record: {note}")
            result.status = result.status or 1
            result.failure = result.failure or note
        else:
            result.gif = job.gif
    result.messages.append(f"term-record: snaps in {out}")
    return result
