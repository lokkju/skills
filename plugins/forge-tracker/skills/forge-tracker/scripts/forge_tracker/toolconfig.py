"""What the forge CLIs' own config files say, read without running them: the hosts `glab` is set
up for. Tokens are never read here.

glab keeps `config.yml` in `$GLAB_CONFIG_DIR`, else `$XDG_CONFIG_HOME/glab-cli`, else
`~/.config/glab-cli`. Its `hosts:` mapping names each host it's set up for, with an optional
`api_host` when the API lives elsewhere (a subpath, another name). The standard library has no
YAML parser, so this reads only the keys of that mapping, by indentation.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from typing import Optional


def _config_home(environ: Mapping[str, str]) -> str:
    xdg = environ.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return xdg
    return os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".config")


def glab_config_path(environ: Mapping[str, str] = os.environ) -> str:
    directory = environ.get("GLAB_CONFIG_DIR") or os.path.join(_config_home(environ), "glab-cli")
    return os.path.join(directory, "config.yml")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _host_of(value: str) -> str:
    value = _unquote(value)
    if "://" in value:
        value = urllib.parse.urlsplit(value).hostname or ""
    return value.split("/", 1)[0].split(":", 1)[0].lower()


def _key_value(text: str) -> tuple[Optional[str], str]:
    """A `key: value` line's key and value, the key possibly quoted (`"host:8443": ...`)."""
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        if end < 0 or not text[end + 1 :].startswith(":"):
            return None, ""
        return text[1:end], text[end + 2 :]
    key, colon, value = text.partition(":")
    return (key, value) if colon else (None, "")


def glab_hosts(environ: Mapping[str, str] = os.environ) -> set[str]:
    """Every host glab is set up for: the keys under `hosts:`, their `api_host`, and
    `GITLAB_HOST` / `GL_HOST` from the environment."""
    hosts = {_host_of(environ[k]) for k in ("GITLAB_HOST", "GL_HOST") if environ.get(k)}
    text = _read(glab_config_path(environ))
    if text is None:
        return {h for h in hosts if h}
    in_hosts = False
    host_indent: Optional[int] = None
    for line in text.splitlines():
        stripped = line.split(" #", 1)[0].rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip())
        if indent == 0:
            in_hosts = stripped == "hosts:"
            host_indent = None
            continue
        if not in_hosts:
            continue
        key, value = _key_value(stripped.strip())
        if key is None:
            continue
        if host_indent is None:
            host_indent = indent
        if indent == host_indent:
            hosts.add(_host_of(key))
        elif key == "api_host" and value.strip():
            hosts.add(_host_of(value))
    return {h for h in hosts if h}
