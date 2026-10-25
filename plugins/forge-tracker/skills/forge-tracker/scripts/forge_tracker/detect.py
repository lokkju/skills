"""Which forge a repo is on, from its origin remote, two per-clone git config keys and the config
file.

The git config keys, set per clone with `git config`, are the ones session-decisions reads, so
one setting fixes the links in both plugins:

- `tracker.web` (the https URL of the repo's web page) replaces the origin remote's host and path;
- `tracker.forge` (github, gitlab, forgejo, or gitea for forgejo) sets the kind.

The kind is `tracker.forge`, else `forge:` in `.claude/forge-tracker.local.md` (the per-repo
default, committed or not), else the built-in host rules. The API base and any pinned CA still
come only from the config file: git config holds the web address, not the API's.
"""

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
# What `tracker.forge` takes. Gitea is read as Forgejo, whose URLs and API it shares.
KINDS = {"github": "github", "gitlab": "gitlab", "forgejo": "forgejo", "gitea": "forgejo"}
OVERRIDE_FORGE, OVERRIDE_WEB = "tracker.forge", "tracker.web"

_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")
_SEGMENT = re.compile(r"^[\w.-]+$")
_CONFIG_PATH = ".claude/forge-tracker.local.md"


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


@dataclass(frozen=True)
class Override:
    """The per-clone `tracker.forge` (as a FORGES name) and `tracker.web`, when set."""

    forge: Optional[str] = None
    web: Optional[str] = None


def git_config(repo_dir: str, key: str, run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """`git config --get <key>` in the repo, or None when it's unset or empty."""
    try:
        result = run(
            ["git", "-C", repo_dir, "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        raise ConfigError("git isn't on PATH") from None
    value = (result.stdout or "").strip() if result.returncode == 0 else ""
    return value or None


def read_override(repo_dir: str, run: Callable[..., Any] = subprocess.run) -> Override:
    """The two keys, checked: a value that can't be used fails the command with the key named,
    rather than falling back to the remote it was set to replace."""
    forge = git_config(repo_dir, OVERRIDE_FORGE, run)
    web = git_config(repo_dir, OVERRIDE_WEB, run)
    kind = None
    if forge is not None:
        kind = KINDS.get(forge.lower())
        if kind is None:
            raise ConfigError(
                f"git config {OVERRIDE_FORGE} is {forge!r}; it takes github, gitlab, forgejo "
                "or gitea"
            )
    if web is not None:
        _web_parts(web)
    return Override(kind, web)


def _web_parts(web: str) -> tuple[str, list[str]]:
    """The host and path segments of a repo's https web URL; ConfigError for anything else."""
    problem = f"git config {OVERRIDE_WEB} is {web!r}, which isn't the https URL of a repo"
    try:
        parts = urllib.parse.urlsplit(web.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ConfigError(problem) from None
    if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(problem)
    if port is not None or not _HOST.match(host):
        raise ConfigError(problem)
    segments = parts.path.strip("/").removesuffix(".git").split("/")
    if len(segments) < 2 or any(not _SEGMENT.match(s) or s in (".", "..") for s in segments):
        raise ConfigError(problem)
    return host, segments


def web_remote(web: str, kind: str) -> Remote:
    """The repo a `tracker.web` URL names, read as session-decisions reads it: on GitLab the whole
    path is the project (groups nest); on GitHub and Forgejo the last two segments are the repo,
    and anything before them is the forge's subpath, which `api_base:` has to name."""
    host, segments = _web_parts(web)
    project = segments if kind == "gitlab" else segments[-2:]
    return Remote(host=host, project="/".join(project))


def host_kind(host: str) -> Optional[str]:
    """The built-in host rules: only hosts whose forge the name alone tells."""
    return {"github.com": "github", "gitlab.com": "gitlab"}.get(host)


def locate(
    repo_dir: str, configured: Optional[str], run: Callable[..., Any] = subprocess.run
) -> tuple[Remote, Optional[str], str]:
    """The repo's host and project, the kind named for it (None leaves it to the host rules), and
    where that kind came from.

    `tracker.web` replaces the origin remote, which then needn't exist. The kind is
    `tracker.forge`, else `configured` (the config file's `forge:`).
    """
    override = read_override(repo_dir, run)
    named, source = configured, f"forge: in {_CONFIG_PATH}"
    if override.forge:
        named, source = override.forge, f"git config {OVERRIDE_FORGE}"
    if override.web:
        host, _ = _web_parts(override.web)
        kind = named or host_kind(host)
        if kind is None:
            raise ConfigError(_unknown(host))
        return web_remote(override.web, kind), named, source
    return parse_remote(origin_url(repo_dir, run)), named, source


def resolve(
    repo_dir: str, configured: Optional[str], run: Callable[..., Any] = subprocess.run
) -> tuple[Remote, str]:
    """`locate`, then `detect_forge`: the repo and the forge forge-tracker reads it through."""
    remote, named, source = locate(repo_dir, configured, run)
    return remote, detect_forge(remote, named, source)


def _unknown(host: str) -> str:
    return (
        f"can't tell which forge {host} runs; set forge: (github, gitlab or forgejo) in "
        f"{_CONFIG_PATH}, or for this clone only `git config {OVERRIDE_FORGE} <kind>`"
    )


def detect_forge(remote: Remote, configured: Optional[str], source: str = "") -> str:
    """The named kind when there is one (`tracker.forge`, else `forge:`), else the host. Never a
    guess.

    The glab host check and the unauthenticated /api/v1/version probe join this when the GitLab
    and Forgejo adapters do; until then an unknown host needs a kind named.
    """
    forge = configured or host_kind(remote.host)
    if forge is None:
        raise ConfigError(_unknown(remote.host))
    if forge not in SUPPORTED:
        named = f" (by {source})" if configured and source else ""
        raise ConfigError(
            f"{remote.host} is {forge}{named}, and forge-tracker reads only GitHub so far "
            "(GitLab and Forgejo adapters are later phases)"
        )
    return forge
