#!/usr/bin/env python3
"""Fail when a skill's frontmatter `name` differs from its directory name.

Checks every plugins/*/skills/*/SKILL.md under the repo root (the parent of this
script's directory). Standard library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def frontmatter_name(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def main() -> int:
    skills = sorted(ROOT.glob("plugins/*/skills/*/SKILL.md"))
    errors = []
    for skill in skills:
        expected = skill.parent.name
        name = frontmatter_name(skill)
        rel = skill.relative_to(ROOT)
        if name is None:
            errors.append(f"{rel}: no `name` in frontmatter (expected {expected!r})")
        elif name != expected:
            errors.append(f"{rel}: name {name!r} does not match directory {expected!r}")
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"{len(skills)} skill name(s) match their directories")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
