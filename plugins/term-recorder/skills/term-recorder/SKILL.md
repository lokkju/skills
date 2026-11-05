---
name: term-recorder
description: Use when you need to see what a terminal program actually shows a person, instead of guessing from its source, such as checking how a TUI renders text, a dialog or a layout, testing keyboard or mouse interaction, or recording a demo GIF. Drives the program from a step file on a private tmux server and returns plain-text screen snaps; works on Claude Code itself.
allowed-tools: mcp__plugin_term-recorder_term-recorder__record, mcp__plugin_term-recorder_term-recorder__check_steps, mcp__plugin_term-recorder_term-recorder__snaps, Bash(${CLAUDE_SKILL_DIR}/scripts/term-record *)
---

# term-recorder

term-recorder runs a command in a terminal of a fixed size, drives it with steps, and captures
what it shows. Use it when the question is "what does this look like on screen", which reading
code can't answer: how a dialog wraps long text, whether markdown renders, what a TUI does at
100 columns, where a click lands.

It runs the command on its own tmux server (`tmux -L term-record-<pid>`, a generated config, no
status line) with a minimal environment, so it never touches the user's tmux sessions and the
command doesn't inherit this session's variables. The server is killed when the run ends.

## Running it

Call the `record` tool (the term-recorder MCP server's; in Claude Code
`mcp__plugin_term-recorder_term-recorder__record`) with:

- `command`: the program and its arguments as a list, such as `["claude", "--model", "haiku"]`.
- `steps`: the steps inline, one per line (below), or `steps_file` for a step file; both run
  the file first. `${EXAMPLES}` in steps is the bundled examples directory.
- `cwd`: where the command starts (default: the project directory). `include` and `paste` paths
  in inline steps are relative to it.
- `out`: where the snaps go (default: a new temporary directory); the directory is created.

Each `snap NAME` writes `<out>/NAME.txt`, and the screen is always snapped as `final` at the end.
The result is every snap's screen as text, then where the snaps went; read them, they're the
answer. `show: "final"` returns only the final screen and `show: "none"` none (the files are
written either way); the `snaps` tool lists an earlier run's snaps or reads one by name. A
failed step (a wait timed out, text to click wasn't on screen, the command exited) makes the
result an error that names the step and shows the screen at that moment.

`check_steps` parses steps without running anything, which is cheap insurance before a run that
costs a real Claude Code conversation.

The defaults: 100x30 (`size`), TERM `screen-256color`, and only HOME, PATH, USER, LOGNAME,
SHELL, LANG, LC_ALL, LC_CTYPE and TMPDIR from the server's environment. Add variables with
`env` (an object) or `pass` (names), and step substitutions with `vars`.

### Without the MCP server

In an agent without the server, run the CLI over the same code. Write the steps to a file in
your scratch directory, then run it as its own command, not chained to another:

```bash
"${CLAUDE_SKILL_DIR}/scripts/term-record" -C <workdir> -s <steps> -o <outdir> -- <command> [args...]
```

`${CLAUDE_SKILL_DIR}` is the directory holding this SKILL.md. Claude Code fills it in; in an
agent that leaves it as written, put that directory's path in its place. Snaps print to stdout
as well as going to `<outdir>`. Exit status 1 means a step failed, with the screen on stderr;
2 is a usage or setup problem. The flags match the tool's arguments: `--size`, `--env
NAME=VALUE`, `--pass NAME`, `--var NAME=VALUE`, `--cast`, `--gif`, `--agg-arg`, and `-s -`
reads steps from stdin.

## Steps

One per line, shell quoting, `#` comments:

| Step | Does |
| --- | --- |
| `wait REGEX [TIMEOUT]` | waits until the screen matches (Python `re`, multiline; 90s) |
| `gone REGEX [TIMEOUT]` | waits until it no longer matches |
| `idle REGEX [QUIET] [TIMEOUT]` | waits until REGEX, a busy marker, has been absent for QUIET seconds (3; 240s) |
| `keys KEY...` | sends tmux key names: `Enter`, `Down`, `Escape`, `C-c` |
| `type TEXT [DELAY]` | types TEXT literally, a character every DELAY seconds (0.03; 0 sends it at once) |
| `paste FILE` | pastes FILE as one bracketed paste, which suits multi-line prompts |
| `click TEXT` | left-clicks the first on-screen TEXT |
| `sleep SECONDS` | |
| `snap NAME` | captures the screen as text |
| `resize COLSxROWS` | changes the terminal's size mid-run; snap after each to see where a layout breaks |
| `record on\|off` | turns cast frames on or off |
| `include FILE` | runs FILE's steps in place |
| `when REGEX STEP...` | runs STEP only if the screen matches now |

Paths in `include` and `paste` are relative to the step file (to `cwd` for inline steps).

## Claude Code as the target

`include ${EXAMPLES}/claude-start.steps` gets a fresh session past the folder-trust dialog to
an empty prompt; put it first. `${EXAMPLES}/ask-dialog.steps` is a complete example that opens
the question dialog and snaps it.

- Start it in a scratch directory (`cwd`), with a cheap model (`--model haiku`) unless the model
  matters, and `DISABLE_AUTOUPDATER=1` in `env` so no update notice lands in the frame.
- To try a plugin from a checkout, add `--plugin-dir <path>`, and pass `--settings <file>` to
  switch the installed copy off (`"enabledPlugins": {"<name>@<marketplace>": false}`).
- The task tools (TaskCreate and the rest) are off on Claude 5 models, interactive or `-p`,
  unless `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` is in `env`. Since Claude Code 2.1.300, `--model haiku` is
  Haiku 5.5, so that includes haiku. Even then they're deferred tools: they show up in the
  deferred-tool list and load through ToolSearch, not in the initial tool list.
- Your prompt is echoed on screen, so wait on text only the result shows (a dialog's numbered
  option such as `'[0-9]\. Accept'`), never on a word from the prompt itself.
- Its busy marker, for `idle`: `'\([0-9]+m? ?[0-9]*s · |esc to interrupt'`.
- Each run is a real conversation on the user's account. Keep it to the turns the check needs.

## Recordings

`cast` writes an asciicast v2 file, and `gif` renders one through
[agg](https://github.com/asciinema/agg) (`agg_args` replaces its arguments); the result gives
both paths. Frames are captured when the screen changes; during an `idle` wait the cast's clock
runs eight times slower (`busy_timescale`), so a minute of thinking plays in a few seconds.
