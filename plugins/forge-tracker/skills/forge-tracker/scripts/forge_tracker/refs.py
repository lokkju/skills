"""Issue numbers named by a change's title or a branch name. `/catchup` groups merged changes by
them and `/next` marks issues in flight with them, so both read names the same way.

A branch names an issue only when the number is clearly one: a whole word at the start or end of
a path segment (`21-github-adapter`, `fix/21`, `sd-26`), or right after a word like `issue` or
`gh` (`issue-21-x`, `gh-21-x`). Digits inside a version, dotted or dashed (`cilium-1.20.2`, `v1`,
`garm-runner-2.338.0-38790455020`, `task-tools-2-1-300`), a date (`plans-2026-10-08`), a long
build or run id, a word with a leading zero, a number in the middle of a name (`phase-2-docs`),
and anything on a dependency bot's branch (`dependabot/...`, `renovate/...`) aren't issue
numbers. Neither is 0.

A title names an issue only by a reference the forge itself links: `#21`, `owner/repo#21` when
that is this project, or `GH-21`. A bare number never counts, so the `300` in "since 2.1.300", a
version such as `v0.17.1`, and a date are all ignored.

`prose_blocks` reads the other kind of reference: blocking an issue body states in words
(`Blocked by: #17`) instead of as a native link, which `/groom` proposes to link natively.
"""

from __future__ import annotations

import re
from typing import Optional

# `#N` not preceded by a word character or `/`, so `acme/other#9` and `abc#9` don't count, and
# not followed by `.digit`, so `#1.2` isn't issue 1.
HASH_REF = re.compile(r"(?<![\w/])#(\d+)\b(?!\.\d)")
# `owner/repo#N` (the project is compared with this one) or `GH-N`, GitHub's other short form.
_PROJECT_REF = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b(?!\.\d)")
_GH_REF = re.compile(r"(?<![\w/.-])GH-(\d+)\b(?![.-]\d)", re.IGNORECASE)


def change_ref(forge: Optional[str], number: int) -> str:
    """A change written the forge's way: `!N` for a GitLab merge request, `#N` for a pull request
    on GitHub and Forgejo, where issues and pull requests share one run of numbers."""
    return f"!{number}" if forge == "gitlab" else f"#{number}"


# Forgejo's (and Gitea's) default closing keywords, then one reference or a comma- or
# "and"-separated list of them.
_CLOSING = re.compile(
    r"(?<![\w-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+(?:\s*(?:,|and)\s*(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)*)",
    re.IGNORECASE,
)
_ONE_REF = re.compile(r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)")


def closing_numbers(body: str, project: Optional[str] = None) -> list[int]:
    """The issues in this project a pull request body closes with a closing keyword (`Closes #4`,
    `fixes acme/widgets#5`, `Resolves #6, #7`), in order and once each. A reference to another
    project doesn't count. Forgejo has no API for these links, so its adapter reads them here."""
    out: list[int] = []
    for match in _CLOSING.finditer(_strip_code(body or "")):
        for other, number in _ONE_REF.findall(match.group(1)):
            if other and (project is None or other.lower() != project.lower()):
                continue
            if int(number) not in out:
                out.append(int(number))
    return out


def _strip_code(text: str) -> str:
    """The text without fenced code blocks and inline code spans, where a keyword is an example."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", " ", text)


def change_word(forge: Optional[str]) -> str:
    """What the forge calls a change: MR on GitLab, PR elsewhere."""
    return "MR" if forge == "gitlab" else "PR"


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


def _numeric(words: list[str], position: int) -> bool:
    return 0 <= position < len(words) and words[position].isdigit()


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
            # Two number words side by side are a dashed version (`2-1-300`, `k3s-1-36`).
            if _numeric(words, position - 1) or _numeric(words, position + 1):
                continue
            at_edge = position == 0 or position == len(words) - 1
            after_keyword = position > 0 and words[position - 1].lower() in _KEYWORDS
            if at_edge or after_keyword:
                out.add(number)
    return out


def title_numbers(title: str, project: Optional[str] = None) -> set[int]:
    """The issues a change's title references (see the module docstring): `#N`, `GH-N`, and
    `owner/repo#N` when it names `project` (compared without case). Never 0, a number with a
    leading zero, or one longer than a run id's worth of digits."""
    found = HASH_REF.findall(title) + _GH_REF.findall(title)
    if project:
        found += [n for p, n in _PROJECT_REF.findall(title) if p.lower() == project.lower()]
    return {number for n in found if (number := _number(n)) is not None}


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


# --- paths and symbols an issue body names ------------------------------------------------------

# File extensions that make a bare word a file name (`groom.py`), not a domain (`github.com`) or
# an abbreviation (`e.g.`).
_EXT = (
    "bash|c|cfg|cjs|conf|cpp|cs|css|go|h|hpp|html|ini|java|js|json|jsx|kt|lock|md|mjs|py|rb|rs|"
    "scss|sh|sql|swift|toml|ts|tsx|txt|xml|yaml|yml|zsh"
)
_PATH_CHARS = r"[A-Za-z0-9_.@+-]"
# A bare path in prose: segments joined by `/`, ending in a file name with one of the extensions.
_BARE_PATH = re.compile(
    rf"(?<![\w/.:@-])((?:\.{{0,2}}/)?(?:{_PATH_CHARS}+/)*{_PATH_CHARS}*[A-Za-z0-9_]\.(?:{_EXT}))"
    rf"(?![\w/-])"
)
# A path inside a code span: no spaces, at least one `/` or a known extension.
_SPAN_PATH = re.compile(rf"^(?:\.{{0,2}}/)?(?:{_PATH_CHARS}+/)*{_PATH_CHARS}+/?$")
# An identifier, dotted or `::`-joined, with an optional `()`.
_SYMBOL = re.compile(r"^[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*(?:\(\))?$")
# File names so common that naming one alone says nothing about which file; they match only as
# a longer path (`plugins/forge-tracker/README.md`).
GENERIC_FILES = {
    "readme.md",
    "changelog.md",
    "license",
    "license.md",
    "agents.md",
    "claude.md",
    "skill.md",
    "plugin.json",
    "package.json",
    "marketplace.json",
    "pyproject.toml",
    "cargo.toml",
    "config.toml",
    "__init__.py",
    "conftest.py",
    "setup.py",
    "main.py",
    "index.ts",
    "index.js",
}


def _symbol_like(word: str) -> bool:
    """An identifier that's clearly code: snake_case, camelCase, or SCREAMING_CASE with an
    underscore, at least four characters. A plain word in backticks (`labels`, `none`) isn't."""
    if len(word) < 4 or not re.fullmatch(r"[A-Za-z_]\w*", word):
        return False
    return "_" in word.strip("_") or bool(re.search(r"[a-z][A-Z]", word))


def _body_lines(body: str) -> list[str]:
    """The body's lines without fenced code blocks, quoted lines, HTML comments and struck-out
    text. Code spans stay: a path or symbol in one is exactly what's wanted."""
    lines = []
    fence: Optional[str] = None
    for line in _COMMENT.sub(" ", body or "").splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1) == fence:
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            continue
        if line.lstrip().startswith(">"):
            continue
        lines.append(_STRUCK.sub(" ", line))
    return lines


def body_names(body: str) -> tuple[list[str], list[str]]:
    """The file paths and code symbols an issue body names, as `(paths, symbols)`, each once in
    order. Paths come from code spans and from bare words ending in a file extension; a URL and
    a directory (`plugins/forge-tracker/`) don't count. Symbols come from code spans only
    (`check_merged`, `adapter.closing_refs()`, `ClosingRef`); for a dotted name, its last part
    is the symbol. Fenced code blocks, quoted lines, HTML comments and struck-out text are left
    out."""
    paths: list[str] = []
    symbols: list[str] = []

    def add(into: list[str], value: str) -> None:
        if value and value not in into:
            into.append(value)

    for line in _body_lines(body):
        for span in _CODE_SPAN.finditer(line):
            content = span.group(0).strip("`").strip()
            if not content or " " in content or "://" in content:
                continue
            if _SPAN_PATH.match(content) and (
                "/" in content.strip("/") or re.search(rf"\.(?:{_EXT})$", content)
            ):
                if not content.endswith("/"):
                    add(paths, _clean_path(content))
                continue
            if _SYMBOL.match(content):
                last = re.split(r"\.|::", content.removesuffix("()"))[-1]
                if _symbol_like(last):
                    add(symbols, last)
        prose = re.sub(r"\b\w+://\S+", " ", _CODE_SPAN.sub(" ", line))
        for match in _BARE_PATH.finditer(prose):
            add(paths, _clean_path(match.group(1)))
    return paths, symbols


def _clean_path(path: str) -> str:
    while path.startswith(("./", "../")):
        path = path.split("/", 1)[1]
    return path.lstrip("/")


def path_matches(named: str, changed: str) -> bool:
    """Whether a path an issue names is the file a change touched: the same path, or its tail
    (`forge_tracker/groom.py` names `.../scripts/forge_tracker/groom.py`). A generic file name
    alone (`README.md`) never matches; it needs a directory in front of it."""
    if not named or not changed:
        return False
    if named == changed:
        return True
    if "/" not in named and named.lower() in GENERIC_FILES:
        return False
    return changed.endswith("/" + named)


def patch_names(patch: str, symbol: str) -> bool:
    """Whether a diff changes something named `symbol`: the name on an added or removed line, or
    in a hunk header (the function or class the hunk is in). Context lines don't count; they are
    what a change left alone."""
    word = re.compile(rf"(?<!\w){re.escape(symbol)}(?!\w)")
    for line in (patch or "").splitlines():
        if line.startswith("@@"):
            parts = line.split("@@", 2)
            if len(parts) == 3 and word.search(parts[2]):
                return True
        elif (
            line.startswith(("+", "-"))
            and not line.startswith(("+++", "---"))
            and word.search(line[1:])
        ):
            return True
    return False
