"""`forge-tracker labels`: propose a `.claude/forge-tracker.local.md` from the labels the repo
already has, and remember a declined offer to set one up.

`suggest` reads the forge and prints a proposal; it writes nothing. Writing the config file is the
assistant's job, and only on the user's yes. `decline` writes one local cache file and reads no
forge. Neither one creates a label: creating one isn't a write operation yet, so a missing label
is a command printed for the user to run.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

from . import state
from .config import LABEL_KEYS, PATH, Config
from .detect import Remote
from .model import Label

# Per role, normalized-name patterns in order of preference; the first pattern any label matches
# wins, and among its matches the shortest name. Names are normalized by `normalize`.
CANDIDATES: dict[str, tuple[str, ...]] = {
    "needs_human": (
        r"needs-human",
        r"human",
        r"needs-human-.+",
        r"needs-(?:owner|person|maintainer|people)",
        r".*human.*",
    ),
    "decision": (
        r"needs-decision",
        r"decision-needed",
        r"decision",
        r"question",
        r"(?!.*(?:made|done)).*decision.*",
    ),
    "decision_made": (r"decision-made", r"decided", r"ruled", r"ruling"),
    "blocked": (r"blocked", r"(?:status-)?blocked-.+", r".+-blocked", r"on-hold"),
    "agent": (r"agent-ready", r"agent", r"ai-ready", r"agent-.+", r".+-agent", r"claude"),
}

# What to call a label that doesn't exist yet, and its description, for the create command.
DEFAULT_NAMES: dict[str, tuple[str, str]] = {
    "needs_human": ("needs-human", "Needs a person to act; agents leave it alone"),
    "decision": ("decision", "Waits on a decision"),
    "decision_made": ("decided", "The decision is recorded in a comment"),
    "blocked": ("blocked", "Can't start until something else lands"),
    "agent": ("agent-ready", "Ready for an agent to pick up"),
}

# Priority families: (pattern with one group, how to order the group). The family with the most
# labels wins; a tie goes to the earlier family.
_WORDS = ("critical", "urgent", "high", "medium", "normal", "low")
TIER_FAMILIES: tuple[tuple[str, Any], ...] = (
    (r"p([0-9])", int),
    (r"priority-p?([0-9])", int),
    (r"priority-(" + "|".join(_WORDS) + r")", _WORDS.index),
    (r"tier-?([0-9])", int),
)


def normalize(name: str) -> str:
    """Lowercase, with runs of spaces, `_`, `:`, `/` and `.` as one `-`."""
    return re.sub(r"[\s_:/.]+", "-", name.strip().lower()).strip("-")


def match_role(role: str, names: Sequence[str], taken: set[str]) -> Optional[str]:
    for pattern in CANDIDATES[role]:
        hits = [n for n in names if n not in taken and re.fullmatch(pattern, normalize(n))]
        if hits:
            return min(hits, key=lambda n: (len(n), n))
    return None


def match_tiers(names: Sequence[str]) -> list[str]:
    best: list[str] = []
    for pattern, order in TIER_FAMILIES:
        found = []
        for name in names:
            m = re.fullmatch(pattern, normalize(name))
            if m:
                found.append((order(m.group(1)), name))
        if len(found) > len(best):
            best = [name for _, name in sorted(found)]
    return best


def suggest(labels: Sequence[Label], cfg: Config) -> dict[str, Any]:
    """The proposal: each role's label (an existing config value wins, `~` stays `~`), the tier
    labels, and the roles nothing matched, with a name to create for each."""
    names = [label.name for label in labels]
    taken = set(cfg.labels.values())
    roles: dict[str, dict[str, Any]] = {}
    for role in LABEL_KEYS:
        if role in cfg.labels:
            roles[role] = {"label": cfg.labels[role], "source": "config"}
        elif f"labels.{role}" in cfg.set_to_none:
            roles[role] = {"label": None, "source": "config"}
        else:
            hit = match_role(role, names, taken)
            if hit:
                taken.add(hit)
            roles[role] = {"label": hit, "source": "matched" if hit else "none"}
    if cfg.tier_labels:
        tiers, tier_source = list(cfg.tier_labels), "config"
    else:
        tiers = match_tiers(names)
        tier_source = "matched" if tiers else "none"
    if cfg.ruling_pattern is not None:
        ruling = {"pattern": cfg.ruling_pattern.pattern, "source": "config"}
    else:
        ruling = {
            "pattern": None,
            "source": "config" if "ruling_pattern" in cfg.set_to_none else "none",
        }
    existing = {name.lower() for name in names}
    missing = []
    for role, entry in roles.items():
        if entry["source"] != "none":
            continue
        name, description = DEFAULT_NAMES[role]
        if name.lower() in existing:  # taken by another role; don't propose a clash
            continue
        missing.append({"key": role, "name": name, "description": description})
    return {
        "roles": roles,
        "tiers": {"labels": tiers, "source": tier_source},
        "ruling_pattern": ruling,
        "missing": missing,
    }


_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/+-]*")


def yaml_str(value: str) -> str:
    """A label name as the config parser reads it back: bare when that's safe, else quoted."""
    if (
        _PLAIN.fullmatch(value)
        and value == value.strip()
        and value not in ("~", "null")
        and not re.fullmatch(r"-?[0-9]+", value)
    ):
        return value
    return "'" + value.replace("'", "''") + "'"


def _comment(entry: dict[str, Any]) -> str:
    if entry["source"] == "matched":
        return "  # matched an existing label"
    if entry["source"] == "none":
        return "  # no such label here; ~ records that"
    return ""


def frontmatter_lines(proposal: dict[str, Any], only_new: bool) -> list[str]:
    """The proposal as frontmatter lines. `only_new` leaves out what the config already sets."""
    lines: list[str] = []
    roles = [
        (role, entry)
        for role, entry in proposal["roles"].items()
        if not (only_new and entry["source"] == "config")
    ]
    if roles:
        lines.append("labels:")
        for role, entry in roles:
            value = "~" if entry["label"] is None else yaml_str(entry["label"])
            lines.append(f"  {role}: {value}{_comment(entry)}")
    tiers = proposal["tiers"]
    if tiers["labels"] and not (only_new and tiers["source"] == "config"):
        listed = ", ".join(yaml_str(t) for t in tiers["labels"])
        lines += ["tiers:", f"  labels: [{listed}]{_comment(tiers)}"]
    ruling = proposal["ruling_pattern"]
    if ruling["source"] == "none":
        lines.append("ruling_pattern: ~  # or a regex that picks out ruling comments")
    return lines


def create_command(remote: Remote, item: Mapping[str, str]) -> str:
    repo = remote.project if remote.host == "github.com" else f"{remote.host}/{remote.project}"
    description = item["description"].replace('"', '\\"')
    return f'gh label create {item["name"]} -R {repo} --description "{description}"'


def report(labels: Sequence[Label], cfg: Config, remote: Remote, now: datetime) -> dict[str, Any]:
    proposal = suggest(labels, cfg)
    for item in proposal["missing"]:
        item["command"] = create_command(remote, item)
    lines = frontmatter_lines(proposal, only_new=cfg.found)
    if cfg.found:
        text = "\n".join(lines) + "\n" if lines else ""
    else:
        text = (
            "---\n"
            + "\n".join(lines)
            + "\n---\n"
            + f"Proposed by `forge-tracker labels suggest` on {now.date().isoformat()}. "
            + "A key set to ~ records that this repo has no such label.\n"
        )
    return {
        "project": remote.project,
        "host": remote.host,
        "labels_read": [label.name for label in labels],
        "config_exists": cfg.found,
        "path": PATH,
        "proposal": proposal,
        "content": text,
    }


def render(data: Mapping[str, Any]) -> str:
    count = len(data["labels_read"])
    head = f"{data['project']} on {data['host']}: {count} label{'s' if count != 1 else ''}"
    out = [head, ""]
    if data["config_exists"]:
        if not data["content"]:
            out.append(f"{data['path']} already sets every key this proposes; nothing to add.")
            return "\n".join(out) + "\n"
        out.append(f"{data['path']} exists. Add these keys to its frontmatter (merge into its")
        out.append("labels: and tiers: blocks where it has them):")
    else:
        out.append(f"Proposed {data['path']} (nothing written):")
    out += ["", data["content"].rstrip("\n"), ""]
    missing = data["proposal"]["missing"]
    if missing:
        out.append("Not on the forge (set to ~ above). To create them instead, the user can run:")
        out += [f"  {item['command']}" for item in missing]
        out.append("and then put each name in place of its ~.")
    return "\n".join(out) + "\n"


# --- the declined offer -----------------------------------------------------------------------


def decline_path(remote: Remote, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(state.cache_root(environ), "setup", f"{remote.key}.json")


def record_decline(remote: Remote, environ: Mapping[str, str], now: datetime) -> str:
    path = decline_path(remote, environ)
    state.write_json(
        path,
        {"declined": now.date().isoformat(), "host": remote.host, "project": remote.project},
    )
    return path


def with_decline(cfg: Config, remote: Remote, environ: Mapping[str, str]) -> Config:
    """`cfg` with `setup_declined` set when the user declined setup for this repo."""
    recorded = state.read_json(decline_path(remote, environ))
    if isinstance(recorded, dict) and isinstance(recorded.get("declined"), str):
        return replace(cfg, setup_declined=recorded["declined"])
    return cfg
