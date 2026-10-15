"""Step files: one action per line, split with shell quoting, `#` comments and blank lines skipped.

    wait REGEX [TIMEOUT]          until the screen matches (Python `re`, multiline)
    gone REGEX [TIMEOUT]          until it no longer matches
    idle REGEX [QUIET] [TIMEOUT]  until REGEX (a busy marker) has been absent for QUIET seconds;
                                  the cast's clock runs slower meanwhile (--busy-timescale)
    keys KEY...                   tmux key names: Enter, Down, Escape, C-c, ...
    type TEXT [DELAY]             TEXT literally, one character every DELAY seconds (0.03)
    paste FILE                    FILE's contents as one bracketed paste
    click TEXT                    a left click on the first on-screen TEXT
    sleep SECONDS
    snap NAME                     the screen as plain text, to NAME.txt and stdout
    record on|off                 cast frames on or off; on from the start unless a step says
    include FILE                  FILE's steps, in place
    when REGEX STEP...            STEP only when the screen matches REGEX right now

`${NAME}` in an argument is replaced from --var NAME=VALUE. Paths in `include` and `paste` are
relative to the file that names them.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Optional

# action: (fewest arguments, most arguments or None for no limit)
ARITY: dict[str, tuple[int, Optional[int]]] = {
    "wait": (1, 2),
    "gone": (1, 2),
    "idle": (1, 3),
    "keys": (1, None),
    "type": (1, 2),
    "paste": (1, 1),
    "click": (1, 1),
    "sleep": (1, 1),
    "snap": (1, 1),
    "record": (1, 1),
    "include": (1, 1),
    "when": (2, None),
}

# Which argument positions hold numbers, by action.
NUMERIC = {"wait": (1,), "gone": (1,), "idle": (1, 2), "type": (1,), "sleep": (0,)}


class StepError(ValueError):
    """A step file that can't be run; the message names the file and line."""


@dataclass
class Step:
    action: str
    args: list[str]
    where: str  # "file:line"
    base: Path = field(default_factory=Path.cwd)  # the directory relative paths resolve against
    then: Optional[Step] = None  # the step a `when` guards

    def number(self, index: int, default: float) -> float:
        return float(self.args[index]) if len(self.args) > index else default


def parse_line(text: str, where: str, base: Path, variables: dict[str, str]) -> Optional[Step]:
    """One line as a Step, or None for a blank line or a comment."""
    try:
        words = shlex.split(text, comments=True)
    except ValueError as err:
        raise StepError(f"{where}: {err}") from None
    if not words:
        return None
    try:
        words = [Template(word).substitute(variables) for word in words]
    except KeyError as err:
        raise StepError(f"{where}: no --var {err.args[0]}") from None
    except ValueError as err:
        raise StepError(f"{where}: {err}") from None
    return build(words, where, base)


def build(words: list[str], where: str, base: Path) -> Step:
    action, args = words[0], words[1:]
    if action not in ARITY:
        raise StepError(f"{where}: unknown step {action!r}")
    if action == "when":
        _regex(args[0] if args else "", where)
        if len(args) < 2:
            raise StepError(f"{where}: when needs a regex and a step")
        then = build(args[1:], where, base)
        if then.action in ("include", "when"):
            raise StepError(f"{where}: when can't guard {then.action}")
        return Step("when", args[:1], where, base, then)
    fewest, most = ARITY[action]
    if len(args) < fewest or (most is not None and len(args) > most):
        if most is None:
            wanted = f"{fewest} or more"
        else:
            wanted = f"{fewest}" if fewest == most else f"{fewest} to {most}"
        raise StepError(f"{where}: {action} takes {wanted} argument(s), got {len(args)}")
    for index in NUMERIC.get(action, ()):
        if len(args) > index:
            try:
                if float(args[index]) < 0:
                    raise ValueError
            except ValueError:
                raise StepError(f"{where}: {action}: {args[index]!r} isn't a number") from None
    if action in ("wait", "gone", "idle"):
        _regex(args[0], where)
    if action == "record" and args[0] not in ("on", "off"):
        raise StepError(f"{where}: record takes on or off, got {args[0]!r}")
    return Step(action, args, where, base)


def _regex(pattern: str, where: str) -> None:
    try:
        re.compile(pattern, re.MULTILINE)
    except re.error as err:
        raise StepError(f"{where}: bad regex {pattern!r}: {err}") from None


def load(
    path: Path, variables: dict[str, str], seen: Optional[tuple[Path, ...]] = None
) -> list[Step]:
    """Every step in PATH, with includes expanded in place."""
    path = path.resolve()
    seen = seen or ()
    if path in seen:
        raise StepError(f"include loops back to {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as err:
        raise StepError(f"can't read {path}: {err.strerror}") from None
    return parse(lines, str(path), path.parent, variables, (*seen, path))


def parse(
    lines: list[str],
    name: str,
    base: Path,
    variables: dict[str, str],
    seen: tuple[Path, ...] = (),
) -> list[Step]:
    steps: list[Step] = []
    for number, text in enumerate(lines, 1):
        step = parse_line(text, f"{name}:{number}", base, variables)
        if step is None:
            continue
        if step.action == "include":
            steps.extend(load(base / step.args[0], variables, seen))
        else:
            steps.append(step)
    return steps
