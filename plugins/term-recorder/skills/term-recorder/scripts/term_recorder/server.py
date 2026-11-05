"""term-recorder as an MCP server over stdio: a recording, its snaps and a step check as tools.

The tools call the same functions the CLI does (job.execute, steps.load and steps.parse);
nothing here runs the CLI. The protocol is mcp_stdio.py's, an identical copy of forge-tracker's.
Logs and stray prints go to stderr, so stdout carries only protocol.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TextIO

from . import mcp_stdio
from . import steps as steplib
from .job import AGG_ARGS, BASE_ENV, Job, Result, environment, execute, variables
from .mcp_stdio import Outcome, Spec, ToolError, schema

NAME = "term-recorder"
VERSION = "1"

INSTRUCTIONS = (
    "term-recorder runs a terminal program on a private tmux server, drives it with steps and "
    "returns what the screen shows as plain text. `record` runs one: the command as an argv "
    "list, the steps inline (`steps`, one per line) or from a file (`steps_file`), and snaps "
    "come back as text. ${EXAMPLES} in steps is the bundled examples directory: `include "
    "${EXAMPLES}/claude-start.steps` gets a fresh Claude Code past its trust dialog. "
    "`check_steps` parses steps without running anything; `snaps` lists or reads the snaps of "
    "an earlier run. Relative paths resolve against the project directory."
)

STEPS_HELP = (
    "wait REGEX [TIMEOUT] | gone REGEX [TIMEOUT] | idle REGEX [QUIET] [TIMEOUT] | keys KEY... | "
    "type TEXT [DELAY] | paste FILE | click TEXT | sleep SECONDS | snap NAME | "
    "resize COLSxROWS | record on|off | include FILE | when REGEX STEP..."
)

STEPS = {
    "type": "string",
    "description": f"steps, one per line, shell quoting, # comments: {STEPS_HELP}",
}
STEPS_FILE = {
    "type": "string",
    "description": "a step file; its steps run before any inline `steps`",
}
VARS = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": "replace ${NAME} in steps (${EXAMPLES} is predefined)",
}
OUT = {"type": "string", "description": "the directory the snaps went to"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "record",
        "title": "Record a terminal program",
        "description": (
            "Run COMMAND in a terminal of a fixed size on a private tmux server with a minimal "
            "environment, drive it with steps, and return each snap's screen as text (the "
            "screen is always snapped as `final` at the end). A failed step (a wait that timed "
            "out, text to click not on screen, the command exiting) is an error result with the "
            "screen at that moment. Paths to the snaps, cast and GIF come back too."
        ),
        "inputSchema": schema(
            {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "the program and its arguments, as an argv list",
                },
                "steps": STEPS,
                "steps_file": STEPS_FILE,
                "vars": VARS,
                "cwd": {
                    "type": "string",
                    "description": "where the command starts, and what inline steps' include "
                    "and paste paths are relative to (default: the project directory)",
                },
                "size": {
                    "type": "string",
                    "default": "100x30",
                    "description": "the starting COLSxROWS; a resize step changes it mid-run",
                },
                "out": {
                    "type": "string",
                    "description": "where snaps go (default: a new temporary directory)",
                },
                "cast": {"type": "string", "description": "write an asciicast v2 file here"},
                "gif": {"type": "string", "description": "render a GIF here with agg"},
                "agg_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"replace agg's arguments (default: {' '.join(AGG_ARGS)})",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "variables to set for the command",
                },
                "pass": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "variables to pass through from the server's environment "
                    f"(always: {', '.join(BASE_ENV)})",
                },
                "term": {
                    "type": "string",
                    "default": "screen-256color",
                    "description": "TERM inside the pane",
                },
                "busy_timescale": {
                    "type": "number",
                    "default": 8,
                    "description": "during idle steps the cast's clock runs this many times slower",
                },
                "show": {
                    "type": "string",
                    "enum": ["all", "final", "none"],
                    "default": "all",
                    "description": "which snaps' screens to return as text (the files are "
                    "written either way)",
                },
            },
            ("command",),
        ),
        "annotations": {
            "title": "Record a terminal program",
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "check_steps",
        "title": "Check steps",
        "description": (
            "Parse steps (inline, from a file, or both) without running anything: each step "
            "with its file and line, includes expanded, or what's wrong and where."
        ),
        "inputSchema": schema(
            {
                "steps": STEPS,
                "steps_file": STEPS_FILE,
                "vars": VARS,
                "cwd": {
                    "type": "string",
                    "description": "what inline steps' include paths are relative to",
                },
            }
        ),
        "annotations": {
            "title": "Check steps",
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "snaps",
        "title": "List or read snaps",
        "description": (
            "The snaps an earlier record wrote to OUT: their names, or with `name` that "
            "snap's screen."
        ),
        "inputSchema": schema(
            {"out": OUT, "name": {"type": "string", "description": "the snap to read"}},
            ("out",),
        ),
        "annotations": {
            "title": "List or read snaps",
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
]


@dataclass
class Env:
    """What the tools run against; tests swap in their own."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def base(self) -> Path:
        return Path(self.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())

    def path(self, value: Any, key: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ToolError(f"{key} must be a path")
        return (self.base() / os.path.expanduser(value)).resolve()


def _strings(args: Mapping[str, Any], key: str) -> list[str]:
    value = args.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{key} must be a list of strings")
    return value


def _mapping(args: Mapping[str, Any], key: str) -> dict[str, str]:
    value = args.get(key) or {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()
    ):
        raise ToolError(f"{key} must be an object of strings")
    return dict(value)


def _optional_path(env: Env, args: Mapping[str, Any], key: str) -> Optional[Path]:
    return env.path(args[key], key) if args.get(key) is not None else None


def _steps(env: Env, args: Mapping[str, Any], cwd: Path) -> list[steplib.Step]:
    subs = variables(_mapping(args, "vars"))
    inline = args.get("steps")
    if inline is not None and not isinstance(inline, str):
        raise ToolError("steps must be a string, one step per line")
    found: list[steplib.Step] = []
    try:
        if args.get("steps_file") is not None:
            found.extend(steplib.load(env.path(args["steps_file"], "steps_file"), subs))
        if inline:
            found.extend(steplib.parse(inline.splitlines(), "<steps>", cwd, subs))
    except steplib.StepError as err:
        raise ToolError(str(err)) from None
    return found


def _cwd(env: Env, args: Mapping[str, Any]) -> Path:
    return _optional_path(env, args, "cwd") or env.base().resolve()


def tool_check_steps(env: Env, args: Mapping[str, Any]) -> Outcome:
    if args.get("steps") is None and args.get("steps_file") is None:
        raise ToolError("give steps, steps_file or both")
    found = _steps(env, args, _cwd(env, args))
    lines = [f"{len(found)} step(s) OK"]
    for step in found:
        shown = [step.action, *step.args]
        if step.then:
            shown += [step.then.action, *step.then.args]
        lines.append(f"{step.where}: {' '.join(shown)}")
    return Outcome("\n".join(lines) + "\n")


def tool_record(env: Env, args: Mapping[str, Any]) -> Outcome:
    command = _strings(args, "command")
    if not command:
        raise ToolError("command must be a non-empty argv list")
    size_text = args.get("size", "100x30")
    size = steplib.parse_size(size_text) if isinstance(size_text, str) else None
    if size is None:
        raise ToolError(f"size wants COLSxROWS, at least 10x3, got {size_text!r}")
    show = args.get("show", "all")
    if show not in ("all", "final", "none"):
        raise ToolError("show must be all, final or none")
    term = args.get("term", "screen-256color")
    if not isinstance(term, str) or not term:
        raise ToolError("term must be a string")
    timescale = args.get("busy_timescale", 8.0)
    if isinstance(timescale, bool) or not isinstance(timescale, (int, float)) or timescale <= 0:
        raise ToolError("busy_timescale must be a positive number")
    cwd = _cwd(env, args)
    job = Job(
        command=command,
        steps=_steps(env, args, cwd),
        cols=size[0],
        rows=size[1],
        cwd=cwd,
        out=_optional_path(env, args, "out"),
        cast=_optional_path(env, args, "cast"),
        gif=_optional_path(env, args, "gif"),
        agg_args=_strings(args, "agg_args") if args.get("agg_args") is not None else None,
        env=environment(_strings(args, "pass"), _mapping(args, "env"), env.environ),
        term=term,
        busy_timescale=float(timescale),
    )
    result = execute(job, io.StringIO())
    return Outcome(render(result, show), error=result.status != 0)


def render(result: Result, show: str) -> str:
    """The snaps' screens as the CLI prints them, then what went wrong and where things are."""
    parts = []
    for snap in result.snaps:
        if show == "all" or (show == "final" and snap.name == "final"):
            parts.append(f"--- {snap.name} ({snap.path}) ---\n{snap.screen.rstrip()}\n")
    if result.failure:
        parts.append(f"term-record: {result.failure}\n")
        if result.screen is not None and not any(s.name == "final" for s in result.snaps):
            parts.append(f"{result.screen}\n")
    if result.out:
        names = ", ".join(s.name for s in result.snaps) or "none"
        parts.append(f"snaps in {result.out}: {names}\n")
    if result.cast:
        parts.append(f"cast: {result.cast}\n")
    if result.gif:
        parts.append(f"gif: {result.gif}\n")
    return "".join(parts)


def tool_snaps(env: Env, args: Mapping[str, Any]) -> Outcome:
    out = env.path(args.get("out"), "out")
    if not out.is_dir():
        raise ToolError(f"no directory {out}")
    name = args.get("name")
    if name is None:
        files = sorted(out.glob("*.txt"), key=lambda p: (p.stat().st_mtime, p.name))
        if not files:
            return Outcome(f"no snaps in {out}\n")
        return Outcome(f"snaps in {out}: {', '.join(p.stem for p in files)}\n")
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        raise ToolError("name must be a snap's name, as record printed it")
    path = out / f"{name}.txt"
    if not path.is_file():
        raise ToolError(f"no snap {name!r} in {out}")
    return Outcome(f"--- {name} ({path}) ---\n{path.read_text(encoding='utf-8').rstrip()}\n")


HANDLERS: dict[str, mcp_stdio.Handler] = {
    "record": tool_record,
    "check_steps": tool_check_steps,
    "snaps": tool_snaps,
}
assert set(HANDLERS) == {t["name"] for t in TOOLS}

# The tools that run a program of the caller's choosing; OpenCode's entry asks before them
# (SCRIPT_TOOLS in harness/opencode/lib.js).
ASK_TOOLS = ("record",)

SPEC = Spec(NAME, VERSION, "term-recorder", INSTRUCTIONS, TOOLS, HANDLERS)


def serve(stdin: TextIO, stdout: TextIO, env: Optional[Env] = None) -> int:
    """Read messages until stdin closes. Each response is one line on stdout."""
    return mcp_stdio.serve(SPEC, stdin, stdout, env or Env())


def main() -> int:
    stdout = sys.stdout
    # Anything a library prints goes to stderr; stdout is the protocol's alone.
    sys.stdout = sys.stderr
    return serve(sys.stdin, stdout)
