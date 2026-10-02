"""Parse, validate and format the subjects of decision items.

A labelled subject is "<label> <KIND>: <body>", for example
"D3 DECIDE: retire the old deploy script? (recommend yes) [#812]".
The assistant writes "DECIDE: ..." or "ACTION: ..."; the PreToolUse hook adds the label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LETTER = {"DECIDE": "D", "ACTION": "A"}

# An optional existing label ("D12 "), the kind keyword, a colon, then the body.
_DRAFT = re.compile(r"^\s*(?:[DA]\d+\s+)?(DECIDE|ACTION)\s*:\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL)
_LABELLED = re.compile(r"^([DA])(\d+) (DECIDE|ACTION): (.*)$", re.DOTALL)
# The link is the last bracketed token, at the very end of the body.
_LINK = re.compile(r"\[[^\[\]]*\S[^\[\]]*\]$")
_RECOMMEND = re.compile(r"\(recommend\s+\S", re.IGNORECASE)


@dataclass(frozen=True)
class Draft:
    kind: str
    body: str


@dataclass(frozen=True)
class Label:
    letter: str
    number: int
    kind: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.letter}{self.number}"


def parse_draft(subject: str) -> Draft | None:
    """Return the draft when the subject is a decision item; None for any other task."""
    match = _DRAFT.match(subject or "")
    if match is None:
        return None
    return Draft(kind=match.group(1).upper(), body=match.group(2))


def problems(draft: Draft) -> list[str]:
    """Return what's wrong with a draft, as short instructions; empty when it's well formed."""
    if not draft.body:
        return ["write the question or action after the colon"]
    found = []
    if not _LINK.search(draft.body):
        found.append(
            "end the subject with one link token: [#123], [!45], [owner/repo#123], [<url>] or [no link]"
        )
    if draft.kind == "DECIDE" and not _RECOMMEND.search(draft.body):
        found.append("a DECIDE item needs a recommendation, written as (recommend <choice>)")
    return found


def format_subject(letter: str, number: int, draft: Draft) -> str:
    return f"{letter}{number} {draft.kind}: {draft.body}"


def parse_label(subject: str) -> Label | None:
    """Parse a subject the hook has already labelled; None for anything else."""
    match = _LABELLED.match(subject or "")
    if match is None:
        return None
    return Label(letter=match.group(1), number=int(match.group(2)), kind=match.group(3), body=match.group(4))
