"""`.claude/forge-tracker.local.md`: frontmatter only, read strictly.

The standard library has no YAML parser, so this reads the subset the config file needs: `key:
value` lines, one level of nested mappings, inline `[a, b]` and block `- item` lists, single and
double quoted strings, integers, `~` or `null` for none, and `#` comments. Anything outside that
subset, and any key not listed in KEYS, fails the load with the line or key named.

A label key or `ruling_pattern` set to `~` records that the repo has no such label or convention,
which is different from leaving it out: the reports say nothing about a key set to `~`, and they
do say when one is missing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .detect import FORGES
from .model import ConfigError

PATH = os.path.join(".claude", "forge-tracker.local.md")

LABEL_KEYS = ("needs_human", "decision", "decision_made", "blocked", "agent")
TIER_KEYS = ("labels", "milestone_prefixes")
KEYS = (
    "forge",
    "api_base",
    "ca_file",
    "labels",
    "tiers",
    "person_only_markers",
    "blocking",
    "ruling_pattern",
    "changelog",
    "report_tags",
    "stale_days",
    "write_wrapper",
    "groom",
)
GROOM_KEYS = ("keep_branches",)


@dataclass(frozen=True)
class Config:
    forge: Optional[str] = None
    api_base: Optional[str] = None
    ca_file: Optional[str] = None
    labels: dict[str, str] = field(default_factory=dict)
    tier_labels: tuple[str, ...] = ()
    milestone_prefixes: tuple[str, ...] = ()
    person_only_markers: tuple[str, ...] = ()
    blocking: Optional[str] = None
    ruling_pattern: Optional[re.Pattern] = None
    changelog: Optional[str] = None
    report_tags: tuple[str, ...] = ()
    stale_days: Optional[int] = None
    write_wrapper: Optional[str] = None
    # Globs of branch names /groom never proposes deleting, on top of its built-in ones.
    keep_branches: tuple[str, ...] = ()
    # Whether the file exists, and the keys it sets to `~` ("labels.needs_human", "ruling_pattern").
    found: bool = False
    set_to_none: frozenset[str] = frozenset()
    # When the user declined the offer to set it up (an ISO date), from the local cache.
    setup_declined: Optional[str] = None

    def label(self, key: str) -> Optional[str]:
        return self.labels.get(key)

    def tier(self, labels: tuple[str, ...], milestone: Optional[str]) -> Optional[str]:
        """The first configured tier label the item carries, else the first milestone prefix its
        milestone starts with, else None (untiered)."""
        for tier in self.tier_labels:
            if tier in labels:
                return tier
        if milestone:
            for prefix in self.milestone_prefixes:
                if milestone.startswith(prefix):
                    return prefix
        return None

    def setup_note(self, keys: tuple[str, ...]) -> Optional[str]:
        """One line saying which of `keys` ("labels.needs_human", "ruling_pattern") are missing,
        what the report can't do without them, and how to set them up; None when every key is
        set or set to `~`. The line starts with SETUP_PREFIX and says "setup was declined" when
        the user declined it here."""
        missing = [k for k in keys if self._get(k) is None and k not in self.set_to_none]
        if not missing:
            return None
        kinds = [k.split(".", 1)[1].replace("_", "-") for k in missing if k.startswith("labels.")]
        lost = []
        if kinds:
            lost.append(f"no label marks {_join(kinds, 'or')} items")
        if "ruling_pattern" in missing:
            lost.append("rulings aren't picked out")
        effect = _join(lost, "and")
        if self.setup_declined:
            how = (
                f"setup was declined on {self.setup_declined}; "
                "`forge-tracker labels suggest` still proposes a config"
            )
        else:
            how = "`forge-tracker labels suggest` proposes one from this repo's labels"
        if not self.found:
            return f"{SETUP_PREFIX} no {PATH}, so {effect}. {how[0].upper()}{how[1:]}"
        return (
            f"{SETUP_PREFIX} {_join(missing, 'and')} unset in {PATH}, so {effect}. Set them, or set "
            f"them to ~ to record that this repo has none; {how}"
        )

    def _get(self, key: str) -> Any:
        if key.startswith("labels."):
            return self.labels.get(key.split(".", 1)[1])
        return getattr(self, key)


SETUP_PREFIX = "setup:"


def _join(parts: list[str], word: str) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {word} " + parts[-1]


def load(repo_dir: str) -> Config:
    path = os.path.join(repo_dir, PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return Config()
    except OSError as error:
        raise ConfigError(f"can't read {PATH}: {error.strerror}") from None
    return replace(from_mapping(parse_frontmatter(text), PATH), found=True)


def parse_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return parse_block(lines[1:end], first_line=2)
    raise ConfigError(f"{PATH}: the frontmatter has no closing ---")


def _strip_comment(line: str) -> str:
    quote: Optional[str] = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(raw: str, where: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    if raw[0] == "'":
        if len(raw) < 2 or raw[-1] != "'":
            raise ConfigError(f"{where}: unterminated quoted string")
        return raw[1:-1].replace("''", "'")
    if raw[0] == '"':
        if len(raw) < 2 or raw[-1] != '"':
            raise ConfigError(f"{where}: unterminated quoted string")
        inner = raw[1:-1]
        return re.sub(r'\\(["\\/nt])', lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), inner)
    if raw[0] == "[":
        if raw[-1] != "]":
            raise ConfigError(f"{where}: unterminated [ list")
        return [_scalar(item, where) for item in _split_flow(raw[1:-1], where)]
    if raw[0] in "{&*!|>%@`":
        raise ConfigError(f"{where}: {raw[0]!r} isn't supported in this config file")
    if raw in ("~", "null"):
        return None
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return raw


def _split_flow(inner: str, where: str) -> list[str]:
    items, current, quote = [], "", None
    for c in inner:
        if quote:
            current += c
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            current += c
        elif c == ",":
            items.append(current)
            current = ""
        elif c in "[]{}":
            raise ConfigError(f"{where}: nested collections aren't supported")
        else:
            current += c
    if quote:
        raise ConfigError(f"{where}: unterminated quoted string")
    if current.strip():
        items.append(current)
    return [item for item in items if item.strip()]


_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?$")


def parse_block(lines: list[str], first_line: int = 1) -> dict[str, Any]:
    """Top-level keys at column 0, each with a scalar, an inline list, or an indented block of
    `sub: value` lines or `- item` lines."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        where = f"{PATH} line {first_line + i}"
        line = _strip_comment(lines[i])
        if not line.strip():
            i += 1
            continue
        if line[0] in " \t":
            raise ConfigError(f"{where}: unexpected indentation")
        match = _KEY.match(line)
        if not match:
            raise ConfigError(f"{where}: expected 'key: value'")
        key, raw = match.group(1), match.group(2)
        if key in result:
            raise ConfigError(f"{where}: {key} is set twice")
        i += 1
        if raw is not None and raw.strip():
            result[key] = _scalar(raw, where)
            continue
        # A nested block: every following indented line.
        block: list[tuple[int, str]] = []
        while i < len(lines):
            inner = _strip_comment(lines[i])
            if inner.strip() and inner[0] not in " \t":
                break
            if inner.strip():
                block.append((first_line + i, inner.strip()))
            i += 1
        result[key] = _nested(block, key)
    return result


def _nested(block: list[tuple[int, str]], key: str) -> Any:
    if not block:
        return None
    if all(text.startswith("- ") or text == "-" for _, text in block):
        return [_scalar(text[1:], f"{PATH} line {n}") for n, text in block]
    mapping: dict[str, Any] = {}
    for n, text in block:
        where = f"{PATH} line {n}"
        match = _KEY.match(text)
        if not match:
            raise ConfigError(f"{where}: expected 'key: value' under {key}")
        sub, raw = match.group(1), match.group(2)
        if sub in mapping:
            raise ConfigError(f"{where}: {key}.{sub} is set twice")
        if raw is None or not raw.strip():
            raise ConfigError(f"{where}: {key}.{sub} needs a value on the same line")
        mapping[sub] = _scalar(raw, where)
    return mapping


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{PATH}: {name} must be a list of strings")
    return tuple(value)


def _string(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{PATH}: {name} must be a string")
    return value


def from_mapping(data: dict[str, Any], source: str = PATH) -> Config:
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise ConfigError(f"{source}: unknown key {', '.join(unknown)}")
    forge = _string(data.get("forge"), "forge")
    if forge is not None and forge not in FORGES:
        raise ConfigError(f"{source}: forge must be one of {', '.join(FORGES)}, not {forge!r}")

    labels = data.get("labels") or {}
    if not isinstance(labels, dict):
        raise ConfigError(f"{source}: labels must be a mapping")
    unknown = sorted(set(labels) - set(LABEL_KEYS))
    if unknown:
        raise ConfigError(f"{source}: unknown key labels.{', labels.'.join(unknown)}")
    for name, value in labels.items():
        _string(value, f"labels.{name}")
    none = {f"labels.{name}" for name, value in labels.items() if value is None}
    if "ruling_pattern" in data and data["ruling_pattern"] is None:
        none.add("ruling_pattern")

    tiers = data.get("tiers") or {}
    if not isinstance(tiers, dict):
        raise ConfigError(f"{source}: tiers must be a mapping")
    unknown = sorted(set(tiers) - set(TIER_KEYS))
    if unknown:
        raise ConfigError(f"{source}: unknown key tiers.{', tiers.'.join(unknown)}")

    groom = data.get("groom") or {}
    if not isinstance(groom, dict):
        raise ConfigError(f"{source}: groom must be a mapping")
    unknown = sorted(set(groom) - set(GROOM_KEYS))
    if unknown:
        raise ConfigError(f"{source}: unknown key groom.{', groom.'.join(unknown)}")

    pattern = _string(data.get("ruling_pattern"), "ruling_pattern")
    try:
        ruling = re.compile(pattern, re.MULTILINE) if pattern else None
    except re.error as error:
        raise ConfigError(f"{source}: ruling_pattern isn't a valid regex: {error}") from None

    stale = data.get("stale_days")
    if stale is not None and (not isinstance(stale, int) or stale < 1):
        raise ConfigError(f"{source}: stale_days must be a positive integer")

    return Config(
        forge=forge,
        api_base=_string(data.get("api_base"), "api_base"),
        ca_file=_string(data.get("ca_file"), "ca_file"),
        labels={name: value for name, value in labels.items() if value is not None},
        tier_labels=_strings(tiers.get("labels"), "tiers.labels"),
        milestone_prefixes=_strings(tiers.get("milestone_prefixes"), "tiers.milestone_prefixes"),
        person_only_markers=_strings(data.get("person_only_markers"), "person_only_markers"),
        blocking=_string(data.get("blocking"), "blocking"),
        ruling_pattern=ruling,
        changelog=_string(data.get("changelog"), "changelog"),
        report_tags=_strings(data.get("report_tags"), "report_tags"),
        stale_days=stale,
        write_wrapper=_string(data.get("write_wrapper"), "write_wrapper"),
        keep_branches=_strings(groom.get("keep_branches"), "groom.keep_branches"),
        set_to_none=frozenset(none),
    )
