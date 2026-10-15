"""The term-record command line."""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import steps as steplib
from .runner import Options, Recorder, StepFailed

# What the command gets from this environment unless --pass adds more. TERM comes from --term.
BASE_ENV = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")

AGG_ARGS = ["--idle-time-limit", "2", "--last-frame-duration", "4", "--font-size", "14"]

EPILOG = """\
Steps (one per line, shell quoting, # comments):
  wait REGEX [TIMEOUT]          until the screen matches (Python re, multiline; 90s)
  gone REGEX [TIMEOUT]          until it no longer matches
  idle REGEX [QUIET] [TIMEOUT]  until REGEX has been absent QUIET seconds (3; 240s)
  keys KEY...                   tmux key names: Enter, Down, Escape, C-c
  type TEXT [DELAY]             TEXT literally, a character every DELAY seconds (0.03)
  paste FILE                    FILE as one bracketed paste
  click TEXT                    a left click on the first on-screen TEXT
  sleep SECONDS
  snap NAME                     the screen as text, to OUT/NAME.txt and stdout
  record on|off                 cast frames on or off (on unless a step says)
  include FILE                  FILE's steps, in place
  when REGEX STEP...            STEP only if the screen matches now

The screen is always snapped as "final" at the end. Exit status: 0 when every step ran, 1 when a
step failed (the screen goes to stderr), 2 for a usage or setup problem.
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="term-record",
        description=(
            "Run COMMAND in a terminal of a fixed size on a private tmux server, with a minimal "
            "environment, drive it with steps, and capture what it shows."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", nargs=argparse.REMAINDER, help="the program to run, after --")
    p.add_argument(
        "-s",
        "--steps",
        action="append",
        default=[],
        metavar="FILE",
        help="a step file; repeat to run several in order; - reads stdin",
    )
    p.add_argument("--size", default="100x30", metavar="COLSxROWS", help="default 100x30")
    p.add_argument("-C", "--cwd", type=Path, default=None, help="where COMMAND starts")
    p.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="where snaps go (default: a new temporary directory)",
    )
    p.add_argument("--cast", type=Path, default=None, help="write an asciicast v2 file")
    p.add_argument("--gif", type=Path, default=None, help="render a GIF with agg")
    p.add_argument(
        "--agg-arg",
        action="append",
        default=None,
        metavar="ARG",
        help=f"replace agg's arguments (default: {' '.join(AGG_ARGS)})",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a variable for COMMAND",
    )
    p.add_argument(
        "--pass",
        dest="pass_env",
        action="append",
        default=[],
        metavar="NAME",
        help=f"pass NAME through from this environment (always: {', '.join(BASE_ENV)})",
    )
    p.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="replace ${NAME} in step files",
    )
    p.add_argument("--term", default="screen-256color", help="TERM inside the pane")
    p.add_argument(
        "--busy-timescale",
        type=float,
        default=8.0,
        metavar="N",
        help="during idle steps the cast's clock runs N times slower (8)",
    )
    p.add_argument(
        "--keep", action="store_true", help="leave the tmux server running and print how to attach"
    )
    p.add_argument("-q", "--quiet", action="store_true", help="don't print snaps to stdout")
    return p


def pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for value in values:
        name, sep, rest = value.partition("=")
        if not sep or not name:
            raise SystemExit(f"term-record: {flag} wants NAME=VALUE, got {value!r}")
        out[name] = rest
    return out


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("term-record: no command; put it after --", file=sys.stderr)
        return 2
    size = re.fullmatch(r"(\d+)x(\d+)", args.size)
    if not size or int(size[1]) < 10 or int(size[2]) < 3:
        print(
            f"term-record: --size wants COLSxROWS, at least 10x3, got {args.size}", file=sys.stderr
        )
        return 2
    if shutil.which("tmux") is None:
        print("term-record: tmux isn't on PATH", file=sys.stderr)
        return 2
    if args.gif and shutil.which("agg") is None:
        print(
            "term-record: --gif needs agg (https://github.com/asciinema/agg) on PATH",
            file=sys.stderr,
        )
        return 2
    try:
        variables = pairs(args.var, "--var")
        env = {name: os.environ[name] for name in BASE_ENV if name in os.environ}
        env.update({name: os.environ[name] for name in args.pass_env if name in os.environ})
        env.update(pairs(args.env, "--env"))
    except SystemExit as err:
        print(err, file=sys.stderr)
        return 2

    try:
        steps: list[steplib.Step] = []
        for source in args.steps:
            if source == "-":
                lines = sys.stdin.read().splitlines()
                steps.extend(steplib.parse(lines, "<stdin>", Path.cwd(), variables))
            else:
                steps.extend(steplib.load(Path(source), variables))
    except steplib.StepError as err:
        print(f"term-record: {err}", file=sys.stderr)
        return 2

    out = args.out or Path(tempfile.mkdtemp(prefix="term-record-"))
    out.mkdir(parents=True, exist_ok=True)
    options = Options(
        cols=int(size[1]),
        rows=int(size[2]),
        cwd=(args.cwd or Path.cwd()).resolve(),
        env=env,
        term=args.term,
        out=out,
        busy_timescale=args.busy_timescale,
    )
    log = io.StringIO() if args.quiet else sys.stdout
    rec = Recorder(command, options, log)
    status = 0
    try:
        rec.start()
        rec.run(steps)
    except StepFailed as err:
        print(f"term-record: {err}", file=sys.stderr)
        try:
            print(rec.screen().rstrip(), file=sys.stderr)
        except RuntimeError:
            pass
        status = 1
    except RuntimeError as err:
        print(f"term-record: {err}", file=sys.stderr)
        status = 2
    except KeyboardInterrupt:
        status = 130
    finally:
        try:
            if status != 2:
                rec.snap("final")
        except RuntimeError:
            pass
        rec.stop(keep=args.keep)

    if args.keep and status != 2:
        print(
            f"term-record: still running; attach with: tmux -L {rec.socket} attach", file=sys.stderr
        )
    cast_text = rec.cast()
    if args.cast:
        args.cast.write_text(cast_text, encoding="utf-8")
    if args.gif:
        cast_path = args.cast or out / "recording.cast"
        if not args.cast:
            cast_path.write_text(cast_text, encoding="utf-8")
        agg = ["agg", *(args.agg_arg if args.agg_arg is not None else AGG_ARGS)]
        done = subprocess.run([*agg, str(cast_path), str(args.gif)], check=False)
        if done.returncode != 0:
            print(f"term-record: agg failed ({done.returncode})", file=sys.stderr)
            status = status or 1
    print(f"term-record: snaps in {out}", file=sys.stderr)
    return status
