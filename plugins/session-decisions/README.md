# session-decisions

Keeps the decisions and actions an assistant is waiting on you for in the session task list
(ctrl+t), so questions buried in long messages don't get lost, and shows them live while they're
open.

- **Items:** `D3 DECIDE: <question> (recommend <choice>) [<link>]` for choices, and
  `A2 ACTION: <what you have to do> [<link>]` for things only you can do. The link is an issue,
  merge or pull request, URL, or `no link`.
- **Numbering:** a hook on TaskCreate assigns `D<k>`/`A<k>` and refuses items without a
  recommendation or a link, telling the assistant what's missing.
- **Band:** while anything is open, a row above the prompt shows the count and the oldest item,
  with Show and Hide buttons. It comes back when a new item arrives.
- **Pane:** `/decisions pane` (or Show) opens a list of every item with its age. Accept fills your
  prompt with `D3: go with <the recommendation>`, Done with `A2: done`, and Answer with `D3: `;
  you edit and send it like any answer. Show settled adds completed items and their rulings.
- **Status:** the open count is the plugin's status entry under the prompt.
- **Compaction:** after compaction or resume, the assistant is reminded of what's still open.
- **Commands:** `/decisions` lists open items, `/decisions all` adds settled ones with their
  rulings, and `/decisions wrap` has the assistant settle each open item before the session ends.
- **Fallback:** where TaskCreate isn't available, the assistant uses the plugin's
  `decision_add` and `decision_close` tools instead, and the items show in the band, pane and
  `/decisions` the same way.

Labels are session-scoped: rulings recorded anywhere outside the session carry the ruling and its
date, never the label.

## Requirements

Claude Code with function hooks, which interactive sessions and background jobs load. A plain
`claude -p` run doesn't load them unless `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` is set; there the
skill tells the assistant to number items itself.

## How it stays in sync

The plugin never polls. It keeps the items in the session's plugin state and updates them from the
TaskCreate and TaskUpdate calls as they finish; the band, pane and status entry redraw from that
state. It reads the task files once, at session start and after compaction or resume, to pick up
items it didn't see created. Label counters and the fallback ledger are kept per session in the
plugin's store, so a resumed session continues its numbering.

On VS Code and mobile there's no band (Claude Code only raises it on the terminal and desktop);
the pane, status entry and `/decisions` work everywhere.

## Limits

- The task files are read from `~/.claude/tasks/<session id>/`, which Claude Code doesn't
  document. If that changes, items created before a resume or compaction stop showing until
  they're updated; labelling is unaffected.
- Two sessions sharing a task list (`CLAUDE_CODE_TASK_LIST_ID`) don't see each other's changes
  until their next session start.
- Function hooks are early access and their API can change between Claude Code releases.

## Development

```bash
claude plugin validate --strict plugins/session-decisions
claude plugin test plugins/session-decisions
```

For an editor or `tsc`, run `/plugin-types plugins/session-decisions/.claude/types` in Claude Code
to write the engine's declarations for your build (git ignores them), then
`npx -p typescript@5 tsc -p plugins/session-decisions/tsconfig.json`.
