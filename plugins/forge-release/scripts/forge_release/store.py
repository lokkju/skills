"""Where the App's credentials go: repo Actions variables and secrets, Pulumi config, or a file."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .manifest import FlowError

# argv and the value to feed on stdin; secrets travel on stdin, never in argv.
Command = Tuple[List[str], Optional[str]]


def github_commands(client_id: str, pem: str, repos: Sequence[str], client_id_var: str,
                    key_secret: str) -> List[Command]:
    commands: List[Command] = []
    for repo in repos:
        commands.append((["gh", "variable", "set", client_id_var, "-R", repo, "--body", client_id], None))
        commands.append((["gh", "secret", "set", key_secret, "-R", repo], pem))
    return commands


def pulumi_key(name: str) -> str:
    """YEET_APP_PRIVATE_KEY -> yeetAppPrivateKey. A name with a namespace (`ns:key`) is kept."""
    if ":" in name:
        return name
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    if not words:
        raise FlowError(f"can't make a Pulumi config key from {name!r}")
    return words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])


def pulumi_commands(values: Sequence[Tuple[str, str]], stack: str, cwd: str) -> List[Command]:
    return [(["pulumi", "config", "set", "--secret", "--stack", stack, "--cwd", cwd, key], value)
            for key, value in values]


def run_commands(commands: Sequence[Command], run: Callable[..., Any] = subprocess.run) -> None:
    for argv, stdin in commands:
        try:
            result = run(argv, input=stdin, capture_output=True, text=True)
        except FileNotFoundError:
            raise FlowError(f"{argv[0]} isn't on PATH") from None
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if stdin and detail:
                detail = detail.replace(stdin.strip(), "[redacted]")
            raise FlowError(f"`{' '.join(argv)}` failed (exit {result.returncode})"
                            + (f": {detail}" if detail else ""))


def write_key_file(path: str, pem: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FlowError(f"{path} already exists; not overwriting it") from None
    with os.fdopen(fd, "w") as handle:
        handle.write(pem)
    os.chmod(path, 0o600)
