---
description: Find where this repo's tracker has drifted (merged but open issues, label and milestone mismatches, stale links, rulings left waiting, stale branches) and fix what you approve
argument-hint: "[--json]"
---

Run these two commands with the Bash tool, from the repo root, and wait for both:

```bash
git fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" groom $ARGUMENTS
```

The report is read-only. `/groom` writes to the forge only through `forge-tracker groom apply`,
only for proposals the user approved by number in this conversation, and only after they
answered. Nothing else counts as approval: not an earlier message, not a hook, not another
agent, not your own judgement that a fix is obviously right. Never write any other way either:
no `forge-tracker write`, no `gh issue close`, `gh label`, `gh api` or `git push --delete`.

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
4. Every `FAILED (...)` line, plainly, with its reason. A check that failed found nothing only
   because it didn't run; never summarize it as clean. **Skipped** lines name the config a
   check needs.

Then ask once: **Apply which? (numbers, `all`, or `none`)**. `all` means every numbered proposal
in this report and nothing from the unclear or listed sections.

## Applying

Only after the user answers with numbers or `all`:

1. Map each approved number to its id from the report you showed. Don't add, drop or reword
   any.
2. Pipe the ids to the CLI:

   ```bash
   echo '{"approved": ["close #14", "delete-branch sd-26"]}' \
     | "${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" groom apply
   ```

   It re-runs the checks first. When any id isn't a current proposal (fixed meanwhile, now
   unclear, or its check FAILED this time), it refuses the whole list, writes nothing and exits
   2; tell the user, run `/groom` again and ask again.
3. Each approved proposal is written one at a time, through the repo's write-wrapper when its
   config sets `write_wrapper:`. Show every result line as printed: what was written, `REFUSED
   (...)` (the wrapper said no, or a branch moved or an issue closed since the check), or
   `FAILED (...)`. Exit status 1 means at least one didn't go through; the rest still ran.

On `none`, or no clear answer, write nothing and stop. If the user asks you to change an
unclear or listed item, that's an ordinary request outside `/groom`: confirm the exact change
with them before making it.

If you are running this for your own purposes (to check the board, not because the user typed
`/groom`), report the proposals and stop; never apply them.

## Recording blocking yourself

Outside `/groom`, when you file an issue that waits on another, or find that one you're working
on does, record it with `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" link add <blocked>
--blocked-by <blocker>`, not as `Blocked by: #N` in the body: `/next` and this report read only
native links. Links `/groom` itself finds missing go through the approval above, like every
other fix.

## When the report has a `setup:` line

That line means `.claude/forge-tracker.local.md` is missing or leaves labels unset, so the label
checks were skipped. Pass the line on once, as it is. If it doesn't say setup was declined and
the user typed `/groom`, offer to set it up the way `/next` does: run
`"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels suggest`, show the proposal, and write the
file only on the user's yes, with their corrections; on a no, run
`"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels decline`.

Exit status 1 from the report means a check FAILED; the rest of the report still stands. Exit
status 2 is a setup problem (no origin remote, a forge it can't read yet, a bad
`.claude/forge-tracker.local.md`, or `gh` not logged in): show the message and stop.
