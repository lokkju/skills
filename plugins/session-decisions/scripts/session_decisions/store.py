"""Read the session task list, and keep the plugin's own per-session state.

The state file holds the label counters, when each label was assigned, and the fallback
ledger used where the task tools are unavailable. It is only changed under a file lock.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .subject import LETTER, Draft, parse_draft, parse_label

try:
    import fcntl
except ImportError:  # Windows: no advisory locks, accept the race
    fcntl = None

OPEN_STATUSES = {"pending", "in_progress"}
_EMPTY = {"counters": {"D": 0, "A": 0}, "created": {}, "ledger": []}


@dataclass
class Item:
    label: str
    kind: str
    body: str
    status: str
    source: str  # "tasks" or "ledger"
    description: str = ""
    created: str | None = None
    ruling: str | None = None

    @property
    def letter(self) -> str:
        return self.label[0]

    @property
    def number(self) -> int:
        """0 for an item the hook didn't get to label ("D?")."""
        digits = self.label[1:]
        return int(digits) if digits.isdigit() else 0

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def subject(self) -> str:
        return f"{self.label} {self.kind}: {self.body}"


def _as_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ruling_in(description: str) -> str | None:
    for line in reversed(description.splitlines()):
        if line.startswith("Ruling ("):
            return line
    return None


def read_task_items(tasks_dir: Path) -> list[Item]:
    """Decision items in the session task list. Anything unreadable is skipped."""
    items = []
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        subject = str(data.get("subject", ""))
        label = parse_label(subject)
        if label is not None:
            text, kind, body = label.text, label.kind, label.body
        else:
            draft = parse_draft(subject)  # created while the hook was failing: unlabelled
            if draft is None:
                continue
            text, kind, body = LETTER[draft.kind] + "?", draft.kind, draft.body
        description = str(data.get("description", ""))
        items.append(Item(label=text, kind=kind, body=body,
                          status=str(data.get("status", "pending")), source="tasks",
                          description=description, ruling=_ruling_in(description)))
    return items


class SessionState:
    def __init__(self, data_dir: Path, sid: str):
        self.path = data_dir / "sessions" / f"{sid}.json"
        self._lock_path = self.path.with_suffix(".lock")

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            return copy.deepcopy(_EMPTY)
        counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
        data["counters"] = {letter: _as_int(counters.get(letter)) for letter in ("D", "A")}
        if not isinstance(data.get("created"), dict):
            data["created"] = {}
        if not isinstance(data.get("ledger"), list):
            data["ledger"] = []
        return data

    @contextmanager
    def locked(self) -> Iterator[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            yield state
            self._save(state)

    def _save(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1)
        os.replace(tmp, self.path)


def assign_label(state: dict, kind: str, existing: list[Item]) -> str:
    """Next label for this kind: one past both the counter and any label already in use."""
    letter = LETTER[kind]
    in_use = [item.number for item in existing if item.letter == letter]
    number = max([int(state["counters"].get(letter, 0)), *in_use]) + 1
    state["counters"][letter] = number
    label = f"{letter}{number}"
    state["created"][label] = now_iso()
    return label


def ledger_items(state: dict) -> list[Item]:
    items = []
    for entry in state.get("ledger", []):
        try:
            items.append(Item(label=entry["label"], kind=entry["kind"], body=entry["body"],
                              status=entry.get("status", "pending"), source="ledger",
                              description=entry.get("description", ""),
                              created=entry.get("created"), ruling=entry.get("ruling")))
        except (KeyError, TypeError):
            continue
    return items


def ledger_add(state: dict, draft: Draft, description: str, existing: list[Item]) -> Item:
    label = assign_label(state, draft.kind, existing)
    entry = {"label": label, "kind": draft.kind, "body": draft.body, "description": description,
             "status": "pending", "created": state["created"][label]}
    state["ledger"].append(entry)
    return ledger_items({"ledger": [entry]})[0]


def ledger_close(state: dict, label: str, ruling: str) -> bool:
    for entry in state.get("ledger", []):
        if entry.get("label") == label:
            entry["status"] = "completed"
            entry["ruling"] = f"Ruling ({today()}): {ruling}"
            return True
    return False


def all_items(tasks_dir: Path, state: dict) -> list[Item]:
    items = read_task_items(tasks_dir) + ledger_items(state)
    created = state.get("created", {})
    for item in items:
        if item.created is None:
            item.created = created.get(item.label)
    return sorted(items, key=lambda item: (item.created or "", item.letter, item.number))
