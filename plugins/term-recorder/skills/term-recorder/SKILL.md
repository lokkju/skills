---
name: term-recorder
description: Use when you need to see what a terminal program actually shows a person, instead of guessing from its source, such as checking how a TUI renders text, a dialog or a layout, testing keyboard or mouse interaction, or recording a demo GIF. Drives the program from a step file on a private tmux server and returns plain-text screen snaps; works on Claude Code itself.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/term-record *)
---

# term-recorder

`${CLAUDE_PLUGIN_ROOT}/scripts/term-record` runs a command in a terminal of a fixed size, drives it
with a step file, and captures what it shows. Use it when the question is "what does this look
like on screen", which reading code can't answer: how a dialog wraps long text, whether markdown
renders, what a TUI does at 100 columns, where a click lands.

It runs the command on its own tmux server (`tmux -L term-record-<pid>`, a generated config, no
status line) with a minimal environment, so it never touches the user's tmux sessions and the
command doesn't inherit this session's variables. The server is killed on exit.

## Running it

Write the steps to a file in your scratch directory, then:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/term-record" -C <workdir> -s <steps> -o <outdir> -- <command> [args...]
```

Each `snap NAME` writes `<outdir>/NAME.txt` and prints the screen to stdout; the screen is always
snapped as `final` at the end. Read the snaps; they're the answer. Exit status 1 means a step
failed (a wait timed out, text to click wasn't on screen, the command exited), and the screen at
that moment goes to stderr. Exit status 2 is a usage or setup problem.

The defaults: 100x30 (`--size`), TERM `screen-256color`, and only HOME, PATH, USER, LOGNAME,
SHELL, LANG, LC_ALL, LC_CTYPE and TMPDIR from this environment. Add variables with
`--env NAME=VALUE` or `--pass NAME`, and step-file substitutions with `--var NAME=VALUE`.

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

Paths in `include` and `paste` are relative to the step file.

## Claude Code as the target

`${CLAUDE_PLUGIN_ROOT}/examples/claude-start.steps` gets a fresh session past the folder-trust
dialog to an empty prompt; include it first. `examples/ask-dialog.steps` is a complete example
that opens the question dialog and snaps it.

- Start it in a scratch directory (`-C`), with a cheap model (`--model haiku`) unless the model
  matters, and `--env DISABLE_AUTOUPDATER=1` so no update notice lands in the frame.
- To try a plugin from a checkout, add `--plugin-dir <path>`, and pass `--settings <file>` to
  switch the installed copy off (`"enabledPlugins": {"<name>@<marketplace>": false}`).
- The task tools (TaskCreate and the rest) are off in an interactive session on Claude 5 models
  unless `--env CLAUDE_CODE_ENABLE_TODO_TOOLS=1`.
- Your prompt is echoed on screen, so wait on text only the result shows (a dialog's numbered
  option such as `'[0-9]\. Accept'`), never on a word from the prompt itself.
- Its busy marker, for `idle`: `'\([0-9]+m? ?[0-9]*s · |esc to interrupt'`.
- Each run is a real conversation on the user's account. Keep it to the turns the check needs.

## Recordings

`--cast FILE` writes an asciicast v2 file, and `--gif FILE` renders one through
[agg](https://github.com/asciinema/agg) (`--agg-arg` replaces its arguments). Frames are
captured when the screen changes; during an `idle` wait the cast's clock runs eight times slower
(`--busy-timescale`), so a minute of thinking plays in a few seconds.
