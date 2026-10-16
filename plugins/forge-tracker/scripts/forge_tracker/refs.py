"""Issue numbers named by a change's title or a branch name. `/catchup` groups merged changes by
them and `/next` marks issues in flight with them, so both read names the same way.

A branch names an issue only when the number is clearly one: a whole word at the start or end of
a path segment (`21-github-adapter`, `fix/21`, `sd-26`), or right after a word like `issue` or
`gh` (`issue-21-x`, `gh-21-x`). Digits inside a version (`cilium-1.20.2`, `v1`,
`garm-runner-2.338.0-38790455020`), a date (`plans-2026-10-08`), a long build or run id, a word
with a leading zero, a number in the middle of a name (`phase-2-docs`), and anything on a
dependency bot's branch (`dependabot/...`, `renovate/...`) aren't issue numbers. Neither is 0.
"""

from __future__ import annotations

import re
from typing import Optional

# `#N` not preceded by a word character or `/`, so `acme/other#9` and `abc#9` don't count.
HASH_REF = re.compile(r"(?<![\w/])#(\d+)\b")

# A date as dashed, dotted or underscored parts, with an optional build counter after it.
_DATE = re.compile(r"(?<![\d.])\d{4}[-._]\d{1,2}[-._]\d{1,2}(?:[-._]\d+)?(?![\d.])")
# Words that say the number after them is an issue.
_KEYWORDS = {"issue", "issues", "gh", "bug", "fix", "fixes", "closes", "resolves", "ticket"}
# Branches whose numbers are always versions.
_BOT_PREFIXES = ("dependabot/", "renovate/")
# Longer runs of digits are run ids, timestamps or hashes, not issue numbers.
_MAX_DIGITS = 6


def _number(word: str) -> Optional[int]:
    if not word.isdigit() or len(word) > _MAX_DIGITS or word.startswith("0"):
        return None
    return int(word)


def branch_numbers(branch: str) -> set[int]:
    """The issue numbers a branch name clearly names (see the module docstring)."""
    if branch.startswith(_BOT_PREFIXES):
        return set()
    out: set[int] = set()
    for segment in branch.split("/"):
        # Dots stay inside a word, so a version such as `1.20.2` is one word with no number.
        words = re.split(r"[-_ ]+", _DATE.sub(" ", segment).strip())
        words = [w for w in words if w]
        for position, word in enumerate(words):
            number = _number(word)
            if number is None:
                continue
            at_edge = position == 0 or position == len(words) - 1
            after_keyword = position > 0 and words[position - 1].lower() in _KEYWORDS
            if at_edge or after_keyword:
                out.add(number)
    return out


def title_numbers(title: str) -> set[int]:
    """`#N` references in a change's title, never `#0`."""
    return {int(n) for n in HASH_REF.findall(title)} - {0}
