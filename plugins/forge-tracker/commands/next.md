---
description: What to work on next on this repo's forge - your queue, agent lanes that can start now, and what's blocked (read-only)
argument-hint: "[--rerank] [--refresh] [--json]"
---

Run these two commands with the Bash tool, from the repo root, and wait for both:

```bash
git fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next $ARGUMENTS
```

`/next` recommends; it never acts. Don't claim, assign, label, comment on or branch for any item
because of this report unless the user asks.

## When the report ends with `slate: needed`

The facts changed since the last ranking (or there is none, or the user passed `--rerank`), so
rank them before presenting anything:

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next facts --json`. Each item has its class
   (`person-only`, `agent-ready`, `blocked`, `unknown`), tier, due date, labels, owners, blockers,
   what it unblocks, what has it in flight, and its body.
2. Build the slate as JSON:

   ```json
   {
     "queue": ["#10", "#15"],
     "lanes": [
       {"area": "plugins/forge-tracker/forges", "items": ["#21", "#30"],
        "why": "both change the GitHub adapter"}
     ],
     "overrules": {"#40": "design work an agent can build from the spec"},
     "note": "optional, one line"
   }
   ```

   - **queue**: the `person-only` items in the order the user should take them. Start from the
     facts' order (most agent-ready items unblocked first) and move items only for a reason you
     can state (a due date, a tier).
   - **lanes**: the `agent-ready` items, by tier and due date, grouped so that no two lanes are
     likely to touch the same files. Judge that from titles, bodies and labels; `area` names the
     files or part of the repo the lane touches. An item with something in flight stays in its
     lane; the report marks it. Items you leave out are listed as not in a lane.
   - **overrules**: an item that's person-only only by a marker word (`marker: legal`, say), never
     by a label, may go in a lane when you give the reason here.
3. Save it: pipe the JSON to `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next save-slate`. A
   refusal (exit 2) names every problem; fix them and save again. It writes only to the local
   cache, never to the forge.
4. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next` again and present that report.

A later `/next` shows the saved slate, with its age, until the facts move.

## Presenting the report

1. **Your queue** first, numbered, keeping each item's number, why it's person-only (the label or
   marker) and what it unblocks.
2. **Agent lanes** next. Say plainly that the grouping is your judgement, not a measurement.
   Keep each item's in-flight marks (an open PR with its draft, conflict and CI state, or a pushed
   branch) and never call a lane with `conflicts: unknown` ready to merge.
3. **Blocked**: each item and what it waits on.
4. **Open PRs with no open issue**: each with its draft, conflict, CI and review state. These are
   in flight too; never drop them, and never call one with `conflicts: unknown` ready to merge.
5. **Flags** last: merged-but-open issues, links to closed blockers and blocked labels with
   nothing linked (all of which `/groom` fixes), blocking cycles, and pushed branches with no PR.
6. State every `FAILED (...)` line plainly, with its reason, in the section it belongs to. A
   failed read is not "nothing": never summarize it as empty or clear. When blocking links
   failed, the items listed as not classified may be blocked; don't call them agent-ready.

An assignee is never a reason an item is the user's or an agent's: agents share the user's
account. Show assignees as owners.

Exit status 1 means at least one read failed; the report still printed, nothing was cached for
reuse, and no slate is shown or saved until a run reads everything. Exit status 2 is a setup
problem (no origin remote, a forge it can't read yet, a bad `.claude/forge-tracker.local.md`, or
`gh` not logged in): show the message and stop.
