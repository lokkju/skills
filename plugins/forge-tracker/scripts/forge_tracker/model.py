"""The normalized shapes every forge adapter returns, and Read: data or a named error."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")
U = TypeVar("U")


class ForgeError(Exception):
    """A read that failed. The message is already redacted and short enough to print."""


class ConfigError(Exception):
    """A config file or a detection step that can't go on; the message says what to fix."""


@dataclass(frozen=True)
class Read(Generic[T]):
    """One read's outcome: `data` when it succeeded, `error` (the reason) when it didn't.

    A failed read never stands in for an empty list; callers print `<part>: FAILED (<reason>)`.
    """

    data: Optional[T] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def map(self, fn: Callable[[T], U]) -> Read[U]:
        if self.error is not None:
            return Read(error=self.error)
        return Read(data=fn(self.data))  # type: ignore[arg-type]


def attempt(fn: Callable[[], T]) -> Read[T]:
    """Run one read and turn a ForgeError into Read(error=...)."""
    try:
        return Read(data=fn())
    except ForgeError as error:
        return Read(error=str(error))


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class User:
    login: str
    is_bot: bool


@dataclass(frozen=True)
class Milestone:
    title: str
    due: Optional[str] = None


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str  # open | closed
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    created: datetime
    updated: datetime
    closed: Optional[datetime] = None
    milestone: Optional[Milestone] = None
    author: Optional[str] = None
    url: str = ""
    body: str = ""


@dataclass(frozen=True)
class Comment:
    issue: int
    author: str
    author_is_bot: bool
    created: datetime
    body: str
    url: str = ""


@dataclass(frozen=True)
class Mention:
    number: int
    title: str
    is_change: bool
    updated: datetime
    url: str = ""


# Conflict state is three-valued: "yes", "no" or "unknown". Never a boolean.
CONFLICTS = ("yes", "no", "unknown")


@dataclass(frozen=True)
class Change:
    """A pull request or merge request."""

    number: int
    title: str
    branch: str
    draft: bool
    author: Optional[str]
    assignees: tuple[str, ...]
    reviewers: tuple[str, ...]
    conflicts: str
    pipeline: Optional[str]  # the head commit's combined CI state, lowercased, or None
    created: datetime
    updated: datetime
    url: str = ""
    merged_at: Optional[datetime] = None
    merge_sha: Optional[str] = None
    closing_issues: tuple[int, ...] = ()
    # The review decision, lowercased (`approved`, `changes_requested`, `review_required`), or
    # None when the forge has none or it wasn't read.
    review: Optional[str] = None


@dataclass(frozen=True)
class Job:
    name: str
    conclusion: Optional[str]
    attempt: int = 1


@dataclass(frozen=True)
class Run:
    id: int
    name: str
    event: str
    status: str
    conclusion: Optional[str]
    attempt: int
    created: datetime
    url: str = ""
    failed_jobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Flaky:
    job: str
    times: int


@dataclass(frozen=True)
class Tag:
    pattern: str
    name: Optional[str]
    date: Optional[datetime] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    has_change: bool
    ahead_by: Optional[int] = None


@dataclass(frozen=True)
class Link:
    blocked: int
    blocker: int
    blocker_open: bool
    kind: str  # native | convention
    blocker_project: Optional[str] = None


@dataclass(frozen=True)
class ClosingRef:
    """A change whose body or commits carry a real closing reference (`Closes #N`) to an issue,
    whatever the change's state. `state` is open, merged or closed."""

    issue: int
    change: int
    state: str
    branch: str
    base: str
    merged_at: Optional[datetime] = None
    change_project: Optional[str] = None  # None when the change is in the issue's project


@dataclass
class Repo:
    """What every report needs to know about the project before it reads anything else."""

    default_branch: str
    extra: dict[str, Any] = field(default_factory=dict)
