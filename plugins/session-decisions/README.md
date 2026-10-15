# session-decisions

**A queue for everything your assistant is waiting on you for.** Questions an assistant asks get
buried in long messages and scroll away; this plugin pins each one as a numbered card (`D1` for a
decision, `A1` for something only you can do) in the transcript, a sidebar and the footer, with a
recommendation you can accept in one click.

![A Claude Code session queues its decisions while planning a SQLite to Postgres move; Accept all answers them in one message, and the sidebar drops to the two actions still waiting](docs/demo.gif)

That's a real session, sped up while the assistant works. I asked Claude to plan moving a small Flask
service from SQLite to Postgres without writing any code. It queued its questions as tasks, shown
as cards in the sidebar, each with the answer it recommends. One click on Accept all put an answer
line for every recommendation in my prompt, and one Enter settled them. What's left are the two
things only I can do: tell it where the database lives, and hand over a sample of the data.

## Install

```bash
claude plugin marketplace add lokkju/skills
claude plugin install session-decisions@lokkju
```

Restart Claude Code. There's nothing to configure; the skill tells the assistant when to queue
something, and the hook keeps it honest.

## How it works

### Asking

The assistant creates an ordinary task with a subject in one of two forms:

| Kind | Subject | Becomes |
| --- | --- | --- |
| Decision | `DECIDE: Postgres driver? (recommend psycopg 3) [#12]` | `D3 DECIDE: Postgres driver? (recommend psycopg 3) [#12]` |
| Action | `ACTION: Provision the target database [no link]` | `A2 ACTION: Provision the target database [no link]` |

A hook on TaskCreate assigns the label and refuses a decision with no recommendation, or any item
with no link, telling the assistant what's missing. The link is an issue (`#123`,
`owner/repo#123`), a merge or pull request (`!45`), a URL, or `no link`. An assistant that can't
write a recommendation isn't ready to ask yet, which is the point.

### Seeing

- **Transcript:** the row where the assistant asked becomes the item's card, and shrinks to
  `✓ D3 · <ruling>` once it's settled.
- **Sidebar:** in fullscreen, the first new item opens the queue beside the transcript (Claude
  Code only seats a pane it wasn't asked for from 144 columns). Close it and it stays closed
  until the queue empties, and then closes itself.
- **Footer:** while anything is open, `● 5 decisions` sits beside the mode labels. Click it to
  open the queue; Esc closes it.
- **Cards:** a `DECIDE` or `ACTION` chip, the label, a `recommends <choice>` tag, the question
  with any qualifier under it, the link, and the item's age, which turns red after a day. An item
  the assistant revised after asking (its description has a `Revised (<date>): ...` line) shows
  `revised <date>`, so a changed recommendation doesn't slip past you. The assistant revises with
  TaskUpdate, or with the plugin's `decision_update` tool for an item it added without the task
  tools. The question and qualifier draw as markdown; one-line rows, chips and toasts show it as
  plain text.
- **Links:** a short link (`#12`, `!45`, `owner/repo#12`, or `no link`) sits at the right end of
  the button row. A URL gets a line of its own under the question, as a clickable link: a GitHub
  issue or pull request reads `owner/repo#12` and a GitHub repository `owner/repo`; any other URL
  is shown whole and wraps instead of being cut off. A plain `http:` URL other than localhost
  can't be a clickable link in Claude Code, so it's shown as dim text.
- **More:** **More**, or a press on the card's label, opens the item in Claude Code's question
  dialog, headed by the label. The dialog shows the question, the full recommendation when the
  tag shortens it, and the start of the item's description (up to six lines). Its options are
  **Accept: <choice>** for a decision or **Done** for an action, **Copy link** when the link is a
  URL, **Copy <command>** for the first command in backticks, and **Close**. Copying works on the
  terminal; Claude Code has no clipboard path for the other surfaces yet. Whatever you type under
  the dialog's Other becomes your answer, `D3: <your text>` in the prompt. Esc closes the dialog
  and changes nothing.
- **Task list:** where the session has TaskCreate, the items are tasks, so ctrl+t lists them too.

### Answering

**Accept** puts `D3: go with <the recommendation>` in your prompt, **Done** puts `A2: done`, and
**Answer** puts `D3: ` for you to finish. The dialog **More** opens fills the same lines. Each press adds or replaces that item's line, so one
message can answer several items; **Accept all** adds a line for every open recommendation. You
edit and send it like any other message, and the assistant marks each item completed with a
dated `Ruling (2026-10-08): ...` line. Completed items stay in the task list as the session's
record; `Settled (n)` in the sidebar shows them.

### Commands

| Command | What it does |
| --- | --- |
| `/decisions` | Lists the open items |
| `/decisions all` | Adds the settled ones, with their rulings |
| `/decisions pane` | Opens the sidebar |
| `/decisions wrap` | Has the assistant settle every open item before the session ends: escalate it to the tracker, carry it into the handoff, or drop it |

### After compaction

When a session is compacted or resumed, the assistant is reminded of what's still open, so a
question asked two hours ago doesn't silently fall out of context.

Labels are session-scoped. Anything recorded outside the session (commits, issues, pull
requests) carries the ruling and its date, never `D3`, which means nothing anywhere else.

## Requirements

Claude Code with function hooks, which interactive sessions and background jobs load. A plain
`claude -p` run doesn't load them unless `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` is set; there the
skill tells the assistant to number items itself.

Not every session gets TaskCreate. For models newer than Claude 4.x, Claude Code turns the task
tools on only for background jobs, a launch tool list that names them, or
`CLAUDE_CODE_ENABLE_TODO_TOOLS` ([#4](https://github.com/lokkju/skills/issues/4)). To get them in an
ordinary session, set it in `~/.claude/settings.json`:

```json
{ "env": { "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1" } }
```

Without them, the assistant uses the plugin's `decision_add` and `decision_close` tools instead. The items show the
same way in the cards, the sidebar, the footer and `/decisions`; only `/decisions` tags them
`ledger`, since that's the one difference you'd notice: they don't appear in ctrl+t.

With `NO_COLOR` set or `TERM=dumb`, chips and glyphs fall back to ASCII (`[DECIDE]`, `+`, `x`).
Otherwise the cards use Claude Code's theme colors and follow your theme.

## How it stays in sync

The plugin never polls. It keeps the items in the session's plugin state and updates them from
TaskCreate and TaskUpdate calls as they finish; the footer, sidebar and cards redraw from that
state. It reads the task files once, at session start and after compaction or resume, to pick up
items it didn't see created, and merges them with the items it already holds and the ones in its
store rather than replacing them.

Label counters, creation times, the fallback ledger and the rulings `decision_close` keeps for
task-list items are stored as one record in the plugin's store, so a session picks up its
numbering and ledger again whatever happens to its id:

- `claude --resume`, `--continue` and compaction keep the session id and its task list, and the
  record is the session's.
- With `CLAUDE_CODE_TASK_LIST_ID` set, the record is keyed by that task list instead, so a session
  that comes back under a new id on the same list finds it. A record kept under the session id
  before the list was named moves under the list the first time it's read. Sessions sharing the
  list write the one record, keeping each other's ledger items and the higher counters.
- Backgrounding a session forks it: the job gets a new session id and a copy of the task files.
  A session with no record of its own whose task files hold decision items starts from the record
  of the session those files came from, found by matching task ids and labels.
- `--fork-session` gives a new id and an empty task list, so the fork starts a fresh queue.

The task tools can come and go within one session: a restart or resume can drop them, and
`CLAUDE_CODE_ENABLE_TODO_TOOLS` brings them back. Items added with `decision_add` while they were
missing stay ledger items when they return, and `decision_close` still settles them. A task-list
item asked before the tools went away can't be updated with TaskUpdate, so `decision_close`
settles it instead: the plugin marks it completed with the ruling in its own state and store, and
keeps it settled when it re-reads a task file that still shows it open. While TaskUpdate is
offered, `decision_close` refuses task-list items and points the assistant at TaskUpdate, and a
status TaskUpdate sets replaces the plugin's ruling.

On VS Code and mobile there's no footer count (Claude Code only raises the footer on the terminal
and desktop); the transcript cards, the sidebar and `/decisions` work everywhere.

## Limits

- The task files are read from `~/.claude/tasks/<session id>/`, which Claude Code doesn't
  document. If that changes, items created before a resume or compaction stop showing until
  they're updated; labelling is unaffected.
- Two sessions sharing a task list (`CLAUDE_CODE_TASK_LIST_ID`) don't see each other's changes
  until their next session start. They share one stored record, but each numbers from what it
  holds, so items the two create in that time can get the same label. Rulings `decision_close`
  keeps for task-list items are written as the last session to write holds them.
- `decision_close` and `decision_update` decide whether TaskUpdate exists from `$.tool.list()`.
  On Claude Code 2.1.286 that list names a deferred tool before the model loads it through
  ToolSearch, so a task-list item is sent to TaskUpdate whenever the session has it; the engine's
  declarations don't promise that. If a later build lists only loaded tools, `decision_close`
  would settle a task-list item in the queue while its task file stays open.
- A forked session is matched to its parent by the task files Claude Code copies into it, which
  it doesn't document either. A fork whose task files hold no decision items (a session that
  only used the fallback ledger, say) has nothing to match and starts a fresh queue. The parent's
  record has to be among the 100 most recently used ones the store keeps.
- A forked session takes a copy of its parent's record. Ledger items either one adds or settles
  afterwards stay in that session.
- A task-list item settled with `decision_close` stays open in its task file, so ctrl+t still
  lists it as pending until something marks it completed with TaskUpdate. The cards, the sidebar,
  `/decisions` and the reminder after compaction all show it settled.
- Whether TaskUpdate is offered comes from the session's tool list. A host that can't list its
  tools is treated as not offering it, so `decision_close` settles task-list items there too.
- ctrl+t is Claude Code's own task list, which a plugin can't add to, so items added with
  `decision_add` (no task tools in the session) never appear there. Everything else this plugin
  draws shows them.
- Function hooks are early access and their API can change between Claude Code releases.

## Development

```bash
claude plugin validate --strict plugins/session-decisions
claude plugin test plugins/session-decisions
```

For an editor or `tsc`, run `/plugin-types plugins/session-decisions/.claude/types` in Claude Code
to write the engine's declarations for your build (git ignores them), then
`npx -p typescript@5 tsc -p plugins/session-decisions/tsconfig.json`.

### Re-recording the demo

`docs/record-demo.sh` drives a real session in a 150x42 tmux pane: it types the prompt, waits for
the assistant, clicks Accept all, sends the answers, and renders the capture with
[agg](https://github.com/asciinema/agg). It needs `tmux`, `jq`, `claude` and `agg` on PATH and
spends one short Sonnet conversation.

```bash
bash plugins/session-decisions/docs/record-demo.sh   # writes docs/demo.gif
```

The recorder opts its session into the task tools with `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`, and its last
line says whether the session queued its items with TaskCreate or fell back to the ledger.
`DEMO_MODEL` picks the model the recorded session runs on.
