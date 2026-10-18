---
name: session-decisions
description: "Use whenever you need a decision, an answer, or an action from the user before you can continue; when the user answers one; and when a session is wrapping up or being handed off. Keeps those items in the session task list as D1, D2, ... (decisions) and A1, A2, ... (actions only the user can do), so none get lost in long messages."
---

# Session decisions

The user reads the decision queue (the cards, the decisions sidebar and footer, `/decisions`, and
ctrl+t where the session has task tools) as the list of things waiting on them. Every question you
need answered, and every step only they can do, goes there in the same turn you ask it. The message still carries the detail; the queue is so nothing gets lost.

Anything you hand back to the user counts, including a merge or a step on their hosts that you
could do yourself if asked: a reply that ends with "left for you" or "you'll need to" items that
aren't queued gets flagged to the user, and you're reminded on the next turn.

## Before you ask

Ask only when the answer blocks the work and can't be settled from the request, the code, the
project's instructions, or a sensible default. A decision has options and a recommendation. If you
can't write the recommendation, it isn't ready to ask; work it out first. Status updates and your
own work items don't go in the queue.

## Creating an item

Call TaskCreate with a subject in one of these forms, and the context in the description:

- `DECIDE: <question> (recommend <choice>) [<link>]`
- `ACTION: <what the user has to do> [<link>]`

`<link>` is one token at the end: an issue (`#123`, `owner/repo#123`), a merge or pull request
(`!45`, `#45`), a URL, or `no link`. A hook adds the label (`D3 DECIDE: ...`, `A2 ACTION: ...`) and
rejects a subject missing its recommendation or link; fix it and create it again. Don't number
items yourself while the hook is working.

Keep the subject to one short line: the question, the recommendation and the link. Everything else
goes in the description, written as short paragraphs or `- ` bullets with real line breaks between
them, never as one long paragraph. The user reads it in a scrollable view that draws your markdown
(lists, tables, code) and shows all of it, but only a few lines fit at once, so put what they need
to decide first.

In your message, refer to the item by its label ("D3"); TaskList shows it if TaskCreate's result doesn't.

If TaskCreate's result shows the subject without a label, the hook isn't running (a `claude -p`
run without `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`, for one). Number it yourself: TaskUpdate the
subject to the next free `D<n>` or `A<n>` from TaskList, and check the recommendation and link by
eye.

If TaskCreate isn't in your tool list, look for it with tool search before deciding it's missing.
Claude Code offers the task tools to background jobs, to older models, and to sessions with
`CLAUDE_CODE_ENABLE_TODO_TOOLS` set; many ordinary sessions don't have them. There, the plugin's
ledger is the normal path, not a degraded one, and never prose alone: call
`mcp__session-decisions__decision_add` with `kind` (`DECIDE` or `ACTION`), `text` (the subject
after the colon, recommendation and link included) and `description`. It returns the labelled
subject, and the item shows in the cards, the sidebar, the footer and `/decisions` just as a task
would. ctrl+t doesn't list ledger items, so end each reply that leaves ledger items open with a
short list of them. Ledger items stay in the ledger if the task tools come back later; keep closing
them with `decision_close`.

## When an open item changes

A follow-up question or a partial answer isn't a ruling. Leave the item open and revise it in the
same turn as your reply whenever its question, options, recommendation or context change, so the
queue never shows what the user has already moved past:

- TaskUpdate the subject in the same form as when you created it (the hook keeps its `D<n>` or
  `A<n>` label and checks the recommendation and link again), and append a line to the
  description: `Revised (<YYYY-MM-DD>): <what changed and why>`. Append; don't overwrite the
  context the user was shown. (Fallback ledger: call `mcp__session-decisions__decision_update`
  with the `label`, the new `text` if the subject changed, and what changed as `description`; it
  adds the Revised line.)
- If the answer turns it into a different question, don't reuse the label: complete the old item
  with `Ruling (<date>): superseded by D<m>` and create the new one.

## When the user answers

- Mark the item completed with TaskUpdate, and add a line to its description:
  `Ruling (<YYYY-MM-DD>): <the answer>`. Don't delete it; completed items are the session's record
  of what was decided. (Fallback ledger: call `mcp__session-decisions__decision_close` with the
  `label` and the `ruling`. If TaskUpdate isn't available for a task-list item, as after a restart
  that drops the task tools or in a background subagent, `decision_close` settles that item too;
  while TaskUpdate is available, use it.)
- If the item has a link, record the ruling there too, using whatever the project uses for that.
- Labels are session-scoped and restart every session. Anything written outside this session
  (commits, issues, pull requests, docs, files) gets the ruling's content and date, never `D3` or
  `A2`; outside the session the number means nothing.
- An answer of "defer", "file it", or "later" means the work needs a durable home: create it in
  the project's tracker, link it, then complete the item with that as the ruling.
- If an item stops mattering, complete it with `Ruling (<date>): moot, <why>`.

The user may answer by pressing Accept, Done or Answer in the decisions pane. That fills their
prompt with `D3: go with <choice>`, `A2: done` or `D3: `, which they edit and send like any other
answer; record it the same way. Their buttons add one line per item, so a single message can
answer several items; record each one.

## Wrapping up

When the user says the session is ending, asks for a handoff, or runs `/decisions wrap`, go
through every open item and settle each one with the user: escalate it to the project's tracker as
a question for the right person, carry it forward (say so explicitly in the handoff), or drop it.
Never leave one open silently.
