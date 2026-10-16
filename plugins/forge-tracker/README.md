# forge-tracker

Keeps durable work on the issue tracker of the repo it's about, and keeps that tracker honest
enough to plan from. The design is [docs/specs/forge-tracker.md](../../docs/specs/forge-tracker.md);
this release has a read-only forge layer for GitHub, `/catchup` and `/next`. The working rules,
`/groom`, and the GitLab and Forgejo adapters come in later phases.

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

## /next

What to work on: your queue, the lanes agents can start now, and what's blocked. It recommends
only; it never claims, assigns, comments or branches.

```text
/next                    the saved slate while the facts hold, or the facts and a request to rank
/next --rerank           rank again even though the facts haven't moved
/next --refresh          re-read the facts even though the fingerprint hasn't moved
/next --json             the same data as JSON
```

It works in two layers. The **facts** are read by the CLI: open issues with tier, due date,
labels, owners and blocking links; open PRs with their conflict and CI state; branches with no
PR; and the closing references (`Closes #N`) that tie a PR to an issue. They're cached with a
fingerprint of cheap reads (the default branch head, open issue and PR counts, the newest issue
and PR update times, the milestones, every branch head, each open PR's head commit and its
combined check state, a hash of the config file and a hash of forge-tracker's own code). A CI run
in progress leaves the fingerprint alone; a new push or a changed check result moves it. While the
fingerprint holds, a rerun reuses the facts and reads only the fingerprint. A run with a failed read is written for inspection but never reused.

The CLI classifies every open issue, deterministically:

- **Person-only:** the needs-human or decision label, or a `person_only_markers` word in the
  title or labels. A label is firm; a marker is a hint Claude may overrule with a stated reason.
  An assignee is never a reason: agents share your account, so assignees show as owners.
- **Blocked:** an open blocker by native link, or the blocked label. A link to a closed blocker
  doesn't block; it's flagged for `/groom`, and so is a blocked label with nothing linked. A
  person-only item that's blocked is listed under Blocked.
- **Agent-ready:** open, not person-only and not blocked. The agent label isn't required; when
  it's configured, labelled items sort first within their tier.

Each item also gets how many items it unblocks, counted transitively through open links (each
item once, so a cycle can't loop; cycles are flagged), what has it **in flight** (an open PR that
closes it or names it in its title or branch, or a pushed branch that names its number; versions
and dates in a branch name aren't issue numbers), and a **merged-but-open** flag when a merged PR
has a real closing reference to it.

The **slate** is Claude's ranking: the order of your queue, and the agent-ready items grouped
into lanes that shouldn't touch the same files. The grouping is Claude's judgement from titles,
bodies and labels, and the report says so. Claude saves it with `forge-tracker next save-slate`
(JSON on stdin), which checks it against the facts (every item open, the queue person-only, the
lanes agent-ready unless a marker hint is overruled with a reason) and stores it beside them. It's
shown, with its age, until the fingerprint moves.

```bash
plugins/forge-tracker/scripts/forge-tracker next                  # the report
plugins/forge-tracker/scripts/forge-tracker next facts --json     # facts with issue bodies, to rank
plugins/forge-tracker/scripts/forge-tracker next save-slate < slate.json
```

The cache is `${XDG_CACHE_HOME:-~/.cache}/forge-tracker/next/<host>_<project>/` (`facts.json`
and `slate.json`, 0600 in a 0700 directory). Deleting it is safe.

Every read fails on its own, and a failed read is never shown as empty: when the blocking links
can't be read, items that would otherwise be agent-ready are listed as not classified, because
they might be blocked. The command exits 1 when any read failed and shows no slate until a run
reads everything.

## Configuration

Per repo, in `.claude/forge-tracker.local.md`. Only the YAML frontmatter is read, every key is
optional, and an unknown key fails the load with its name. A report that needs an unset key says
so and leaves that part out. The keys `/catchup` and `/next` use:

```markdown
---
forge: github                 # only for a GitHub Enterprise host; github.com is detected
api_base: https://git.corp.example/api/v3   # only when the host's API isn't at /api/v3
labels:
  needs_human: needs-human     # person-only (firm)
  decision: decision           # person-only (firm)
  blocked: blocked             # /next: blocked even with nothing linked
  agent: agent-ready           # /next: sorts first within its tier; never required
tiers:                        # first match wins; milestone prefixes are the fallback
  labels: [P0, P1, P2, P3]
  milestone_prefixes: ["Tier 0", "Tier 1"]
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
changelog: CHANGELOG.md       # bullet lines added on the default branch in the window
report_tags: ["v*"]           # globs over release and tag names
person_only_markers: [hardware, legal]   # /next: words in a title or label that hint person-only
---
Free notes for the assistant.
```

The file also accepts the keys later phases read (`ca_file`, `labels.decision_made`,
`blocking`, `stale_days`, `write_wrapper`); the design doc describes them. The standard library has no YAML parser, so
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

No test touches the network. `tests/fixtures/github/` holds `/catchup` and `/next` runs recorded
from a live repo with `--record <file>` (scrubbed of tokens and thinned of fields no adapter reads) and the
report they produce; the edge cases a live repo rarely shows at once (conflicts, retries, bots,
failures, page caps) are built in the tests. The GitHub fixtures will move to a dedicated seeded
repo, and the docker-compose stack of GitLab CE and Forgejo that the GitLab and Forgejo phases
record from will live in `tests/live/`.
