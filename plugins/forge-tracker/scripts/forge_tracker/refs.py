"""Issue numbers named by a change's title or a branch name. `/catchup` groups merged changes by
them and `/next` marks issues in flight with them, so both read names the same way.

A branch names an issue only when the number is clearly one: a whole word at the start or end of
a path segment (`21-github-adapter`, `fix/21`, `sd-26`), or right after a word like `issue` or
`gh` (`issue-21-x`, `gh-21-x`). Digits inside a version (`cilium-1.20.2`, `v1`,
`garm-runner-2.338.0-38790455020`), a date (`plans-2026-10-08`), a long build or run id, a word
with a leading zero, a number in the middle of a name (`phase-2-docs`), and anything on a
dependency bot's branch (`dependabot/...`, `renovate/...`) aren't issue numbers. Neither is 0.

`prose_blocks` reads the other kind of reference: blocking an issue body states in words
(`Blocked by: #17`) instead of as a native link, which `/groom` proposes to link natively.
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


# --- blocking written as prose ------------------------------------------------------------------

# `#N` or `owner/repo#N`. A bare `#N` mustn't follow a word character or `/`.
_REF_RE = re.compile(
    r"(?:(?P<project>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)|(?<![\w/]))#(?P<number>\d+)\b"
)
_REF = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|(?<![\w/]))#\d+\b"
# Emphasis, a colon and spaces between a keyword and its first reference (`**Blocked by:** #17`).
_GAP = r"[\s*_:]*"
# What may separate references in one list: commas, `and`, `&`, spaces and emphasis.
_SEP = r"[\s*_]*(?:,|&|\band\b)?[\s*_]*"
_LIST = rf"{_GAP}(?P<refs>{_REF}(?:{_SEP}{_REF})*)"
# `blocked by`, `blocked on` and `depends on` anywhere in a line; `blocks` and `blocking` only
# where a line starts (after a list marker, a task box and emphasis), since "blocks" is also a
# noun ("code blocks #3").
_LINE_START = r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\[[ xX]\]\s+)?[*_]*"
_BLOCKED_BY = re.compile(rf"\b(?:blocked\s+(?:by|on)|depends\s+on){_LIST}", re.IGNORECASE)
_BLOCKS = re.compile(rf"{_LINE_START}(?:blocks|blocking)\b{_LIST}", re.IGNORECASE)
# Words before a keyword that say the opposite: "no longer blocked by", "isn't blocked by".
_NEGATED = re.compile(r"(?:\bnot|\bno\s+longer|n't|\bnever)\s+(?:been\s+|be\s+)?$", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_STRUCK = re.compile(r"~~.+?~~")


def _strip(text: str) -> list[str]:
    """The body's lines with code blocks, quotes, comments, code spans and struck-out text
    blanked, so a reference inside any of them isn't read as a claim about the issue."""
    out = []
    fence: Optional[str] = None
    for line in _COMMENT.sub(" ", text).splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1) == fence:
                fence = None
            out.append("")
        elif opened:
            fence = opened.group(1)
            out.append("")
        elif line.lstrip().startswith(">"):
            out.append("")
        else:
            out.append(_STRUCK.sub(" ", _CODE_SPAN.sub(" ", line)))
    return out


def prose_blocks(body: str) -> list[tuple[str, Optional[str], int, str]]:
    """Blocking an issue body states in words, as `(direction, project, number, line)`.

    `direction` is `blocked_by` (this issue waits on the reference) or `blocks` (the reference
    waits on this issue); `project` is None for a bare `#N`; `line` is the line it was read
    from, trimmed. The forms read are `Blocked by: #17`, `blocked on #17`, `Depends on #17, #18`,
    and, only where a line starts, `Blocks #18` and `Blocking: acme/other#4`. Code blocks and
    spans, quoted lines, HTML comments, struck-out text and negations ("no longer blocked by
    #17") don't count. Each reference is listed once per direction; the first line wins."""
    out: list[tuple[str, Optional[str], int, str]] = []
    seen: set[tuple[str, Optional[str], int]] = set()
    for line in _strip(body or ""):
        for direction, pattern in (("blocked_by", _BLOCKED_BY), ("blocks", _BLOCKS)):
            for match in pattern.finditer(line):
                if direction == "blocked_by" and _NEGATED.search(line[: match.start()]):
                    continue
                for ref in _REF_RE.finditer(match.group("refs")):
                    number = _number(ref.group("number"))
                    if number is None:
                        continue
                    key = (direction, ref.group("project"), number)
                    if key not in seen:
                        seen.add(key)
                        out.append((direction, ref.group("project"), number, line.strip()))
    return out
