"""Where the App's credentials go: repo Actions variables and secrets, Pulumi config, or a file."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Optional

from .manifest import FlowError

# argv and the value to feed on stdin; secrets travel on stdin, never in argv.
Command = tuple[list[str], Optional[str]]


def github_commands(
    client_id: str, pem: str, repos: Sequence[str], client_id_var: str, key_secret: str
) -> list[Command]:
    commands: list[Command] = []
    for repo in repos:
        commands.append(
            (["gh", "variable", "set", client_id_var, "-R", repo, "--body", client_id], None)
        )
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


def pulumi_commands(values: Sequence[tuple[str, str]], stack: str, cwd: str) -> list[Command]:
    return [
        (["pulumi", "config", "set", "--secret", "--stack", stack, "--cwd", cwd, key], value)
        for key, value in values
    ]


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
            raise FlowError(
                f"`{' '.join(argv)}` failed (exit {result.returncode})"
                + (f": {detail}" if detail else "")
            )


def write_key_file(path: str, pem: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FlowError(f"{path} already exists; not overwriting it") from None
    with os.fdopen(fd, "w") as handle:
        handle.write(pem)
    os.chmod(path, 0o600)


def _sops(
    run: Callable[..., Any],
    argv: list[str],
    stdin: Optional[str],
    cwd: str,
    secrets: Sequence[str],
) -> str:
    """Run sops with the path's directory as cwd; stdout comes back in memory, never in an error."""
    try:
        result = run(argv, input=stdin, capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError:
        raise FlowError("sops isn't on PATH") from None
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        for secret in secrets:
            if secret.strip():
                detail = detail.replace(secret.strip(), "[redacted]")
        raise FlowError(
            f"`{' '.join(argv[:3])} ...` failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout or ""


def _decrypt(run: Callable[..., Any], path: str, cwd: str, secrets: Sequence[str]) -> Any:
    out = _sops(run, ["sops", "--decrypt", "--output-type", "json", path], None, cwd, secrets)
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


def sops_store_values(
    path: str,
    values: Mapping[str, str],
    replace: bool = False,
    secret_name: Optional[str] = None,
    namespace: Optional[str] = None,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Put each value under its key in a SOPS-encrypted Kubernetes Secret file, then check they
    read back.

    The plaintext only travels on pipes: an existing file is edited with `sops set --value-stdin`
    on a copy of its (encrypted) bytes, and a new one is encrypted from stdin. The result replaces
    `path` by rename, so a failed run leaves the original alone."""
    if not values:
        raise FlowError("nothing to store")
    secrets = list(values.values())
    # sops runs with the file's directory as cwd (so it finds the right .sops.yaml); a relative
    # path like sub/x.sops.yaml would then point at sub/sub/x.sops.yaml.
    path = os.path.abspath(path)
    cwd = os.path.dirname(path)
    if os.path.exists(path):
        doc = _decrypt(run, path, cwd, secrets)
        field = "data" if "data" in doc and "stringData" not in doc else "stringData"
        taken = [key for key in values if key in (doc.get(field) or {})]
        if taken and not replace:
            raise FlowError(
                f"{path} already has {', '.join(repr(k) for k in taken)} under {field}; "
                "pass --replace to overwrite"
            )
        fd, tmp = tempfile.mkstemp(dir=cwd, prefix=".github-app-create-")
        try:
            with os.fdopen(fd, "wb") as handle, open(path, "rb") as source:
                handle.write(source.read())
            for key, value in values.items():
                _sops(
                    run,
                    [
                        "sops",
                        "set",
                        "--value-stdin",
                        "--input-type",
                        "yaml",
                        "--output-type",
                        "yaml",
                        tmp,
                        "".join(f"[{json.dumps(part)}]" for part in (field, key)),
                    ],
                    json.dumps(_encode(field, value)),
                    cwd,
                    secrets,
                )
            with open(tmp) as handle:
                encrypted = handle.read()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        if not secret_name or not namespace:
            raise FlowError(
                f"{path} doesn't exist; --secret-name and --namespace are needed to create it"
            )
        field = "stringData"
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "type": "Opaque",
            field: dict(values),
        }
        encrypted = _sops(
            run,
            [
                "sops",
                "--encrypt",
                "--filename-override",
                path,
                "--input-type",
                "json",
                "--output-type",
                "yaml",
                "/dev/stdin",
            ],
            json.dumps(secret),
            cwd,
            secrets,
        )
        if not encrypted.strip():
            raise FlowError("sops produced no output")
    _atomic_write(path, encrypted)
    stored = _decrypt(run, path, cwd, secrets).get(field) or {}
    wrong = [key for key, value in values.items() if stored.get(key) != _encode(field, value)]
    if wrong:
        raise FlowError(
            f"{path} doesn't read back with the new {', '.join(repr(k) for k in wrong)} "
            "entry; check it before relying on it"
        )


def sops_store_key(
    path: str,
    key: str,
    pem: str,
    replace: bool = False,
    secret_name: Optional[str] = None,
    namespace: Optional[str] = None,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Put `pem` under `key` in a SOPS-encrypted Kubernetes Secret file (see sops_store_values)."""
    sops_store_values(
        path, {key: pem}, replace=replace, secret_name=secret_name, namespace=namespace, run=run
    )
