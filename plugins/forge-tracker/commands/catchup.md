---
description: What changed on this repo's forge and what's waiting on you since your last catchup (read-only)
argument-hint: "[--since 3d|ISO] [--no-mark] [--all] [--json] [--workspace DIR]"
---

Run these two commands with the Bash tool, from the repo root, and wait for both:

```bash
git fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" catchup $ARGUMENTS
```

If you are running this for your own purposes (to orient yourself, not because the user typed
`/catchup`), add `--no-mark`, so the user's next catchup still starts where their last one ended.

The report is read-only, and so is this command: don't comment, label, close, merge, assign or
branch because of anything in it unless the user asks. The writes it may lead to are the local
config file and, on a second yes, the missing labels, as set out under setup below.

Present it like this:

1. **Waiting on you** first. Keep every item's number, and say which list put it there (review
   requested, assigned, mentioned, yours to merge, the needs-human or decision label). `yours to
   merge` is a PR the user opened (or an agent on their account did) that waits only for them to
   merge it; it is still never "clean" when it says `conflicts: unknown`. Keep the standing-backlog
   count line as it is, `standing: none` included. When the section has a `setup:` line, an empty
   list there means no label is configured, not that nothing waits on the user; say so.
2. Then **Merged since**, **Decisions made**, **Board movement** and **Health**, shortened where
   the report is long, but never dropping a failed CI run, a flaky job or a ruling.
3. State every `FAILED (...)` line plainly, with its reason, in the section it belongs to. A
   failed part is not "nothing": never summarize it as empty or quiet.
4. Say `conflicts: unknown` as unknown, never as clean.

## When the report has a `setup:` line

That line means `.claude/forge-tracker.local.md` is missing, leaves labels unset, or sets label
roles to `~` (no such label on the forge yet), so the report can't see what those labels would
mark. Present the report first, then handle setup:

- If the line says `Setup was declined`, or you are running this for your own purposes, don't
  offer anything; pass the line on once, as it is.
- Otherwise offer to set it up, once. Run
  `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels suggest`. It only reads the repo's labels
  and prints a proposed config (labels, tiers, `ruling_pattern`), where `~` records that the repo
  has no such label, plus the labels it would create for each role no label matched or the
  config sets to `~`. Show the user the proposal and ask whether to write it, as is or with
  their corrections. When it says the file `already sets every key this proposes; nothing to
  add`, there's no config question; go straight to the labels question below.
- **Yes:** write `.claude/forge-tracker.local.md` with the proposed content and the user's
  corrections. When the file already exists, add only the keys the suggestion printed and keep
  everything else in it. Don't commit it unless the user asks (committing it shares the label
  names, and whether to is the repo's call).
- **Labels that don't exist yet:** whenever the suggestion lists any (`Not on the forge ...`),
  whether or not the config file already exists, ask a second, separate question: whether to
  create them on the forge, naming each label, its color and description,
  and saying that it goes through the repo's write-wrapper when one is set. The yes to the config
  isn't a yes to this. Only on an explicit yes, run
  `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels create <names>` with exactly the names
  the user approved (a default name such as `needs-human`, or `role=name`, such as
  `agent=ready-for-agent`, for a name of their own). It refuses, creating nothing, a name the
  repo already has in any case or a role the config already maps. Then put each created name in
  the file in place of its `~`, as its output lists them. Show any `REFUSED` or `FAILED` line
  as it is and leave that role at `~`. On a no, or for labels the user didn't approve, give
  them the `gh label create` commands from the suggestion and leave the `~` in place. Never
  create labels yourself any other way, even through `gh`; those commands are the user's.
- **No:** run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels decline`. It writes one file
  in the local cache (never the repo or the forge), so this command won't offer setup again
  here; the report's line keeps saying what's unset, and a line about roles at `~` goes quiet.
  Tell the user they can ask for it any time. Use this for a no to the labels question too, when
  the user wants those roles left at `~`.

After writing the file, the next `/catchup` uses it. To see this window with the labels now, run
the report command again with `--since` and the window's start; that leaves the marker alone.

Exit status 1 means at least one read failed; the report still printed, and the catchup marker
did not move, so the next catchup covers this window again. Exit status 2 is a setup problem
(no origin remote and no `tracker.web`, a forge it can't read yet, a bad
`.claude/forge-tracker.local.md`, or `gh` not logged in): show the message and stop.

The first line of the report says which window it covers and whether the marker moved.

## In a workspace folder

When the start directory isn't a git repo but holds several (a folder of checkouts), the same
report command reads them all and prints one report: its first line starts `Workspace <dir>: N
repos`. `git fetch` fails in such a folder; that's expected, so don't stop at it. The repos are
the folder's child repos with an `origin` remote (or a `tracker.web` key), or the `repos:` list
in the folder's own `.claude/forge-tracker.local.md`; `--workspace DIR` names a folder from
anywhere. Changelog entries come from each repo's last fetch, so when a repo's config sets
`changelog:`, run `git -C <dir> fetch --quiet origin` for it first.

Present it in the same order. **Waiting on you** covers every repo, each item as
`owner/repo#N`; keep the repo in every reference. Then each repo's own sections. A repo where
nothing happened is one line (`owner/repo: nothing since ...; main push: passed`); keep it as
one line. A `setup:` line is said once, with the repos it applies to after it.

- A line `<dir>: FAILED (...)` is a repo that couldn't be read at all (a forge it can't read yet,
  a bad config file, no token). Say it, with its reason, like any failed read.
- Exit status 1 means some repo's read failed. Only that repo's marker stays; the others moved,
  and the first line names the repos whose marker stayed.
- A `Not read:` line lists child repos left out for having no `origin` remote. Pass it on.
- Setup is per repo. Don't run the setup flow from the workspace: pass the `setup:` line on
  once, and if the user wants a repo set up, run `labels suggest` and `labels decline` for it with
  `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" --repo <dir> labels ...` and write that repo's
  own `.claude/forge-tracker.local.md`.

Exit status 2 in a workspace is a problem with the folder itself (no repo found, or a bad config
file at its root): show the message and stop.
