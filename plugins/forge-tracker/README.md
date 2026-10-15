# forge-tracker

Keeps durable work on the issue tracker of the repo it's about, and keeps that tracker honest
enough to plan from. The design is [docs/specs/forge-tracker.md](../../docs/specs/forge-tracker.md);
this release is its first phase: a read-only forge layer for GitHub and `/catchup`. The working
rules, `/next`, `/groom`, and the GitLab and Forgejo adapters come in later phases.

## Requirements

- `python3` 3.9 or newer (standard library only) and `git`.
- `gh`, logged in to the repo's host (`gh auth login`). forge-tracker reads the token with
  `gh auth token` and sends it only in the `Authorization` header of its own requests.

## /catchup

For coming back to a project: what changed and what's waiting on you, since your last
successful `/catchup` (seven days the first time). It's read-only.

```text
/catchup                 since your last catchup; moves the marker when every read succeeded
/catchup --since 3d      an ad hoc look back (3d, 12h, 2w, 90m or an ISO time); marker untouched
/catchup --no-mark       read the marker but don't move it (agents always pass this)
/catchup --all           list the standing backlog in full instead of as counts
/catchup --json          the same data as JSON
```

The report has five sections, in order: **Waiting on you** (mentions, review requests and
assignments, and issues carrying the needs-human or decision label; the standing backlog is one
count line), **Merged since** (grouped by the issues each PR closes or names), **Decisions
made** (comments matching your ruling pattern), **Board movement** (issues opened and closed by
tier) and **Health** (the default branch's newest push and scheduled runs, jobs that passed only
on retry more than once, and the tags you list).

Every read fails on its own. A failed read prints `<part>: FAILED (<reason>)` where its section
would be, the command exits 1, and the marker doesn't move, so the next catchup covers the same
window again. A pull request whose mergeability GitHub hasn't computed yet shows `conflicts:
unknown`, never clean.

The CLI behind the command runs on its own too, which is how agents use it:

```bash
plugins/forge-tracker/scripts/forge-tracker catchup --no-mark
plugins/forge-tracker/scripts/forge-tracker --repo ~/src/widgets catchup --since 2d --json
plugins/forge-tracker/scripts/forge-tracker smoke     # every read once, counts and failures
```

### The marker

The marker is
`${XDG_CACHE_HOME:-~/.cache}/forge-tracker/catchup/<host>_<project>_<user>.json` (the project
path with `/` as `__`; a relative `XDG_CACHE_HOME` is ignored). Directories are 0700 and the
file 0600. The new marker is written to a temp file before the report prints and renamed into
place after; if printing fails, the temp file is deleted and the old marker stays. The next
window opens five minutes before the marker's time, so a fast local clock can't skip events.
Deleting the file is safe: the next catchup covers seven days.

## Configuration

Per repo, in `.claude/forge-tracker.local.md`. Only the YAML frontmatter is read, every key is
optional, and an unknown key fails the load with its name. A report that needs an unset key says
so and leaves that part out. The keys `/catchup` uses:

```markdown
---
forge: github                 # only for a GitHub Enterprise host; github.com is detected
api_base: https://git.corp.example/api/v3   # only when the host's API isn't at /api/v3
labels:
  needs_human: needs-human
  decision: decision
tiers:                        # first match wins; milestone prefixes are the fallback
  labels: [P0, P1, P2, P3]
  milestone_prefixes: ["Tier 0", "Tier 1"]
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
changelog: CHANGELOG.md       # bullet lines added on the default branch in the window
report_tags: ["v*"]           # globs over release and tag names
---
Free notes for the assistant.
```

The file also accepts the keys later phases read (`ca_file`, `labels.decision_made`,
`labels.blocked`, `labels.agent`, `person_only_markers`, `blocking`, `stale_days`,
`write_wrapper`); the design doc describes them. The standard library has no YAML parser, so
the frontmatter is a strict subset: `key: value`, one level of nesting, `[a, b]` and `- item`
lists, quoted strings, integers and `#` comments.

## Reads can't write

Every request goes through one guard in `scripts/forge_tracker/transport.py` before it's sent.
GET is allowed. A POST is allowed only to the GraphQL endpoint, and only when every operation in
the document is a query; the guard finds each operation's type by tokenizing the document
(strings and comments skipped), so a `mutation` hidden after a query, or in a document it can't
parse, is refused. Lists are read to the last page or fail: a page cap that is hit is a failed
read with the cap in the reason, never a short list. Error text is cut to 300 characters with
anything token-shaped replaced by `***`.

## Tests

```bash
uv run --no-project --with pytest pytest plugins/forge-tracker/tests
```

No test touches the network. `tests/fixtures/github/` holds responses recorded from a live
repo with `--record <file>` (scrubbed of tokens and thinned of fields no adapter reads) and the
report they produce; the edge cases a live repo rarely shows at once (conflicts, retries, bots,
failures, page caps) are built in the tests. The GitHub fixtures will move to a dedicated seeded
repo, and the docker-compose stack of GitLab CE and Forgejo that the GitLab and Forgejo phases
record from will live in `tests/live/`.
