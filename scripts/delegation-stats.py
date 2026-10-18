#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Measure how much a Claude Code project's sessions delegate to subagents.

Reads the session transcripts under ~/.claude/projects/<project>/ and prints, per session:
prompts that made tool calls, the share of those with an Agent call, the longest run of reads
with no Agent call, runs of 10 or more reads, median and peak main-thread context, and the
tokens its subagents used. Numbers only; it never prints prompt, tool input or result content.

Usage:
  uv run scripts/delegation-stats.py PROJECT [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                     [--session ID-PREFIX ...] [--json]

PROJECT is a directory under ~/.claude/projects/, its name, or a working directory, which maps
to a project by replacing every non-alphanumeric character with `-`. Dates are UTC and
inclusive; a prompt is in range when its own timestamp is.

The transcript format is internal to Claude Code and changes between versions. A record shape
this script doesn't recognize stops it with exit status 2, naming the file and line.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"Bash", "Read", "Grep", "Glob", "WebFetch", "WebSearch"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
LONG_RUN = 10

# User strings that Claude Code injects rather than the person typing them.
INJECTED_PREFIXES = ("<task-notification>", "<system-reminder>", "<local-command")
# User records carrying these flags are injected too: skill bodies, compaction summaries.
INJECTED_FLAGS = ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")

# Every record type seen in transcripts so far. Only user and assistant records are measured;
# the rest are bookkeeping. A type outside this set is an unrecognized shape.
MEASURED_TYPES = frozenset({"user", "assistant"})
IGNORED_TYPES = frozenset(
    {
        "agent-name",
        "agent-setting",
        "ai-title",
        "api-request",
        "api-request-blob",
        "api-request-shape",
        "artifact-autoreact-ledger",
        "artifact-comment-monitor",
        "atis-latch",
        "attachment",
        "bridge-session",
        "continued-in",
        "cost-state",
        "custom-title",
        "dev-mods",
        "file-history-delta",
        "file-history-snapshot",
        "fork-context-ref",
        "frame-link",
        "last-prompt",
        "mode",
        "permission-mode",
        "pr-link",
        "queue-operation",
        "relocated",
        "summary",
        "system",
        "worktree-state",
    }
)
USER_BLOCKS = frozenset({"tool_result", "text", "image", "document"})
ASSISTANT_BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})
CONTEXT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TOKEN_KEYS = (*CONTEXT_KEYS, "output_tokens")


class FormatError(Exception):
    """A record this script doesn't recognize. The message never quotes record content."""

    def __init__(self, path: Path, line: int, what: str) -> None:
        super().__init__(f"{path}:{line}: {what}")


@dataclass
class Prompt:
    timestamp: datetime
    tools: Counter = field(default_factory=Counter)
    longest_read_run: int = 0
    peak_context: int = 0

    @property
    def agent(self) -> bool:
        return any(self.tools[name] for name in AGENT_TOOLS)


@dataclass
class Session:
    session_id: str
    prompts: list[Prompt] = field(default_factory=list)
    contexts: list[int] = field(default_factory=list)
    runs: list[int] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)


def parse_timestamp(value: Any, path: Path, line: int) -> datetime:
    if not isinstance(value, str):
        raise FormatError(path, line, "timestamp missing or not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FormatError(path, line, "timestamp is not ISO 8601") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def records(path: Path):
    """Yield (line number, record) for each measured record, validating every record's type."""
    with path.open(encoding="utf-8") as handle:
        for line, text in enumerate(handle, 1):
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                raise FormatError(path, line, "not valid JSON") from None
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                raise FormatError(path, line, "record is not an object with a string `type`")
            kind = record["type"]
            if kind in IGNORED_TYPES:
                continue
            if kind not in MEASURED_TYPES:
                raise FormatError(path, line, f"unrecognized record type {kind!r}")
            if not isinstance(record.get("isSidechain"), bool):
                raise FormatError(path, line, f"{kind} record without a boolean `isSidechain`")
            message = record.get("message")
            if not isinstance(message, dict):
                raise FormatError(path, line, f"{kind} record without a `message` object")
            yield line, record


def prompt_kind(record: dict[str, Any], path: Path, line: int) -> str:
    """Classify a user record as 'prompt', 'injected' or 'other' (tool results and the like)."""
    content = record["message"].get("content")
    if isinstance(content, str):
        if any(record.get(flag) for flag in INJECTED_FLAGS):
            return "injected"
        if content.lstrip().startswith(INJECTED_PREFIXES):
            return "injected"
        return "prompt"
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in USER_BLOCKS:
                kind = block.get("type") if isinstance(block, dict) else type(block).__name__
                raise FormatError(path, line, f"user content block of unrecognized type {kind!r}")
        return "other"
    raise FormatError(path, line, "user `message.content` is neither a string nor a list")


def assistant_parts(record: dict[str, Any], path: Path, line: int):
    """Return (message id, usage token counts, tool_use blocks as (id, name)) for a record."""
    message = record["message"]
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise FormatError(path, line, "assistant record without a string `message.id`")
    usage = message.get("usage")
    if not isinstance(usage, dict):
        raise FormatError(path, line, "assistant record without a `message.usage` object")
    tokens = {}
    for key in TOKEN_KEYS:
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise FormatError(path, line, f"`message.usage.{key}` missing or not an integer")
        tokens[key] = value
    content = message.get("content")
    if not isinstance(content, list):
        raise FormatError(path, line, "assistant `message.content` is not a list")
    calls = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in ASSISTANT_BLOCKS:
            shown = kind if kind is not None else type(block).__name__
            raise FormatError(path, line, f"assistant content block of unrecognized type {shown!r}")
        if kind == "tool_use":
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                raise FormatError(path, line, "tool_use block without a string `id` and `name`")
            calls.append((block["id"], block["name"]))
    return message_id, tokens, calls


def in_range(moment: datetime, since: date | None, until: date | None) -> bool:
    day = moment.astimezone(timezone.utc).date()
    return (since is None or day >= since) and (until is None or day <= until)


def read_session(path: Path, since: date | None, until: date | None) -> Session:
    session = Session(session_id=path.stem)
    prompt: Prompt | None = None
    in_window = False
    run = 0
    seen_responses: set[str] = set()
    seen_calls: set[str] = set()

    def close_run() -> None:
        nonlocal run
        if run and prompt is not None and in_window:
            session.runs.append(run)
            prompt.longest_read_run = max(prompt.longest_read_run, run)
        run = 0

    for line, record in records(path):
        if record["isSidechain"]:
            continue
        stamp = parse_timestamp(record.get("timestamp"), path, line)
        if record["type"] == "user":
            if prompt_kind(record, path, line) != "prompt":
                continue
            close_run()
            prompt = Prompt(timestamp=stamp)
            in_window = in_range(stamp, since, until)
            if in_window:
                session.prompts.append(prompt)
            continue
        message_id, tokens, calls = assistant_parts(record, path, line)
        if prompt is None or not in_window:
            continue
        if message_id not in seen_responses:
            seen_responses.add(message_id)
            if record["message"].get("model") != "<synthetic>":
                context = sum(tokens[key] for key in CONTEXT_KEYS)
                session.contexts.append(context)
                prompt.peak_context = max(prompt.peak_context, context)
        for call_id, name in calls:
            if call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            prompt.tools[name] += 1
            if name in AGENT_TOOLS:
                close_run()
            elif name in READ_TOOLS:
                run += 1
    close_run()
    session.subagents = read_subagents(path.with_suffix(""), since, until)
    return session


def read_subagents(directory: Path, since: date | None, until: date | None) -> list[dict]:
    """Sum each subagent transcript's tokens, deduped on message.id (last record wins)."""
    found = []
    for path in sorted((directory / "subagents").glob("agent-*.jsonl")):
        usage: dict[str, dict[str, int]] = {}
        first: datetime | None = None
        for line, record in records(path):
            stamp = parse_timestamp(record.get("timestamp"), path, line)
            first = first or stamp
            if record["type"] == "assistant":
                message_id, tokens, _ = assistant_parts(record, path, line)
                usage[message_id] = tokens
        if first is None or not in_range(first, since, until):
            continue
        meta_path = path.with_suffix(".meta.json")
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raise FormatError(meta_path, 1, "not valid JSON") from None
            if not isinstance(meta, dict):
                raise FormatError(meta_path, 1, "not a JSON object")
        totals = {key: sum(tokens[key] for tokens in usage.values()) for key in TOKEN_KEYS}
        found.append(
            {
                "agent_id": path.stem.removeprefix("agent-"),
                # Identifiers only; the meta file's `description` is prose and stays unread.
                "agent_type": meta.get("agentType"),
                "model": meta.get("model"),
                "responses": len(usage),
                "tokens": {**totals, "total": sum(totals.values())},
            }
        )
    return found


def summarize(session_id: str, sessions: list[Session]) -> dict[str, Any]:
    prompts = [p for s in sessions for p in s.prompts if p.tools]
    contexts = [c for s in sessions for c in s.contexts]
    runs = [r for s in sessions for r in s.runs]
    subagents = [a for s in sessions for a in s.subagents]
    stamps = [p.timestamp for s in sessions for p in s.prompts]
    with_agent = sum(p.agent for p in prompts)
    return {
        "session": session_id,
        "first_prompt": min(stamps).isoformat() if stamps else None,
        "last_prompt": max(stamps).isoformat() if stamps else None,
        "prompts_with_tool_calls": len(prompts),
        "prompts_with_agent_call": with_agent,
        "agent_share": round(with_agent / len(prompts), 4) if prompts else None,
        "longest_read_run": max(runs, default=0),
        "runs_of_10_plus_reads": sum(r >= LONG_RUN for r in runs),
        "main_context_median": int(statistics.median(contexts)) if contexts else None,
        "main_context_peak": max(contexts, default=None),
        "main_api_calls": len(contexts),
        "tool_calls": dict(sum((p.tools for p in prompts), Counter()).most_common()),
        "subagent_count": len(subagents),
        "subagent_tokens": {
            key: sum(a["tokens"][key] for a in subagents) for key in (*TOKEN_KEYS, "total")
        },
    }


def per_prompt(prompt: Prompt) -> dict[str, Any]:
    return {
        "timestamp": prompt.timestamp.isoformat(),
        "tool_calls": dict(prompt.tools.most_common()),
        "agent_call": prompt.agent,
        "longest_read_run": prompt.longest_read_run,
        "peak_context": prompt.peak_context,
    }


def kilo(value: int | None) -> str:
    return "-" if value is None else f"{round(value / 1000)}k"


def big(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return kilo(value)


def table(rows: list[dict[str, Any]]) -> str:
    header = (
        "session",
        "dates",
        "prompts",
        "with Agent",
        "longest run",
        "runs 10+",
        "ctx median/peak",
        "subagents",
        "subagent tokens",
    )
    lines = [header]
    for row in rows:
        share = row["agent_share"]
        dates = "-"
        if row["first_prompt"]:
            dates = f"{row['first_prompt'][5:10]} to {row['last_prompt'][5:10]}"
        lines.append(
            (
                row["session"][:8],
                dates,
                str(row["prompts_with_tool_calls"]),
                f"{row['prompts_with_agent_call']} ({'-' if share is None else f'{share:.0%}'})",
                str(row["longest_read_run"]),
                str(row["runs_of_10_plus_reads"]),
                f"{kilo(row['main_context_median'])} / {kilo(row['main_context_peak'])}",
                str(row["subagent_count"]),
                big(row["subagent_tokens"]["total"]),
            )
        )
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in lines
    )


def resolve_project(argument: str, root: Path) -> Path:
    candidate = Path(argument).expanduser()
    if candidate.is_dir() and candidate.resolve().parent == root.resolve():
        return candidate
    if "/" not in argument and (root / argument).is_dir():
        return root / argument
    mapped = root / re.sub(r"[^A-Za-z0-9]", "-", str(candidate.resolve()))
    if mapped.is_dir():
        return mapped
    raise SystemExit(f"delegation-stats: no project directory for {argument} (looked for {mapped})")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="delegation-stats",
        description="Delegation metrics from Claude Code session transcripts (numbers only).",
    )
    parser.add_argument("project", help="project dir or name under the projects root, or a cwd")
    parser.add_argument("--since", type=parse_date, help="first day, UTC, inclusive")
    parser.add_argument("--until", type=parse_date, help="last day, UTC, inclusive")
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        metavar="ID",
        help="only sessions whose id starts with this (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="JSON with per-prompt metrics")
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="default: ~/.claude/projects",
    )
    args = parser.parse_args(argv)
    project = resolve_project(args.project, args.projects_root)

    sessions = []
    try:
        for path in sorted(project.glob("*.jsonl")):
            if args.session and not path.stem.startswith(tuple(args.session)):
                continue
            session = read_session(path, args.since, args.until)
            if session.prompts:
                sessions.append(session)
    except FormatError as error:
        print(f"delegation-stats: unrecognized transcript shape: {error}", file=sys.stderr)
        return 2

    rows = [summarize(s.session_id, [s]) for s in sessions]
    total = summarize("all", sessions) if len(sessions) > 1 else None
    if args.json:
        output = {
            "project": project.name,
            "since": args.since.isoformat() if args.since else None,
            "until": args.until.isoformat() if args.until else None,
            "sessions": [
                {**row, "prompts": [per_prompt(p) for p in s.prompts if p.tools]}
                for row, s in zip(rows, sessions)
            ],
            "subagents": {s.session_id: s.subagents for s in sessions},
            "total": total,
        }
        json.dump(output, sys.stdout, indent=2)
        print()
    elif not rows:
        print("no sessions with prompts in range")
    else:
        print(table(rows + ([total] if total else [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
