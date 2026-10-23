---
name: session-decisions
description: "Use whenever a step waits on the user (a secret to store, a prompt to approve, access only they have, a merge), or you need a decision or an answer from them, even when it doesn't block your own work; when the user answers one; when the open items need reconciling with what the session has settled; and when a session is wrapping up or being handed off. Keeps those items in the session task list as D1, D2, ... (decisions) and A1, A2, ... (actions only the user can do), so none get lost in long messages."
---

# Session decisions

The user reads the decision queue (the cards, the decisions sidebar and footer, `/decisions`, and
ctrl+t where the session has task tools) as the list of things waiting on them. Every step only
they can do, and every question you need answered, goes there in the same turn you raise it. The
message still carries the detail; the queue is so nothing gets lost.

## Actions only the user can take

An `ACTION` is any step that waits on the user: a secret or credential to put somewhere, a prompt
or security key to approve, access or an account only they have, a payment or a physical step, a
merge, a step on their hosts that you could do yourself if asked. It is the item most likely to go
unqueued, because it never blocks you: a question stops your work until it's answered, so you
queue it to get unblocked, but you can keep working around an action indefinitely while the thing
it blocks sits in a paragraph.

Queue it when you find it, even when:

- it arrived as the answer to "what do you recommend?". An answer of "put the client secret in
  Vault at this path" creates an action; it doesn't settle one.
- it's routine. An SSH key prompt to approve on every push is an action each time it's pending.
- you've already written it down. Describing it in prose, a "what's next" list, a handoff or a
  compaction summary doesn't queue it; only TaskCreate (or `decision_add`) does.

For example, when a restart is blocked on a secret only the user can store:

```text
ACTION: put the client secret in Vault so the build can restart [no link]
```

with the path, the reason and any caveat in the description.

Create the item before you write the sentence that names it. Never write `D<n>` or `A<n>` for an
item you haven't created yet; the hook picks the label, and a label in prose with no item behind
it is a promise the queue doesn't keep.

A reply that hands the user a step without queuing it ("left for you", "you'll need to", "I can't
write to Vault for you", "say the word and I'll push"), or names a label the queue doesn't hold,
gets flagged to the user, and you're reminded on the next turn.

## Before you ask a question

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

## Reconciling the queue

In a long session, items go stale behind the conversation: the user answers one in passing
without its label, says "A4: done" while you're busy with something else, or keeps adding facts to
a question until its recommendation no longer fits. When the user runs `/decisions reconcile` or
asks you to clean up the queue, and whenever you notice several open items that look settled, go
through every open item in one pass:

1. Get the open set from `mcp__session-decisions__decision_list` (or the brief
   `/decisions reconcile` sends): each item's label, kind, age, link and text, and the lines of the
   user's own messages that name it. Don't rebuild it from memory or old reminders.
2. For each item, look for evidence that it's settled: the user's own words answering it, by label
   or by subject; the state of what it links to, which you check now (a merged pull request or a
   closed issue); or a later item that supersedes it.
3. Propose, in one table with a row per item: **close**, with the ruling quoted from the user's
   message or the forge state named ("#12 merged 2026-10-21"); **update**, with the new subject
   when the question or recommendation has drifted; or **keep**. Put the evidence in its own
   column.
4. Change nothing until the user answers. Then apply everything they approved in one message,
   every TaskUpdate (or `decision_close` and `decision_update` for ledger items) as a parallel
   call, and report what changed in a line per item.

Never close an item on your own judgment. A close needs the user's words or a linked forge state;
your belief that the work is done is not a ruling. When the evidence is ambiguous, the row says
keep, and the evidence column says what you found.

## Wrapping up

When the user says the session is ending, asks for a handoff, or runs `/decisions wrap`, go
through every open item and settle each one with the user: escalate it to the project's tracker as
a question for the right person, carry it forward (say so explicitly in the handoff), or drop it.
Never leave one open silently.
