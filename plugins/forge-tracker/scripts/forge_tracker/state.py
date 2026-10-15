"""Local cache files: 0700 directories, 0600 files, written by a temp file and os.replace.

Nothing here is a source of truth; every file is safe to delete.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from typing import Any, Optional


def cache_root(environ: Mapping[str, str] = os.environ) -> str:
    """`$XDG_CACHE_HOME/forge-tracker`, or `~/.cache/forge-tracker` when it's unset, empty or
    relative (the XDG spec says to ignore a relative value)."""
    base = environ.get("XDG_CACHE_HOME", "")
    if not base or not os.path.isabs(base):
        home = environ.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".cache")
    return os.path.join(base, "forge-tracker")


def ensure_dir(path: str) -> None:
    """Create `path` and any missing parents as 0700 (whatever the umask says). Directories that
    already exist keep their mode, except `path` itself, which is set to 0700."""
    missing = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        os.chmod(directory, 0o700)
    os.chmod(path, 0o700)


def read_json(path: str) -> Optional[Any]:
    """The file's JSON, or None when it's missing or unreadable (a cache, so never fatal)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


class PendingWrite:
    """A 0600 temp file beside `path`, holding the new content until `commit` renames it into
    place. `discard` deletes it and leaves the old file untouched."""

    def __init__(self, path: str, data: Any):
        self.path = path
        directory = os.path.dirname(path)
        ensure_dir(directory)
        fd, self.temp = tempfile.mkstemp(prefix=".tmp-", dir=directory)  # mkstemp is 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(self.temp)
            raise
        self.done = False

    def commit(self) -> None:
        if not self.done:
            os.replace(self.temp, self.path)
            self.done = True

    def discard(self) -> None:
        if not self.done:
            try:
                os.unlink(self.temp)
            except FileNotFoundError:
                pass
            self.done = True


def write_json(path: str, data: Any) -> None:
    PendingWrite(path, data).commit()
