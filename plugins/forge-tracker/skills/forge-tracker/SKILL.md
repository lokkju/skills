---
name: forge-tracker
description: Use when filing an issue, recording that one issue blocks, waits on or depends on another, linking issues, or about to write "Blocked by", "Depends on" or "Blocks" in an issue body or comment. Records blocking as the forge's native links, which /next and /groom read, and labels person-only work when filing.
---

# forge-tracker working rules

## Blocking is a native link, never only a sentence

When an issue you file or work on waits on another, record it in the forge's relationship data:

```bash
"${CLAUDE_SKILL_DIR}/scripts/forge-tracker" link add 18 --blocked-by 17      # #18 waits on #17
"${CLAUDE_SKILL_DIR}/scripts/forge-tracker" link add 18 --blocked-by acme/other#4
"${CLAUDE_SKILL_DIR}/scripts/forge-tracker" link remove 18 --blocked-by 17   # no longer true
```

It goes through the repo's write-wrapper when `.claude/forge-tracker.local.md` sets one, and
writes nothing (exit 2) for a link that already exists, a remove with no link, a self-link, or a
pull request or closed issue on either side. File the issue first, then link it; a link needs
both numbers.

`${CLAUDE_SKILL_DIR}` is the directory holding this SKILL.md. Claude Code fills it in; in an
agent that leaves it as written, put that directory's path in its place.

Where the script can't run, make the same call on GitHub:

```bash
id=$(gh api repos/OWNER/REPO/issues/17 --jq .id)    # the blocker's id, not its number
gh api -X POST repos/OWNER/REPO/issues/18/dependencies/blocked_by -F issue_id="$id"
gh api -X DELETE repos/OWNER/REPO/issues/18/dependencies/blocked_by/"$id"
```

Never record blocking only as `Blocked by: #17`, `Depends on #17` or `Blocks #18` in a body or
comment. `/next` and `/groom` read only native links, so that sentence blocks nothing they see,
and it goes stale silently when #17 closes. A sentence may stay beside the link as a gloss for
people. Link only issues you filed or are working on, or when the user asks.

## Person-only work gets its label when filed

When the issue needs the user (a decision, a credential, hardware, a purchase, a legal call),
label it at filing with the repo's configured roles: `labels.needs_human` or `labels.decision` in
the frontmatter of `.claude/forge-tracker.local.md`. Use the label names that file gives, never
a guess. When the file is missing or sets the role to `~`, add no label and say in your reply
that the issue is person-only and unlabelled; `/next` offers the label setup.

## The commands

- `/next`: the user's queue, the lanes agents can start now, and what's blocked.
- `/catchup`: what changed and what's waiting on the user since the last catchup. When you run
  it for your own purposes, run `"${CLAUDE_SKILL_DIR}/scripts/forge-tracker" catchup --no-mark`
  so the user's marker stays put.
- `/groom`: tracker drift, including blocking written only as prose, fixed on the user's
  approval.
