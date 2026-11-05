"""The term-record command line."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Optional

from . import steps as steplib
from .job import AGG_ARGS, BASE_ENV, Job, environment, execute, variables

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
  resize COLSxROWS              a new terminal size mid-run, to snap several widths
  record on|off                 cast frames on or off (on unless a step says)
  include FILE                  FILE's steps, in place
  when REGEX STEP...            STEP only if the screen matches now

${EXAMPLES} in a step file is the bundled examples directory. The screen is always snapped as "final" at the end. Exit status: 0 when every step ran, 1 when a
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
    p.add_argument(
        "--size",
        default="100x30",
        metavar="COLSxROWS",
        help="the starting size (100x30); a resize step changes it mid-run",
    )
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
    size = steplib.parse_size(args.size)
    if size is None:
        print(
            f"term-record: --size wants COLSxROWS, at least 10x3, got {args.size}", file=sys.stderr
        )
        return 2
    try:
        subs = variables(pairs(args.var, "--var"))
        env = environment(args.pass_env, pairs(args.env, "--env"))
    except SystemExit as err:
        print(err, file=sys.stderr)
        return 2

    try:
        steps: list[steplib.Step] = []
        for source in args.steps:
            if source == "-":
                lines = sys.stdin.read().splitlines()
                steps.extend(steplib.parse(lines, "<stdin>", Path.cwd(), subs))
            else:
                steps.extend(steplib.load(Path(source), subs))
    except steplib.StepError as err:
        print(f"term-record: {err}", file=sys.stderr)
        return 2

    job = Job(
        command=command,
        steps=steps,
        cols=size[0],
        rows=size[1],
        cwd=args.cwd or Path.cwd(),
        out=args.out,
        cast=args.cast,
        gif=args.gif,
        agg_args=args.agg_arg,
        env=env,
        term=args.term,
        busy_timescale=args.busy_timescale,
        keep=args.keep,
    )
    log = io.StringIO() if args.quiet else sys.stdout
    result = execute(job, log)
    for message in result.messages:
        print(message, file=sys.stderr)
    return result.status
