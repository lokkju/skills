# forge-tracker

Keeps durable work on the issue tracker of the repo it's about, and keeps that tracker honest
enough to plan from. The design is [docs/specs/forge-tracker.md](../../docs/specs/forge-tracker.md);
this release has a read-only forge layer for GitHub, `/catchup`, `/next` and `/groom`, plus the
write operations `/groom` applies. The working rules and the GitLab and Forgejo adapters come in
later phases.

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
/catchup --workspace DIR every repo in a folder of checkouts, in one report
```

The report has five sections, in order: **Waiting on you** (mentions, review requests and
assignments, PRs that are yours to merge, and issues carrying the needs-human or decision label;
the standing backlog is one count line), **Merged since** (grouped by the issues each PR closes or names), **Decisions
made** (comments matching your ruling pattern), **Board movement** (issues opened and closed by
tier) and **Health** (the default branch's newest push and scheduled runs, jobs that passed only
on retry more than once, and the tags you list).

Every read fails on its own. A failed read prints `<part>: FAILED (<reason>)` where its section
would be, the command exits 1, and the marker doesn't move, so the next catchup covers the same
window again. A pull request whose mergeability GitHub hasn't computed yet shows `conflicts:
unknown`, never clean.

**Yours to merge** is a PR you opened that waits only for you: not a draft, no review requested
of anyone, no changes requested, no known conflict, and checks that passed (or none). On a solo
repo nobody asks you to review your own PR, so this is where those show up. Agents working on
your account open PRs as you, so theirs count too.

### A folder of repos

Started in a folder that isn't a git repo but holds several (say `~/projects/acme/`, with a
checkout per repo), `/catchup` reads every child repo that has an `origin` remote and prints one
report. **Waiting on you** comes first and covers them all, each item as `owner/repo#N`; then
each repo's own sections. A repo where nothing happened is one line (`acme/exemplar: nothing
since 2026-10-10 09:00; main push: passed`), and a `setup:` line the repos share is said once,
with the repos it applies to.

Each repo keeps its own config, window and marker. If one repo's read fails, that repo's marker
stays, the others move, and the command exits 1; a repo that can't be read at all (an
unsupported forge, a bad config file) is one `FAILED` line. To pick the repos yourself, list them
in the folder's own `.claude/forge-tracker.local.md`:

```markdown
---
repos: [citare, stare, infra]   # paths relative to the folder; the only key read there
---
```

`--workspace DIR` reads a folder from anywhere. The other commands read one repo; in a folder of
repos they name the repos they found and ask for `--repo <dir>`.

The CLI behind the command runs on its own too, which is how agents use it:

```bash
plugins/forge-tracker/scripts/forge-tracker catchup --no-mark
plugins/forge-tracker/scripts/forge-tracker --repo ~/src/widgets catchup --since 2d --json
plugins/forge-tracker/scripts/forge-tracker catchup --workspace ~/src --no-mark
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
- **Unclassified:** when the repo configures neither the needs-human nor the decision label,
  nothing could have marked an item person-only, so an item that's neither blocked nor marked by
  a marker word isn't called agent-ready. It's listed under Not classified, the report's second
  line says how many there are, and the slate places each one.

Each item also gets how many items it unblocks, counted transitively through open links (each
item once, so a cycle can't loop; cycles are flagged), what has it **in flight** (an open PR that
closes it or names it in its title or branch, or a pushed branch that names its number; versions
and dates in a branch name aren't issue numbers), and a **merged-but-open** flag when a merged PR
has a real closing reference to it. Open PRs that no open issue has in flight get their own list,
with their draft, conflict, CI and review state.

The **slate** is Claude's ranking: the order of your queue, and the agent-ready items grouped
into lanes that shouldn't touch the same files. The grouping is Claude's judgement from titles,
bodies and labels, and the report says so. Claude saves it with `forge-tracker next save-slate`
(JSON on stdin), which checks it against the facts (every item open, the queue person-only, the
lanes agent-ready unless a marker hint is overruled with a reason) and stores it beside them. It's
shown, with its age, until the fingerprint moves.

On a repo without the labels, Claude reads each unclassified item's title and body. One whose
text says you have to act (physical work, a device or test window, research waiting on your
decision, an external event, a parked item) goes in your queue through the slate's
`person_only` map, with the reason, and the report shows it as `judged person-only: <reason>`.
One an agent can do goes in a lane. One Claude can't call is listed as not placed. save-slate
refuses a `person_only` entry for any item a label already classified.

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

## /groom

Finds where the tracker has drifted from the truth and proposes the fixes. The report writes
nothing; Claude shows you the proposals, numbered with their evidence, asks which to apply, and
applies only the ones you name.

```text
/groom                   the report, then "Apply which? (numbers, all, none)"
/groom --json            the same data as JSON, each proposal with its write payload
```

| Check | Proposes | Never proposes, puts in Unclear instead |
| --- | --- | --- |
| Merged but still open | `close`, with a comment naming the PR, when a merged PR has a real closing reference (`Closes #N`) and its merge commit is on the default branch | an issue a merged PR names only in its title or branch; a closing reference from a PR merged into another branch, or whose merge commit isn't on the default branch |
| Tier label and milestone (tier label N pairs with milestone prefix N) | `labels` to add the missing tier label; `milestone` when exactly one open milestone fits | a label and milestone that disagree; no single milestone that fits |
| Blocked label with nothing linked | `unlink` the label | |
| Link to a closed blocker | `unlink` the link | |
| Decided but still waiting: a ruling comment (by `ruling_pattern`) dated after the needs-human or decision label was added | `labels`: remove those, add `decision_made` | an issue whose label history can't be read |
| Stale branch: no open PR, nothing ahead of the default branch | `delete-branch` (refused at apply time if the branch moved) | a branch with commits the default branch lacks, when idle for `stale_days` or the branch of a merged PR |
| Conflicted or stale PR | nothing; listed for you | `conflicts: unknown` |
| Likely duplicates (token-set similarity of titles and labels, open issues and those closed in 30 days) | nothing | every pair |
| Placeholder due dates (one due date on more than one open milestone) | nothing; listed for you | |

Merged PRs are read back 90 days. `stale_days` defaults to 14. The stale-branch check never
looks at the default branch, protected branches, or the branches release and dependency bots
create and reuse (`yeet/*`, `release-please--*`, `changeset-release/*`, `dependabot/*`,
`renovate/*`); `groom.keep_branches` adds globs of your own to that list. A check whose config is unset
(the tier, blocked-label and ruling checks) is listed as skipped, with the key it needs. Every
check fails on its own: a failed read prints `<check>: FAILED (<reason>)` and the command exits 1,
and nothing about that check is shown as clean.

`forge-tracker groom apply` reads `{"approved": ["close #14", "delete-branch sd-26"]}` (the
proposal ids) on stdin, re-runs every check, and refuses the whole list, writing nothing, when any
id isn't a proposal any more. Then it writes the approved ones one at a time and prints a line for
each: what it wrote, `REFUSED (...)` or `FAILED (...)`.

```bash
plugins/forge-tracker/scripts/forge-tracker groom
echo '{"approved": ["close #14"]}' | plugins/forge-tracker/scripts/forge-tracker groom apply
```

### Writes

Every write goes through `scripts/forge_tracker/writes.py`, the only module with a write
transport (POST, PATCH and DELETE under the repo's own REST path; nothing else). The operations
are named, and each takes a JSON payload:

| Operation | Payload | On GitHub |
| --- | --- | --- |
| `close` | `issue`, `comment`, `change` | the comment, then close as completed; refused if it's already closed |
| `labels` | `issue`, `add`, `remove` | add, then remove each; a label already off is noted |
| `milestone` | `issue`, `milestone` (`{number, title}` or `null`) | set or clear |
| `unlink` | `issue` and either `label` or `blocker` (+ `blocker_project`) | remove the label, or the blocked-by link; refused if the blocker reopened |
| `delete-branch` | `branch`, `sha` | delete the ref; refused if the branch moved off `sha` or is protected |

`forge-tracker write <op>` runs one with its payload on stdin. When the config sets
`write_wrapper:`, forge-tracker sends no write of its own: it runs `<wrapper> <op>` in the repo
root with the same JSON on stdin (plus `repo`), and the wrapper does the write and exits 0, or
exits non-zero to refuse; its stderr is shown. Every operation, written, refused or failed, is
appended to `${XDG_CACHE_HOME:-~/.cache}/forge-tracker/writes/<host>_<project>.jsonl` (0600).

## Configuration

Per repo, in `.claude/forge-tracker.local.md`. Only the YAML frontmatter is read, every key is
optional, and an unknown key fails the load with its name. A report that needs an unset key
leaves that part out and says so in one `setup:` line, which names what's missing and how to set
it up. Set a label or `ruling_pattern` to `~` to record that the repo has none; the reports then
say nothing about it. The keys the commands use:

```markdown
---
forge: github                 # only for a GitHub Enterprise host; git config tracker.forge wins
api_base: https://git.corp.example/api/v3   # only when the host's API isn't at /api/v3
labels:
  needs_human: needs-human     # person-only (firm)
  decision: decision           # person-only (firm)
  decision_made: decided       # /groom: added when a ruling settles a waiting issue
  blocked: blocked             # /next: blocked even with nothing linked
  agent: agent-ready           # /next: sorts first within its tier; never required
tiers:                        # first match wins; milestone prefixes are the fallback
  labels: [P0, P1, P2, P3]
  milestone_prefixes: ["Tier 0", "Tier 1"]
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
changelog: CHANGELOG.md       # bullet lines added on the default branch in the window
report_tags: ["v*"]           # globs over release and tag names
person_only_markers: [hardware, legal]   # /next: words in a title or label that hint person-only
stale_days: 14                # /groom: when a PR or an unmerged branch counts as stale
write_wrapper: ./scripts/tracker-write   # every write runs `<wrapper> <op>` instead
groom:
  keep_branches: ["release/*", "gh-pages"]   # /groom: never propose deleting these (globs)
---
Free notes for the assistant.
```

The file also accepts the keys later phases read (`ca_file`, `blocking`); the design doc
describes them.

### Per clone: `tracker.web` and `tracker.forge`

forge-tracker finds the repo from its `origin` remote. When that remote is an SSH alias, a mirror
or a push-only URL, tell this one clone where the repo really is with git config. These are the
keys session-decisions reads for its links, so one setting fixes both:

```bash
git config tracker.web https://github.com/acme/widgets   # replaces the remote's host and path
git config tracker.forge github                          # github, gitlab, forgejo or gitea
```

`tracker.forge` wins over `forge:` in the config file, and both win over the built-in host rules
(`github.com`, `gitlab.com`). Put `forge:` in the file when it holds for every clone, which matters
when the file is committed; use `tracker.forge` for what is true only of this clone. `api_base:`
and `ca_file:` still come only from the config file, since git config holds the web address and
not the API's. A value forge-tracker can't use stops the command with the key named. The standard library has no YAML parser, so the frontmatter is a strict subset: `key: value`, one level of nesting, `[a, b]` and `- item`
lists, quoted strings, integers, `~` or `null` for none, and `#` comments.

### Setting it up

When a repo has no config file, or one that leaves labels unset, `/catchup` and `/next` show the
`setup:` line and Claude offers to set it up, once per repo. It runs `forge-tracker labels
suggest`, which reads the repo's existing labels and prints a proposed file: each role
mapped to the label that looks like it (`needs-human`, `human`, `decision`, `question`,
`blocked`, `agent-ready` and their variants), the priority labels as tiers (`P0` to `P3`,
`priority: high`, `priority:1`), and `~` for any role no label matched. It writes nothing. Claude
shows you the proposal and writes `.claude/forge-tracker.local.md` only when you say so, with
your corrections. That file is the only thing it writes; it doesn't commit it.

For each role no label matched, the suggestion also prints a `gh label create` command.
Creating a label isn't one of forge-tracker's write operations yet, so Claude gives you those
commands to run and doesn't create labels itself; once you have, it puts their names in the file.

On a no, Claude runs `forge-tracker labels decline`, which records the decline in
`${XDG_CACHE_HOME:-~/.cache}/forge-tracker/setup/<host>_<project>.json` (never in the repo or on
the forge). The `setup:` line then says setup was declined, and Claude doesn't offer it again.
Deleting the file brings the offer back.

```bash
plugins/forge-tracker/scripts/forge-tracker labels suggest          # the proposal, as text
plugins/forge-tracker/scripts/forge-tracker labels suggest --json   # the same, for scripts
plugins/forge-tracker/scripts/forge-tracker labels decline          # stop offering setup here
```

## Reads can't write

Every request goes through one guard in `scripts/forge_tracker/transport.py` before it's sent.
GET is allowed. A POST is allowed only to the GraphQL endpoint, and only when every operation in
the document is a query; the guard finds each operation's type by tokenizing the document
(strings and comments skipped), so a `mutation` hidden after a query, or in a document it can't
parse, is refused. Lists are read to the last page or fail: a page cap that is hit is a failed
read with the cap in the reason, never a short list. Error text is cut to 300 characters with
anything token-shaped replaced by `***`. Writes have their own transport in `writes.py`, which
no adapter or report imports (see [Writes](#writes)).

## Tests

```bash
uv run --no-project --with pytest pytest plugins/forge-tracker/tests
```

No test touches the network, and no test writes to a forge: writes go to a fake transport or
a fake wrapper. `tests/fixtures/github/` holds `/catchup`, `/next` and `/groom` runs recorded
from a live repo with `--record <file>` (scrubbed of tokens and thinned of fields no adapter reads) and the
report they produce; the edge cases a live repo rarely shows at once (conflicts, retries, bots,
failures, page caps) are built in the tests. The GitHub fixtures will move to a dedicated seeded
repo, and the docker-compose stack of GitLab CE and Forgejo that the GitLab and Forgejo phases
record from will live in `tests/live/`.
