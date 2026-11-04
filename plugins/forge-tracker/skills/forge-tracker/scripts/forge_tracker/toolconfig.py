"""What the forge CLIs' own config files say, read without running them: the hosts `glab` is set
up for, and `tea`'s logins. glab's tokens are never read here (glab makes GitLab's requests
itself); a tea login's token is, because Forgejo's requests are forge-tracker's own, and it goes
only into their Authorization header.

glab keeps `config.yml` in `$GLAB_CONFIG_DIR`, else `$XDG_CONFIG_HOME/glab-cli`, else
`~/.config/glab-cli`. Its `hosts:` mapping names each host it's set up for, with an optional
`api_host` when the API lives elsewhere (a subpath, another name). The standard library has no
YAML parser, so this reads only the keys of that mapping, by indentation.

tea keeps `config.yml` in `$XDG_CONFIG_HOME/tea`, else `~/.config/tea`: a `logins:` list whose
items have `name`, `url` (the instance, possibly under a subpath), `token` and `user`.
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


def tea_config_path(environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(_config_home(environ), "tea", "config.yml")


def tea_logins(environ: Mapping[str, str] = os.environ) -> list[dict[str, str]]:
    """Each login in tea's config as its scalar fields (`name`, `url`, `token`, `user`, ...)."""
    text = _read(tea_config_path(environ))
    if text is None:
        return []
    logins: list[dict[str, str]] = []
    in_logins = False
    item_indent: Optional[int] = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # A list may sit at the key's own indent (`logins:` then `- name: ...` at column 0).
        if indent == 0 and not (in_logins and line.startswith("- ")):
            in_logins = line.rstrip() == "logins:"
            continue
        if not in_logins:
            continue
        body = line.strip()
        if body.startswith("- "):
            item_indent = indent
            logins.append({})
            body = body[2:].strip()
        elif item_indent is None or indent <= item_indent or not logins:
            continue
        key, value = _key_value(body)
        if key is not None and value.strip():
            logins[-1][key.strip()] = _unquote(value)
    return [login for login in logins if login.get("url")]


def tea_hosts(environ: Mapping[str, str] = os.environ) -> set[str]:
    return {_host_of(login["url"]) for login in tea_logins(environ)} - {""}


def tea_login(
    host: str, api_base: Optional[str] = None, environ: Mapping[str, str] = os.environ
) -> Optional[dict[str, str]]:
    """The tea login for `host`: when several share the host, the one whose URL the API base
    starts with (an instance under a subpath), else the first."""
    matches = [login for login in tea_logins(environ) if _host_of(login["url"]) == host]
    if api_base:
        for login in matches:
            if api_base.startswith(login["url"].rstrip("/") + "/"):
                return login
    return matches[0] if matches else None
