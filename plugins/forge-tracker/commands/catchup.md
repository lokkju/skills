---
description: What changed on this repo's forge and what's waiting on you since your last catchup (read-only)
argument-hint: "[--since 3d|ISO] [--no-mark] [--all] [--json]"
---

Run these two commands with the Bash tool, from the repo root, and wait for both:

```bash
git fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" catchup $ARGUMENTS
```

If you are running this for your own purposes (to orient yourself, not because the user typed
`/catchup`), add `--no-mark`, so the user's next catchup still starts where their last one ended.

The report is read-only, and so is this command: don't comment, label, close, merge, assign or
branch because of anything in it unless the user asks.

Present it like this:

1. **Waiting on you** first. Keep every item's number, and say which list put it there (review
   requested, assigned, mentioned, the needs-human or decision label). Keep the standing-backlog
   count line as it is.
2. Then **Merged since**, **Decisions made**, **Board movement** and **Health**, shortened where
   the report is long, but never dropping a failed CI run, a flaky job or a ruling.
3. State every `FAILED (...)` line plainly, with its reason, in the section it belongs to. A
   failed part is not "nothing": never summarize it as empty or quiet.
4. Say `conflicts: unknown` as unknown, never as clean.

Exit status 1 means at least one read failed; the report still printed, and the catchup marker
did not move, so the next catchup covers this window again. Exit status 2 is a setup problem
(no origin remote, a forge it can't read yet, a bad `.claude/forge-tracker.local.md`, or `gh`
not logged in): show the message and stop.

The first line of the report says which window it covers and whether the marker moved.
