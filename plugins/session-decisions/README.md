# session-decisions

Keeps the decisions and actions an assistant is waiting on you for in the session task list
(ctrl+t), so questions buried in long messages don't get lost, and shows them live while they're
open.

- **Items:** `D3 DECIDE: <question> (recommend <choice>) [<link>]` for choices, and
  `A2 ACTION: <what you have to do> [<link>]` for things only you can do. The link is an issue,
  merge or pull request, URL, or `no link`.
- **Numbering:** a hook on TaskCreate assigns `D<k>`/`A<k>` and refuses items without a
  recommendation or a link, telling the assistant what's missing.
- **Footer:** while anything is open, `● N decisions` sits beside the mode labels at the right of
  the prompt footer. Click it to open the queue; Esc closes it.
- **Transcript:** the row where the assistant asked becomes the item's card, with its buttons, and
  shrinks to `✓ D3 · <ruling>` once settled.
- **Sidebar:** in fullscreen, a new item opens the queue as a sidebar beside the transcript (Claude
  Code places a pane it wasn't asked for only from 144 columns). Close it and it stays closed until
  the queue empties; it also closes itself then. Docked, the cards drop their borders to save
  rows.
- **Cards:** each open item shows a kind chip (`DECIDE` or `ACTION`), a `recommends <choice>` tag
  beside the label, the question with any qualifier of the recommendation under it, the link, and
  its age, which turns red after a day. Accept adds `D3: go with <the recommendation>` to your
  prompt, Done adds `A2: done`, and Answer adds `D3: `. Each press adds or replaces that item's
  line, so you can answer several items in one message; Accept all adds a line for every open
  recommendation. You edit and send it like any answer. `Settled (n)` shows completed items and
  their rulings.
- **Colors:** the cards use Claude Code's own theme colors, so they follow your theme. With
  `NO_COLOR` set or `TERM=dumb`, chips and glyphs fall back to ASCII (`[DECIDE]`, `+`, `x`).
- **Task list:** the items are ordinary tasks, so ctrl+t lists them too.
- **Compaction:** after compaction or resume, the assistant is reminded of what's still open.
- **Commands:** `/decisions` lists open items, `/decisions all` adds settled ones with their
  rulings, and `/decisions wrap` has the assistant settle each open item before the session ends.
- **Fallback:** where TaskCreate isn't available, the assistant uses the plugin's
  `decision_add` and `decision_close` tools instead, and the items show in the footer, pane and
  `/decisions` the same way.

Labels are session-scoped: rulings recorded anywhere outside the session carry the ruling and its
date, never the label.

## Requirements

Claude Code with function hooks, which interactive sessions and background jobs load. A plain
`claude -p` run doesn't load them unless `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` is set; there the
skill tells the assistant to number items itself.

## How it stays in sync

The plugin never polls. It keeps the items in the session's plugin state and updates them from the
TaskCreate and TaskUpdate calls as they finish; the footer, pane and transcript cards redraw from that
state. It reads the task files once, at session start and after compaction or resume, to pick up
items it didn't see created. Label counters and the fallback ledger are kept per session in the
plugin's store, so a resumed session continues its numbering.

On VS Code and mobile there's no footer count (Claude Code only raises the footer on the terminal
and desktop); the transcript cards, the pane and `/decisions` work everywhere.

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
