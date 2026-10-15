"""Which forge a repo is on, from its origin remote and the config file."""

from __future__ import annotations

import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .model import ConfigError

FORGES = ("github", "gitlab", "forgejo")
# Forges whose adapter is built. The others are recognised and named in the error.
SUPPORTED = ("github",)

_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


@dataclass(frozen=True)
class Remote:
    host: str
    project: str  # owner/repo, or group/sub/project on GitLab

    @property
    def key(self) -> str:
        """`<host>_<project>` with `/` as `__`, the form cache file names use."""
        return f"{self.host}_{self.project.replace('/', '__')}"


def parse_remote(url: str) -> Remote:
    """https://host/owner/repo(.git), ssh://git@host[:port]/owner/repo, or git@host:owner/repo."""
    url = url.strip()
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
    else:
        match = _SCP.match(url)
        if not match:
            raise ConfigError(f"can't read a host and project from the origin URL {url!r}")
        host, path = match.group("host"), match.group("path")
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or path.count("/") < 1 or any(not part for part in path.split("/")):
        raise ConfigError(f"can't read a host and project from the origin URL {url!r}")
    return Remote(host=host.lower(), project=path)


def origin_url(repo_dir: str, run: Callable[..., Any] = subprocess.run) -> str:
    try:
        result = run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        raise ConfigError("git isn't on PATH") from None
    if result.returncode != 0:
        raise ConfigError(f"{repo_dir} has no origin remote (or isn't a git repo)")
    return result.stdout.strip()


def detect_forge(remote: Remote, configured: Optional[str]) -> str:
    """`forge:` from the config when set, else the host. Never a guess.

    The glab host check and the unauthenticated /api/v1/version probe join this when the GitLab
    and Forgejo adapters do; until then an unknown host needs `forge:` in the config file.
    """
    if configured:
        forge = configured
    elif remote.host == "github.com":
        forge = "github"
    elif remote.host == "gitlab.com":
        forge = "gitlab"
    else:
        raise ConfigError(
            f"can't tell which forge {remote.host} runs; set forge: (github, gitlab or forgejo) "
            "in .claude/forge-tracker.local.md"
        )
    if forge not in SUPPORTED:
        raise ConfigError(
            f"{remote.host} is {forge}, and forge-tracker reads only GitHub so far "
            "(GitLab and Forgejo adapters are later phases)"
        )
    return forge
