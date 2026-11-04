"""What the adapters share: picking the newest release or tag for each configured glob."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from datetime import datetime
from typing import Optional

from ..model import Tag


def pick_tags(
    patterns: Iterable[str],
    releases: Iterable[tuple[Optional[datetime], str]],
    tag_names: list[str],
    forge_name: str,
) -> list[Tag]:
    """The newest dated release matching each glob, else the first tag that matches (the forges
    list tags newest first), else None. `registry:` entries aren't read yet and come back with a
    note, not a failure."""
    dated_releases = [(when, name) for when, name in releases if when is not None]
    out = []
    for pattern in patterns:
        if pattern.startswith("registry:"):
            out.append(Tag(pattern, None, note=f"registry tags aren't read on {forge_name} yet"))
            continue
        dated = [
            (when, name) for when, name in dated_releases if fnmatch.fnmatchcase(name, pattern)
        ]
        if dated:
            when, name = max(dated)
            out.append(Tag(pattern, name, when))
            continue
        names = [n for n in tag_names if fnmatch.fnmatchcase(n, pattern)]
        out.append(Tag(pattern, names[0] if names else None))
    return out
