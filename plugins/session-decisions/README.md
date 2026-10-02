# session-decisions

Keeps the decisions and actions an assistant is waiting on you for in the session task list
(ctrl+t), so questions buried in long messages don't get lost.

- **Items:** `D3 DECIDE: <question> (recommend <choice>) [<link>]` for choices, and
  `A2 ACTION: <what you have to do> [<link>]` for things only you can do. The link is an issue,
  merge or pull request, URL, or `no link`.
- **Numbering:** a PreToolUse hook on TaskCreate assigns `D<k>`/`A<k>` from per-session counters
  and rejects items without a recommendation or a link.
- **Compaction:** a SessionStart hook re-shows open items after compaction or resume.
- **Statusline:** `decisions-status` prints `N open`; run `/decisions statusline` for the snippet.
- **Fallback:** where the task tools aren't available (some child and background sessions), a
  small ledger in the plugin's data directory takes their place; `/decisions` shows both.
- **Commands:** `/decisions` lists open items; `/decisions wrap` settles each one before a session
  ends.

Labels are session-scoped: rulings recorded anywhere outside the session carry the ruling and its
date, never the label.

## Requirements

`python3` (3.9 or newer) on `PATH`. No other dependencies.

## Limits

- The task list is read from `~/.claude/tasks/<session id>/`, which Claude Code doesn't document.
  If that changes, the hooks and statusline show nothing rather than failing.
- Fallback-ledger items don't appear in the ctrl+t panel; use `/decisions` or the statusline.

## Development

```bash
uv run --no-project --with pytest pytest plugins/session-decisions/tests -q
claude plugin validate --strict plugins/session-decisions
```
