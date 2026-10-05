"""Where the App's credentials go: repo Actions variables and secrets, Pulumi config, or a file."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
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


def _sops(run: Callable[..., Any], argv: List[str], stdin: Optional[str], cwd: str, secret: str) -> str:
    """Run sops with the path's directory as cwd; stdout comes back in memory, never in an error."""
    try:
        result = run(argv, input=stdin, capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError:
        raise FlowError("sops isn't on PATH") from None
    if result.returncode != 0:
        detail = (result.stderr or "").strip().replace(secret.strip(), "[redacted]")
        raise FlowError(f"`{' '.join(argv[:3])} ...` failed (exit {result.returncode})"
                        + (f": {detail}" if detail else ""))
    return result.stdout or ""


def _decrypt(run: Callable[..., Any], path: str, cwd: str, secret: str) -> Any:
    out = _sops(run, ["sops", "--decrypt", "--output-type", "json", path], None, cwd, secret)
    try:
        return json.loads(out)
    except ValueError:
        raise FlowError(f"couldn't read the decrypted {path}") from None


def _encode(field: str, pem: str) -> str:
    return base64.b64encode(pem.encode()).decode() if field == "data" else pem


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".github-app-create-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sops_store_key(path: str, key: str, pem: str, replace: bool = False,
                   secret_name: Optional[str] = None, namespace: Optional[str] = None,
                   run: Callable[..., Any] = subprocess.run) -> None:
    """Put `pem` under `key` in a SOPS-encrypted Kubernetes Secret file, then check it reads back.

    The plaintext only travels on pipes: an existing file is edited with `sops set --value-stdin`
    on a copy of its (encrypted) bytes, and a new one is encrypted from stdin. The result replaces
    `path` by rename, so a failed run leaves the original alone."""
    cwd = os.path.dirname(os.path.abspath(path))
    if os.path.exists(path):
        doc = _decrypt(run, path, cwd, pem)
        field = "data" if "data" in doc and "stringData" not in doc else "stringData"
        if key in (doc.get(field) or {}) and not replace:
            raise FlowError(f"{path} already has {key!r} under {field}; pass --replace to overwrite it")
        fd, tmp = tempfile.mkstemp(dir=cwd, prefix=".github-app-create-")
        try:
            with os.fdopen(fd, "wb") as handle, open(path, "rb") as source:
                handle.write(source.read())
            _sops(run, ["sops", "set", "--value-stdin", "--input-type", "yaml", "--output-type", "yaml",
                        tmp, "".join(f"[{json.dumps(part)}]" for part in (field, key))],
                  json.dumps(_encode(field, pem)), cwd, pem)
            with open(tmp) as handle:
                encrypted = handle.read()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        if not secret_name or not namespace:
            raise FlowError(f"{path} doesn't exist; --secret-name and --namespace are needed to create it")
        field = "stringData"
        secret = {"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": secret_name, "namespace": namespace},
                  "type": "Opaque", field: {key: pem}}
        encrypted = _sops(run, ["sops", "--encrypt", "--filename-override", path, "--input-type", "json",
                                "--output-type", "yaml", "/dev/stdin"], json.dumps(secret), cwd, pem)
        if not encrypted.strip():
            raise FlowError("sops produced no output")
    _atomic_write(path, encrypted)
    stored = (_decrypt(run, path, cwd, pem).get(field) or {}).get(key)
    if stored != _encode(field, pem):
        raise FlowError(f"{path} doesn't read back with the new {key!r} entry; check it before relying on it")
