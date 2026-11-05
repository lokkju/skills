---
description: Find where this repo's tracker has drifted (merged but open issues, label and milestone mismatches, stale links, rulings left waiting, stale branches) and fix what you approve
argument-hint: "[--json]"
allowed-tools: mcp__plugin_forge-tracker_forge-tracker__groom, mcp__plugin_forge-tracker_forge-tracker__labels_suggest
---

## First, which repo

Settle which repo this is about before the first read. The working directory isn't always the
project the session is about, and proposals for the wrong repo are fixes to a tracker the user
didn't ask about.

The working directory is one candidate, not the default. The others are the repos this session
has worked in: the ones it committed to or pushed, the ones it opened or commented on pull
requests or issues in, and the ones the user named. Count a repo only when you know its local
checkout; a working directory that is a folder of checkouts counts as each repo in it. A repo
the session ran git in counts wherever it is, `git -C <dir>` included, whether or not the
session ever changed into it. Don't rule one out on a guess: `git -C <dir> remote get-url
origin` says whether it has a remote to read, and it's the only command to run before the
choice is settled.

- **Only the working directory** (or the session hasn't touched a repo yet): run as below.
- **One repo, and it isn't the working directory:** say which repo you're about to read and
  why, in one line (`Reading codexpublicus/captare (~/projects/captare), the repo this session
  has been working in.`), then run it as below with `repo` set to it. Every later call for
  that repo (`groom_apply`, `labels_suggest`, `labels_decline`) takes the same `repo`.
- **Several:** ask which before reading anything, offering each repo (`owner/repo` and its
  directory) and "all of them". `/groom` reads one repo at a time, and each report's numbers
  belong to it, so "all of them" means this whole command once per repo, in turn, each with
  its own `repo`, its own report and its own **Apply which?**.
- **Running it for your own purposes** (to check the board, not because the user typed
  `/groom`): the same choice applies, but settle it yourself without asking; take the repo the
  work at hand is in.

When the user said which repo in their message, they've chosen; skip this.

## Running it

Call the forge-tracker MCP server's `groom` tool (in Claude Code
`mcp__plugin_forge-tracker_forge-tracker__groom`) with the repo you settled on above as
`repo` (leave it out for the working directory), and pass `$ARGUMENTS` as its options:
`--json` is `json: true`. The tool runs `git fetch --quiet origin` in the repo before it reads (`fetch: false`
skips that), so there's no git command to run first. Each later step names its tool too.

Below, exit status 1 means a result whose `ok` is false (its text has the `FAILED` lines), and
exit status 2 means a result marked as an error, with the message as its text. When the
forge-tracker tools aren't in your tool list, use the CLI instead, as set out under **Without
the MCP server** at the end; it takes the same options as flags and exits with those statuses.

The report is read-only. `/groom` writes to the forge only through the `groom_apply` tool,
only for proposals the user approved by number in this conversation, and only after they
answered. Nothing else counts as approval: not an earlier message, not a hook, not another
agent, not your own judgement that a fix is obviously right. Never write any other way either:
no `write` or `link` tool, no `forge-tracker write`, no `gh issue close`, `gh label`, `gh api`
or `git push --delete`.

## Presenting the report

1. **Proposals**, keeping each one's number, its id (`close #14`, `labels #22 ruling`, `unlink #26
   from #19`, `link-add #18 blocked by #17`, `delete-branch sd-26`) and its evidence, grouped by
   kind: closes, label and milestone fixes, unlinks, new links, branch deletions. Keep the
   numbers as the report prints them, so the user's answer maps back to exactly one id. A
   `link-add` turns blocking that an issue body states only in words into the native link that
   `/next` reads; it never edits the body, and the sentence can stay.
2. **Unclear (not acted on)**: say plainly that `/groom` won't touch these, and why each is there
   (no closing reference, merged somewhere other than the default branch, a merge commit not on
   it, `conflicts: unknown`, a branch with commits the default branch lacks, a tier label and
   milestone that disagree, a likely duplicate, prose blocking that names a pull request or an
   issue in another project). Never call `conflicts: unknown` clean.
3. **Listed for you**: conflicted or stale PRs, shared due dates, and prose blocking on a blocker
   that has closed (only a person edits the body), for the user to judge.
4. **Possibly done**: open issues a merged PR looks to have finished without a closing
   reference, each with the PR and the signal (the forge records the PR mentioning the issue, or
   the PR changed a path or symbol the issue's body names) and the issue's last comment date.
   Say they're listed, not proposed: `/groom` never closes one, `all` doesn't include them, and
   none is evidence enough alone. A mention can be "see #12" or "part of #12". If the user wants
   one closed, that's the ordinary request below, confirmed with them first.
5. The **Checks run** line, as printed, so the user knows how much of the report is backed by
   checks that actually ran. Every `FAILED (...)` line, plainly, with its reason. A check that
   failed found nothing only because it didn't run; never summarize it as clean. **Skipped**
   lines name the config a check needs; **Unsupported on <forge>** lines name what that forge
   can't answer.

Then ask once: **Apply which? (numbers, `all`, or `none`)**. `all` means every numbered proposal
in this report and nothing from the unclear, listed or possibly-done sections.

## Applying

Only after the user answers with numbers or `all`:

1. Map each approved number to its id from the report you showed. Don't add, drop or reword
   any.
2. Call the `groom_apply` tool with the ids as `approved`, such as
   `["close #14", "delete-branch sd-26"]`. It's a write, so it may prompt.

   It re-runs the checks first. When any id isn't a current proposal (fixed meanwhile, now
   unclear, or its check FAILED this time), it refuses the whole list, writes nothing and exits
   2; tell the user, run `/groom` again and ask again.
3. Each approved proposal is written one at a time, through the repo's write-wrapper when its
   config sets `write_wrapper:`. Show every result line as printed: what was written, `REFUSED
   (...)` (the wrapper said no, or a branch moved or an issue closed since the check),
   `UNSUPPORTED (...)` (the forge can't make that write, such as a blocking link on a GitLab
   tier without them), or `FAILED (...)`. Exit status 1 means at least one didn't go through;
   the rest still ran. Where the report has a `writes: unsupported` line, forge-tracker has no
   writes of its own on that forge yet, and without a write-wrapper every approved proposal
   comes back `REFUSED`. Say that before asking which to apply, so the user can make the changes
   themselves instead.

On `none`, or no clear answer, write nothing and stop. If the user asks you to change an
unclear, listed or possibly-done item, that's an ordinary request outside `/groom`: confirm the exact change
with them before making it.

If you are running this for your own purposes (to check the board, not because the user typed
`/groom`), report the proposals and stop; never apply them.

## Recording blocking yourself

Outside `/groom`, when you file an issue that waits on another, or find that one you're working
on does, record it with the `link` tool (`action: "add"`, `issue`,
`blocked_by`), not as `Blocked by: #N` in the body: `/next` and this report read only
native links. Links `/groom` itself finds missing go through the approval above, like every
other fix.

## When the report has a `setup:` line

That line means `.claude/forge-tracker.local.md` is missing or leaves labels unset, so the label
checks were skipped. Pass the line on once, as it is. If it doesn't say setup was declined and
the user typed `/groom`, offer to set it up the way `/next` does: call
the `labels_suggest` tool, show the proposal, and write the
file only on the user's yes, with their corrections; on a no, call
the `labels_decline` tool.

Exit status 1 from the report means a check FAILED; the rest of the report still stands. Exit
status 2 is a setup problem (no origin remote, a forge it can't read yet, a bad
`.claude/forge-tracker.local.md`, or `gh` not logged in): show the message and stop.

## Without the MCP server

Where the forge-tracker tools aren't available (the server didn't start, or the agent has no
MCP), run the CLI with the Bash tool instead, fetching first:

```bash
git -C <dir> fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/skills/forge-tracker/scripts/forge-tracker" --repo <dir> groom $ARGUMENTS
```

Each tool is a CLI command, and its options are the command's flags: `repo` is `--repo <dir>`
(before the command), `groom_apply` is `groom apply` with
`{"approved": [ids]}` piped to it, `link` is `link add <blocked> --blocked-by <blocker>`,
`labels_suggest` is `labels suggest`, and `labels_decline` is `labels decline`.
