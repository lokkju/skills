# forge-tracker: design

_Status: decided, 2026-10-15. Covers [#12](https://github.com/lokkju/skills/issues/12) (the
plugin and its fixed constraints), [#21](https://github.com/lokkju/skills/issues/21) (the
read-only forge layer), [#22](https://github.com/lokkju/skills/issues/22) (`/groom`),
[#23](https://github.com/lokkju/skills/issues/23) (`/catchup`) and
[#24](https://github.com/lokkju/skills/issues/24) (`/next`). The constraints come from the
forge-tracker section of the 2026-10-01 session-decisions and forge-tracker spec in the private
skills-marketplace repo; they're restated here so this file stands on its own. Every design
question is settled; the rulings are in [Decisions (2026-10-15)](#decisions-2026-10-15)._

## Purpose

forge-tracker keeps durable work on the issue tracker of the repo it's about, and keeps that
tracker honest enough to plan from. It does two kinds of thing:

- **Working rules** the assistant follows during any session: file discovered work on the right
  tracker, record rulings one way, fetch and re-read before acting on an issue, and put
  `Closes #N` in a PR or MR whose work was picked up from an issue.
- **Three commands** built on one read-only forge layer that works the same on GitHub, GitLab and
  Forgejo: `/catchup` (what changed and what waits on you), `/next` (what to work on, and what
  agents can start now) and `/groom` (find tracker drift and fix it when you approve).

It's written to be shared: no personal repos, usernames, hosts or paths. Anything personal is
plugin `userConfig` or the per-repo config file.

## The prototype

#24's comment reports a measurement "while using the prototype". That prototype isn't reused:
its lessons (the four-minute window, the page cap that rendered as "none", `merge_status:
unchecked`) are already in #23, #24 and this design, which takes them as given. The local test
stack under [Testing](#testing) replaces it as the place GitLab and Forgejo behaviour is measured.

## Layout

```
plugins/forge-tracker/
  .claude-plugin/plugin.json      userConfig: crossProjectTracker
  scripts/forge-tracker           entry point (python3, standard library only)
  scripts/forge_tracker/
    detect.py                     origin URL -> forge, host, project
    config.py                     .claude/forge-tracker.local.md frontmatter, strict
    transport.py                  read transport (GET only) and the separate write transport
    forges/github.py gitlab.py forgejo.py    one adapter each, same normalized shape
    model.py                      Issue, Change, Pipeline, Branch, Release, Read[T] (data or named error)
    catchup.py next.py groom.py   the three reports
    state.py                      XDG cache files: 0700 dirs, 0600 files, atomic replace
    writes.py                     the named write operations and the write-wrapper
  commands/catchup.md next.md groom.md
  skills/forge-tracker/SKILL.md   the working rules
  hooks/hooks.json, hooks/register.ts     SessionStart list and the closing-reference check
  tests/                          pytest over recorded fixtures; claude plugin test for the module
```

The forge layer and the three reports are a Python CLI, like forge-release's
`github-app-create`. The reasons are specific:

- Agents need to run `/catchup --no-mark` and `/next` for their own purposes, and an agent can
  run a CLI through Bash but can't invoke a slash command.
- Self-hosted GitLab and Forgejo with a pinned CA need an `ssl` context with a CA file. The hooks
  module's `$.http.fetch` takes no CA option, while `urllib` with `ssl.create_default_context
  (cafile=...)` does, and `glab` reads its own `ca_cert`.
- #23 wants the marker written 0600 with an atomic replace. `$.fs.write` has neither a mode nor a
  rename.
- forge-release already set the pattern: standard-library Python, an injectable `Http` callable,
  pytest with no network.

The function-hooks module stays small. It owns what has to be a hook: the SessionStart list and
the closing-reference check on PR or MR creation. It reaches the forge only by running the CLI
with `$.process.run`. The commands are markdown files that run the CLI and hand its output to Claude
with instructions. The cost is two languages in one plugin and two test runners, which the
reasons above outweigh.

## The forge layer (#21)

### Detection

`git remote get-url origin` gives the host and project path. The forge is decided in this order:

1. `forge:` in `.claude/forge-tracker.local.md`, when set (`github`, `gitlab` or `forgejo`).
2. The host: `github.com` is GitHub and `gitlab.com` is GitLab.
3. A host that `glab` knows (`glab config get host --host <host>` succeeds) is GitLab.
4. An unauthenticated probe of `https://<host>/api/v1/version`. A JSON `version` field means
   Forgejo or Gitea. The result is cached per host for a day.
5. Otherwise the command stops and asks for `forge:` in the config file. It never guesses.

A GitLab project path keeps its subgroups (`group/sub/project`). A self-hosted instance under a
subpath (`https://code.example.org/gitlab/group/project`) needs `api_base:` in the config, since
the subpath can't be told apart from a group from the URL alone.

References are written the forge's way: `#N` for issues everywhere, `#N` for GitHub and Forgejo
pull requests, `!N` for GitLab merge requests, and `group/project#N` across projects.

### Transport and auth

| Forge | Reads through | Auth | CA |
| --- | --- | --- | --- |
| GitHub | `gh api` (REST), `gh api graphql` where REST needs one call per item | `gh auth` | system |
| GitLab | `glab api` (REST), `glab api graphql` for the same reason | `glab auth` | `glab`'s `ca_cert` for the host |
| Forgejo | `urllib` against `/api/v1` | `FORGEJO_TOKEN`, else the matching login in `tea`'s config | `ca_file:` in the config file |

Forgejo goes through `urllib` and not `tea` because `tea`'s output is meant for terminals and
covers only part of what's needed; it does no harm to borrow its token.

### What it reads

Every adapter returns the same shapes (`model.py`). Each read returns either data or a named
error, never an empty list standing in for a failure.

| Read | Fields | GitHub | GitLab | Forgejo |
| --- | --- | --- | --- | --- |
| Identity | login, is_bot | `GET /user` (`type == "Bot"`, `[bot]` suffix) | `GET /user` (`bot`) | `GET /user` |
| Issues open, or updated since T | number, title, labels, milestone (title, due), assignees, created, updated, closed | `GET /issues?state=all&since=` (PRs filtered out) | `GET /issues?updated_after=` | `GET /issues?type=issues&since=` |
| Comments since T | issue, author, created, body | `GET /issues/comments?since=` (repo-wide) | project events with `target_type=note` | `GET /issues/comments?since=` |
| Mentions of you since T | issue or change, author, time | search `mentions:<login>` | `GET /todos?action=mentioned` | search `mentioned=true` (verify) |
| Blocking links | blocked, blocker, kind (native or convention) | GraphQL `blockedBy`, `subIssues` | `GET /issues/:iid/links` (`blocks` on tiers that have it) | `GET /issues/{i}/dependencies` |
| Open changes | number, title, source branch, draft, reviewers, assignees, conflicts, head pipeline | GraphQL `pullRequests` (`mergeable` is missing from the REST list) | `GET /merge_requests?state=opened`, then one GET per MR for `head_pipeline` | `GET /pulls?state=open` |
| Merged since T | number, title, branch, merge sha, merged at, closing issues | GraphQL `closingIssuesReferences` | `GET /merge_requests?state=merged&updated_after=`, `closes_issues` | `GET /pulls?state=closed` filtered on `merged` |
| CI | newest default-branch run by trigger (push, schedule), failed jobs, jobs that passed only on retry | `GET /actions/runs?branch=&event=`, jobs with `filter=all` | `GET /pipelines?ref=&source=`, jobs with `include_retried=true` | Actions runs where the instance has the API, else commit status |
| Branches | name, head sha, has an open change, ahead of default | `GET /branches`, `GET /compare/<default>...<branch>` | `GET /repository/branches`, `GET /repository/compare` | `GET /branches`, `GET /compare` |
| Tags and releases | name, sha, date | `GET /tags`, `GET /releases` | `GET /repository/tags`, `GET /releases` | `GET /tags`, `GET /releases` |

Registry tags are read only when the config lists them. "(verify)" marks an endpoint I haven't
confirmed against a live instance; each one becomes a fixture in phase 1 of that forge or is
replaced.

Where a forge has no native blocking (GitLab CE has only `relates_to`), the config's
`blocking:` convention applies: a `relates_to` link from an issue carrying the blocked label
counts as "blocked by" the linked issue. Every link carries `kind: native | convention`, and the
reports say which.

### Mergeability is three-valued

Conflict state is `yes`, `no` or `unknown`, never a boolean. From #24's comment: right after a
merge moves the target branch, GitLab reports `detailed_merge_status: unchecked` or `checking`
and `has_conflicts: false` until it re-checks, and GitHub's `mergeable: null` is the same case.

| Forge | `yes` | `no` | `unknown` |
| --- | --- | --- | --- |
| GitHub | `CONFLICTING` / `false` | `MERGEABLE` / `true` | `UNKNOWN` / `null` |
| GitLab | `detailed_merge_status: conflict` | a checked status with `has_conflicts: false` | `unchecked`, `checking`, `preparing`, `approvals_syncing`, or a missing status |
| Forgejo | `mergeable: false` once confirmed | `mergeable: true` | `mergeable: false` on a PR whose base moved within the last 10 minutes (verify on a live instance) |

`has_conflicts` is read only when the status says the check ran. Every consumer treats
`unknown` as not clean: `/catchup` prints `conflicts: unknown`, `/next` never marks such a lane
ready to merge, and `/groom` puts it in the unclear bucket and never acts on it. The reports
never re-poll to wait for an answer.

### Rules

- **Reads can't write.** #21's "GET only" means read-only. The read transport refuses every
  method but GET, with one exception: a POST to the forge's GraphQL endpoint whose document's
  operations are all queries. The transport finds the operation type of every operation in the
  document by parsing it (comments and strings skipped, a shorthand `{ ... }` counted as a
  query), never by matching the string `mutation`; a mutation or subscription anywhere in the
  document, a POST to any other path, or a document it can't parse is refused before any request
  is sent. The check is in `transport.py`, below the adapters, where no adapter can bypass it.
  Writes go through a separate transport that only `writes.py` imports.
- **Paginate to the end or fail.** Every list follows `Link` or `X-Next-Page` to the last page.
  A page cap that is hit makes the read FAILED with the cap in the reason. It never becomes a
  short list that renders as "none".
- **Each read fails on its own.** A read returns `Read(data)` or `Read(error="<reason>")`.
  Reports print `<part>: FAILED (<reason>)` in place of the section, and exit non-zero when any
  part failed.
- **The shared account.** Agents usually authenticate as the user. Nothing treats "the user's
  account did X" as "the user did X", and no assignee is a signal that a human must act. Where a
  report needs "a human acted" (a ruling, a review), it trusts the configured ruling pattern or
  label, not the author.
- **No secrets in errors.** Error text is the CLI's stderr cut to 300 characters, with anything
  matching a token pattern (`gh[pousr]_`, `glpat-`, the Forgejo token in use, `Authorization:`)
  replaced by `***`.
- **Injectable fetch.** Adapters take a `Fetch` callable (method, path, params) and never import
  `subprocess` or `urllib` themselves. Tests pass canned payloads per forge.

## Writes

The read layer can't write. Writes are a short list of named operations in `writes.py`, each
one an approved `/groom` change or a working-rule action:

| Operation | Used by |
| --- | --- |
| `file` (create an issue with title, body, labels) | discovered work |
| `rule` (ruling comment, add decision-made, remove needs-human) | recording a ruling |
| `close` (close with a resolution comment naming the PR or MR) | `/groom` |
| `labels` (add and remove) | `/groom` |
| `milestone` (set or clear) | `/groom` |
| `unlink` (remove a blocked label, or a link to a closed blocker) | `/groom` |
| `delete-branch` | `/groom` |

Each runs as `forge-tracker write <op>` with a JSON payload on stdin and prints what it did.

**The write-wrapper.** When the config sets `write_wrapper:`, every operation runs the wrapper
instead of calling the forge: `<wrapper> <op>` with the same JSON on stdin, in the repo root.
The wrapper does the write itself (a project's own `decide` command, its scrubber) and exits 0,
or exits non-zero to refuse, and its stderr is shown. forge-tracker never sends a write of its
own when a wrapper is set.

## Configuration

### userConfig

| Key | Type | Meaning |
| --- | --- | --- |
| `crossProjectTracker` | string, optional | Web URL of the project whose tracker takes cross-project work, e.g. `https://github.com/acme/meta`. A URL names the host and forge unambiguously; `owner/repo` does not. Unset: the assistant asks, every time. |

The module gets it as `register(on, options)`. The CLI gets it as
`CLAUDE_PLUGIN_OPTION_CROSSPROJECTTRACKER`; the exact case mapping is checked in phase 1, as the
2026-10-01 spec's settled question 3 asked.

### `.claude/forge-tracker.local.md`

Per repo, frontmatter only matters; the body is free notes for the assistant. Unknown keys fail
the load with the key named. Every key is optional; a report that needs an unset one says so and
leaves that part out.

```markdown
---
forge: gitlab                       # only when detection can't tell
api_base: https://code.example.org/gitlab/api/v4
ca_file: /etc/ssl/example-root.pem  # Forgejo; GitLab uses glab's ca_cert
labels:
  needs_human: needs-human
  decision: decision
  decision_made: decided
  blocked: blocked
  agent: agent-ready
tiers:                              # first match wins; milestone prefixes are the fallback
  labels: [P0, P1, P2, P3]
  milestone_prefixes: ["Tier 0", "Tier 1", "Tier 2"]
person_only_markers: [hardware, legal, purchase]
blocking: relates_to+blocked        # GitLab CE convention; native where the forge has it
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
changelog: CHANGELOG.md
report_tags: ["v*", "registry:app"]
stale_days: 14
write_wrapper: ./scripts/tracker-write
---
Releases are cut by yeet; don't file issues for release PRs.
```

The `.local.md` name follows the plugin-settings convention. Whether to commit it is the repo's
call: committed, everyone shares the label names; gitignored, it's personal.

## Commands

Each command is a markdown file that runs the CLI, passes its text output to Claude, and tells
Claude how to present it and that it may act on nothing unless asked. Every command also takes
`--json`, which prints the same data for scripts and agents.

### `/catchup` (#23)

For coming back to a project: what changed and what's waiting on you. Read-only.

**Window.** From the previous successful `/catchup`'s end, kept in
`${XDG_CACHE_HOME:-~/.cache}/forge-tracker/catchup/<host>_<project>_<user>.json` (project path
with `/` as `__`). A relative `XDG_CACHE_HOME` is ignored. The window opens 5 minutes before the
marker, so a fast local clock can't skip events; an event seen twice is harmless. No marker means
7 days.

**Marker.** It moves only when every read succeeded, and only after the report printed. The CLI
writes the new marker to a temp file, prints the report, then renames it into place; a failed
print deletes the temp file and leaves the old marker. `--since 3d` (or an ISO time) looks back
ad hoc and neither reads nor moves the marker. `--no-mark` reads the marker and doesn't move it;
the skill tells agents to always pass it when they run `/catchup` for their own purposes.

**Sections, in order:**

1. **Waiting on you:** mentions in the window, open changes where you're reviewer or assignee,
   issues assigned to you, and issues carrying the needs-human or decision labels. Items created
   or updated in the window are listed in full; the standing backlog is one count line per list.
   An item on several lists is listed once and counted in each. `--all` lists everything.
2. **Merged since:** changes merged in the window, grouped by the issue numbers in their titles
   or branches (date-shaped branch segments such as `2026-10-14` are ignored), plus changelog
   entries added when `changelog:` is set.
3. **Decisions made:** comments in the window matching `ruling_pattern`.
4. **Board movement:** issues opened and closed, counted by tier.
5. **Health:** the default branch's newest push run and its failed jobs, a failed scheduled run on
   its own line, jobs that passed only on retry more than once in the window, and the configured
   tags.

**Exit:** 0 only when every read succeeded.

```text
lokkju/skills on github.com, 2026-10-12 09:14 to 2026-10-15 08:02 (since your last catchup)

Waiting on you
  !41 review requested: feat(forge-tracker): GitHub adapter (draft no, conflicts: unknown)
  #22 needs-human, updated 2026-10-14: /groom finds tracker drift...
  standing: 3 assigned, 2 needs-human, 1 decision (not updated in the window)
Merged since
  #21  2 PRs: #44 feat(forge-tracker): read transport, #45 fix(forge-tracker): paginate links
  (no issue) #33 chore: release 2026.10.15.1
Decisions made
  #10  Ruling (2026-10-13): forge-release configures yeet, no new tooling
Board movement
  P1 +2 -1   P2 +1 -0   untiered +3 -2
Health
  main push run 1843: passed
  scheduled run 1839: FAILED (job: link-check)
  flaky: plugins (passed on retry 3 times)
  tags: session-decisions--v2026.10.15.1
```

### `/next` (#24)

Recommends what to work on: your queue, lanes agents can start now, and what's blocked. It
recommends only; it never claims, assigns, comments or branches.

**Facts** (the CLI, `forge-tracker next facts`): open issues with tier, due date, labels,
owners and links; open changes with head pipeline and conflict state; branches with no change.
Cached under `${XDG_CACHE_HOME:-~/.cache}/forge-tracker/next/<host>_<project>/facts.json` with
a fingerprint of cheap inputs: default-branch head, the newest issue, change and pipeline
`updated_at`, open issue and change counts, a digest of the milestones (id, title, due), the
remote branch heads, and a schema tag that hashes the package's own source. An unchanged
fingerprint reuses the facts. A run with a failed read is written for inspection but never
reused.

**Classification** (the CLI, deterministic):

- **Person-only:** the needs-human or decision label, or a `person_only_markers` word in the
  title or labels. Markers are hints Claude may overrule with a stated reason (design work an
  agent can build, say). An assignee is never a reason; it's shown as owner.
- **Blocked:** an open blocker by native link or the configured convention. A link whose blocker
  is closed isn't blocking, and `/groom` reports it.
- **Agent-ready:** open, not person-only, not blocked. The agent label isn't required; when it's
  configured, it only sorts labelled items first within their tier.
- **Merged but open:** flagged on the item when a merged change has a real closing reference to
  it (the same rule `/groom` uses), since it inflates the ready list badly; `/groom` fixes it.

**Unblock count:** for each person-only item, how many agent-ready items it unblocks, counted
transitively through blocking links with a visited set: a cycle counts each item once and is
reported as a cycle.

**The slate** (Claude's ranking): the command gives Claude the facts and asks for three lists.
Claude saves its answer with `forge-tracker next save-slate` (JSON on stdin), stored beside the
facts with their fingerprint. While the fingerprint holds, `/next` shows the saved slate with its
age and doesn't re-rank; `/next --rerank` forces a new one.

- **Your queue:** person-only items, most agent-ready items unblocked first.
- **Agent lanes:** agent-ready items by tier and due date, grouped into lanes that shouldn't touch
  the same files. The grouping is Claude's stated judgement from titles, bodies and labels, and
  the output says so. A lane with an open change or a pushed branch is marked in flight.
- **Blocked:** each blocked item and what it waits on.

```text
lokkju/skills, slate from 2026-10-15 07:40 (facts unchanged since)

Your queue
  1. #10 settle forge-release scope (decision)          unblocks 4: #11, #22, #23, #24
  2. #15 triage the statusline overflow (needs-human)   unblocks 1: #20
Agent lanes (grouped by judgement: these shouldn't share files)
  A  plugins/forge-tracker/forges   #21 GitHub adapter (P1, due 10-20)  in flight: !41
  B  plugins/session-decisions      #20 footer width (P2)
  C  docs                           #13 0.1.0 minor fixes (P3)
Blocked
  #22 /groom waits on #21 (native), #10 (convention: relates_to + blocked)
Flags
  #14 is referenced by merged #31 but still open (run /groom)
```

### `/groom` (#22)

Keeps the tracker telling the truth, which is what lets `/catchup` and `/next` rank real work. It reports first
and writes nothing until you approve.

**Checks:**

| Check | Evidence the report shows | Proposed change |
| --- | --- | --- |
| Merged but still open | the merged change, its closing reference (GitHub `closingIssuesReferences`, GitLab `closes_issues`, Forgejo closing keywords), and that the merge sha is on the default branch | `close` with a comment naming it |
| Tier label without matching milestone, or the reverse | the label, the milestone | `labels` or `milestone` |
| Blocked label with nothing linked | the label, no links | `unlink` |
| Link whose blocker is closed | the link, the blocker's close date | `unlink` |
| Decided but still waiting | the ruling comment, dated after the label was added | `labels` (remove needs-human or decision, add decision-made) |
| Stale branch | no open change, every commit on the default branch (`ahead_by == 0`) | `delete-branch` |
| Conflicted or stale change | `conflicts: yes`, or no activity for `stale_days` | none; listed for you |
| Likely duplicates | the pair and their similarity | none; listed for you |
| Placeholder due dates | the date and how many issues share it | none; listed for you |

**Verified or unclear.** A change goes in the proposal list only when the CLI verified it. The
rest goes in an unclear bucket that's never acted on: `conflicts: unknown`, a merged change that
mentions `#N` only in its title or branch without a closing reference, a branch
whose commits aren't all reachable from the default branch, and every duplicate.

**Approval.** Claude shows the proposals numbered with their evidence and asks once. You answer
with numbers, `all`, or `none`. Each approved change runs through `forge-tracker write` and the
write-wrapper, one at a time, and the result line says what was written or why it was refused.

**Duplicates** are found in the CLI: token-set similarity over titles and labels, among open
issues and issues closed in the last 30 days, above a fixed threshold. They're candidates for you
to judge, never closed by `/groom`.

```text
lokkju/skills: 4 proposals, 3 unclear

Proposals
  1. close #14: merged #31 "fix(session-decisions): ... Closes #14", 7c1e2a0 is on main
  2. labels #22: remove needs-human, add decided (Ruling (2026-10-14) after the label, 2026-10-12)
  3. unlink #23: blocked by #19, closed 2026-10-09
  4. delete-branch sd-26: no open PR, 0 commits ahead of main
Unclear (not acted on)
  !41 conflicts: unknown (GitHub still computing)
  #18 mentioned in branch 18-footer of merged #29, no closing reference
  #25 and #27 may be duplicates (0.71): "TaskUpdate deferred ..." / "deferred TaskUpdate ..."
Listed for you
  #30 no activity for 21 days
  9 issues share due date 2026-12-31
Approve which? (numbers, all, none)
```

## Working rules (the skill)

`skills/forge-tracker/SKILL.md` tells the assistant how to use the tracker in any session.
None of it needs the commands.

- **Routing.** Work goes on the tracker of the repo it's about. Work about several projects goes
  to `crossProjectTracker`; if that's unset, ask which tracker, every time. Nothing is
  hard-coded.
- **Discovered work** is filed when it's found, with `forge-tracker write file`, and mentioned in
  one line of the reply (`Filed #52: flaky link-check job`). It isn't held for approval. Before
  filing, search open issues for the same thing and comment there instead when one exists.
- **Rulings** are recorded with one verb: `forge-tracker write rule <N>`, which goes through the
  write-wrapper when there is one and otherwise posts `Ruling (YYYY-MM-DD): <ruling text>`, adds
  the decision-made label and removes needs-human and decision, in one step. `/groom`'s "decided
  but still waiting" check then catches only rulings recorded some other way. Never an ad hoc
  comment, a bundled note, or a session label such as `D12`.
- **Freshness.** At pickup, before merge and before handoff: `git fetch`, then re-read the
  issue and its comments since you last read it (`forge-tracker issue <N> --since <time>`), and
  say what changed. At pickup, `forge-tracker issue <N> --pickup` also records `N` against the
  current branch, which is what the closing-reference rule below reads.
- **Closing references.** When the work was picked up from an issue, the PR or MR body carries
  `Closes #N` (or `Closes !N` / `group/project#N` as the forge writes it). Work counts as picked
  up from an issue when forge-tracker recorded the pickup for the branch, or when the branch name
  has the issue number as a segment (`21-github-adapter`, `fix/21`; date-shaped segments such as
  `2026-10-14` don't count). A PR or MR with no such issue needs no reference. After merge,
  confirm the issue closed; when it didn't (a merge to a non-default branch, say), close it with
  `write close`.
- **Shared account.** Never infer that the user acted from the account that acted.
- **Agents** pass `--no-mark` to `/catchup` (or `forge-tracker catchup --no-mark`) when they run
  it for themselves.

## Hooks

The function-hooks module (`hooks/register.ts`, registered through `hooks.json` like
session-decisions) does two things.

**SessionStart needs-human list.** On a `startup` SessionStart, it runs `forge-tracker
needs-human --json` with a 5 second budget and adds one context block: open issues carrying the
needs-human label and assigned to the current user, number and title only, capped at 10 with a
count of the rest. It's context for the assistant, not a queue: nothing is imported into any
task list. The CLI answers from a 10-minute cache keyed by host, project and user; a burst of
session starts makes one forge read. On failure the block is one line, `forge-tracker: needs-human list
FAILED (<reason>)`. Outside a git repo, or with no detectable forge, it adds nothing.

Assignment is what the spec asks for, and the label is what makes an item need a human; the
assignee only narrows the list to you. That's consistent with the shared-account rule, which
forbids reading an assignee as the signal.

**Closing-reference check.** A `tool.call` hook on Bash that matches `gh pr create`, `glab mr
create` and `tea pr create` (and `gh api` POSTs to `/pulls`). It acts only when the work was
picked up from an issue, by the rule in [Working rules](#working-rules-the-skill): a pickup
forge-tracker recorded for the current branch, or the issue number as a branch segment. When
that issue has no closing reference in the body, the hook lets the call run and adds a warning
to the assistant's context naming the issue, so it can edit the PR or MR body. It never denies
the call, and a PR or MR with no issue behind it gets nothing. It doesn't look at PRs opened
outside the assistant.

## State

All state is local to the machine, outside the repo, and safe to delete.

| What | Where | Lifetime |
| --- | --- | --- |
| `/catchup` marker | `${XDG_CACHE_HOME:-~/.cache}/forge-tracker/catchup/<host>_<project>_<user>.json` | until the next successful catchup |
| `/next` facts and slate | `.../forge-tracker/next/<host>_<project>/{facts,slate}.json` | until the fingerprint moves |
| needs-human cache | `.../forge-tracker/needs-human/<host>_<project>_<user>.json` | 10 minutes |
| forge probe | `.../forge-tracker/hosts.json` | a day |
| issue pickups | `.../forge-tracker/pickup/<host>_<project>.json` (branch to issue) | until the branch's change merges |

Directories are created 0700 and files written 0600 by a temp file and `os.replace`. A
relative `XDG_CACHE_HOME` is ignored. The tracker itself (issues, labels, comments) is the only
durable state; nothing in the cache is a source of truth.

## Independence

forge-tracker names neither session-decisions nor git-commit, in code, skill text or docs. The
two plugins meet only through what each does on its own: a session-decisions item can link
`#N`, and forge-tracker's SessionStart list is context that never enters a task list. A repo
with only forge-tracker installed gets every feature here. The same goes for git-commit's
`Closes #N` wording: forge-tracker checks it from its own hook and skill.

## Testing

- **Contract suite (#21's done-when).** One set of pytest tests runs against every adapter with
  recorded fixtures per forge: pagination to the end, a hit page cap as FAILED, each read failing
  alone, three-valued mergeability (including GitLab `unchecked` with `has_conflicts: false`),
  bot detection, native and convention links, and a token in stderr coming out as `***`.
  Fixtures are real responses recorded from the live targets below and scrubbed, stored under
  `tests/fixtures/<forge>/`; CI reads only these, so it stays fast and needs no network.
- **Live targets.** GitHub uses a dedicated public repo, `lokkju/forge-tracker-fixtures`, created
  through Pulumi in lokkju/infra and seeded with issues, PRs, labels, milestones and blocking
  links. GitLab CE and Forgejo run from a docker-compose stack in
  `plugins/forge-tracker/tests/live/`, with a seed script that creates the repos, issues, MRs,
  labels and blocking links (native on Forgejo, the `relates_to` convention on GitLab CE). GitLab
  is served under a subpath behind a TLS proxy with a locally generated CA, so `api_base:` and
  the pinned-CA path are exercised. Live runs go against the stack on a machine that can host it
  (ml-01 for this repo) and against the GitHub fixtures repo; recording fixtures is a live run
  with `--record`.
- **Transport.** A write method, a GraphQL `mutation`, or a mutation hidden after a query in the
  same document is refused before any process runs.
- **Reports.** Each report's text and `--json` against fixture worlds: the marker moving only
  after a full success and a print, the 5-minute overlap, `--since` and `--no-mark` leaving the
  marker alone, fingerprint reuse and a failed run never reused, cycle-safe unblock counts, and
  every unclear `/groom` case staying out of proposals.
- **Writes.** Each operation against a fake forge, with and without a write-wrapper, and a
  wrapper refusing.
- **Module.** `claude plugin test plugins/forge-tracker`, with `process.run` hooked to return
  canned CLI output: the SessionStart block, its failure line, nothing outside a repo, and the
  closing-reference warning: a recorded pickup, an issue-numbered branch, a date-shaped branch,
  and a PR with no issue that gets nothing.
- **Live smoke.** One read-only run per forge (`forge-tracker smoke`), by hand, not in CI: every
  read against its live target, printing counts and any FAILED part.
- CI gets a `forge-tracker` job shaped like `forge-release`'s (ruff, pytest on 3.11 and 3.14),
  and `claude plugin test plugins/forge-tracker` joins the `plugins` job.

## Phases

Each phase is a releasable PR or two.

1. **GitHub read layer and `/catchup`.** Detection, config loader, transport rules, the GitHub
   adapter with the contract suite, `/catchup` with its marker, the plugin skeleton, the
   marketplace entry and the yeet target. This is the smallest slice that's useful on its own,
   and it exercises every rule in #21. Its fixtures come from the GitHub fixtures repo; until
   that repo exists and is seeded, they're recorded from this repo and scrubbed.
2. **Working rules and hooks.** The skill, `write file` and `write rule` with the write-wrapper,
   the SessionStart list, `issue --pickup` and the closing-reference hook. Still GitHub only.
3. **`/next` on GitHub.** Facts, fingerprint cache, classification, unblock counts, the slate.
4. **`/groom` on GitHub.** The checks, the unclear bucket, the remaining writes.
5. **GitLab adapter.** The docker-compose stack and its seed script, the contract suite with
   GitLab fixtures recorded from it, self-hosted subpath and CA, the `relates_to` convention.
   Every report then works on GitLab with no other change.
6. **Forgejo adapter.** Forgejo joins the stack; the same, plus the Forgejo mergeability
   measurement.
7. **Later, not now:** the status pane from #24 that shows the slate's top items, reading only
   the cache.

## Decisions (2026-10-15)

Ruled on [#38](https://github.com/lokkju/skills/pull/38). The body above already reflects each one.

1. **Architecture.** A standard-library Python CLI plus a thin hooks module. `$.http.fetch` can't
   pin a CA, `$.fs.write` can't set 0600 or write atomically, and agents can't call slash
   commands.
2. **Prototype.** Not needed. Its lessons are already in #24 and in this doc, and a local test
   instance replaces it. The phase order stands, so GitHub comes first.
3. **GraphQL.** Allowed. A POST goes only to the GraphQL endpoint, and only for documents whose
   operations are all queries, checked by parsing the operation type rather than matching a
   string. #21's "GET only" meant read-only.
4. **Write-wrapper.** The wrapper does the write itself: `<wrapper> <op>`, JSON on stdin, and a
   non-zero exit refuses.
5. **Agent-ready.** Open, not person-only and not blocked. The agent label only sorts items
   within a tier.
6. **Rulings and needs-human.** Recording a ruling adds decision-made and removes needs-human and
   decision in one step.
7. **Closing references.** `Closes #N` is expected only when the work was picked up from an
   issue, recorded by forge-tracker or named in the branch. A missing one only warns, and PRs with
   no issue are untouched.
8. **Test setup.** A docker-compose stack of Forgejo and GitLab CE, with a seed script for repos,
   issues, MRs, labels and blocking links. Fixtures recorded from it keep CI fast, live runs go
   against the stack on ml-01, and GitHub uses a dedicated test repo created through infra Pulumi.
9. **Merged but open.** Propose a close only for real closing references. An `#N` seen only in a
   title or branch goes to the unclear bucket.
