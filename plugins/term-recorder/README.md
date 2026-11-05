# term-recorder

term-recorder runs a terminal program in a terminal of a fixed size, drives it from a step file of keystrokes, waits and clicks, and captures what it shows: plain-text screen snaps, an asciicast, and a GIF through [agg](https://github.com/asciinema/agg). I built it so an assistant can look at a TUI the way a person does, Claude Code included, instead of guessing from the source how a dialog wraps or whether markdown renders.

```bash
term-record -C "$(mktemp -d)" -s skills/term-recorder/examples/ask-dialog.steps -- claude --model haiku
```

That one starts a fresh Claude Code session, gets past the folder-trust dialog, asks for a question dialog with a multi-line markdown question, and prints the screen with the dialog open.

## How it runs

The command gets its own tmux server: `tmux -L term-record-<pid>` with a generated config and no status line, so it never touches your tmux server, your sessions or your `~/.tmux.conf`. It starts with only HOME, PATH, USER, LOGNAME, SHELL, LANG, LC_ALL, LC_CTYPE and TMPDIR from your environment, plus whatever you add with `--env NAME=VALUE` or `--pass NAME`. The default size is 100x30; `--size` changes it. The server is killed when the run ends, or left up with `--keep`, which prints how to attach.

Exit status 0 means every step ran. 1 means a step failed (a wait timed out, the text to click wasn't on screen, or the command exited), and the screen at that moment goes to stderr. 2 is a usage or setup problem.

## Step files

One step per line, split with shell quoting, `#` comments allowed:

```
include claude-start.steps
paste prompt.txt
keys Enter
wait 'Close' 120
snap dialog
```

`wait`, `gone` and `idle` wait on regular expressions; `keys`, `type`, `paste` and `click` send input; `snap` captures the screen; `include` and `when` compose step files. `term-record --help` has the full list, and the [skill](skills/term-recorder/SKILL.md) has it with notes on driving Claude Code. [`examples/claude-start.steps`](skills/term-recorder/examples/claude-start.steps) gets a fresh Claude Code session to an empty prompt.

## Recording

`--cast FILE` writes an asciicast v2 file, and `--gif FILE` renders a GIF with agg. Frames are captured whenever the screen changes. During an `idle` wait, which is the step for "until the assistant stops working", the cast's clock runs eight times slower, so a minute of thinking plays in a few seconds.

## The MCP server

The plugin also starts an MCP server (`.mcp.json`, `skills/term-recorder/scripts/term-recorder-mcp`) whose tools call the same code as the CLI, so the skill records without a shell command at all:

| Tool | Does |
| --- | --- |
| `record` | runs `command` (an argv list) with `steps` inline or from `steps_file`, and returns each snap's screen as text, then the paths of the snaps, the cast and the GIF; a failed step is an error result with the screen at that moment |
| `check_steps` | parses steps, includes expanded, without running anything |
| `snaps` | lists the snaps in an earlier run's `out`, or reads one by `name` |

Arguments mirror the flags: `size`, `cwd`, `out`, `cast`, `gif`, `agg_args`, `env` (an object), `pass`, `vars`, `term`, `busy_timescale`, plus `show` (`all`, `final` or `none`) for which screens come back as text. Relative paths resolve against the project directory. `${EXAMPLES}` in any step file, for the CLI too, is the bundled examples directory, so inline steps can start with `include ${EXAMPLES}/claude-start.steps`. `--keep` has no tool argument; a server left running past the call would outlive anything that could attach to it.

The server speaks MCP as newline-delimited JSON-RPC in the standard library, through `term_recorder/mcp_stdio.py`. That module is forge-tracker's, copied byte for byte: the two plugins install separately, so neither can import the other's. `scripts/sync-mcp-stdio.py` copies forge-tracker's over, and its `--check` runs in CI and pre-commit, as does a test in this plugin, so the copies can't drift.

## Permissions

The scripts and the example step files live in the skill's own directory (`skills/term-recorder/`), so an install that copies only the skill, such as `npx skills add`, gets them too. The skill's `allowed-tools` pre-approves the three MCP tools (`mcp__plugin_term-recorder_term-recorder__record`, `__check_steps` and `__snaps`) and, for agents without the server, `${CLAUDE_SKILL_DIR}/scripts/term-record`, for the turn the skill is used in. Installing the plugin is the grant; you don't edit any settings. A shell grant only matches a command line that starts with the script, so `mkdir -p out && term-record ...` prompted; a tool call has no command line to chain. To allow recording outside the skill's turn, add the tool names to `permissions.allow` yourself; `record` runs whatever command it's given, as the script does.

In OpenCode, the package's entry adds the server as `term-recorder`. `term-recorder_record` runs a command as bash does, so it follows your bash catch-all: OpenCode allows bash by default and allows `record` with it, while a `bash` set to `"ask"` or `"deny"` (or its `"*"` rule) gives `record` the same rule. The `allowScripts` option, which already trusts the script, leaves `record` allowed.

## Requirements

Python 3.9 or newer (standard library only), tmux 3.0 or newer, and agg for `--gif`.

## Tests

```bash
uv run --no-project --with pytest pytest plugins/term-recorder/tests
```

They drive real tmux sessions against small shell and Python targets, so tmux has to be installed. `test_server.py` starts the MCP server as a child process and speaks MCP to it over stdio, recording a small shell target through `record`.
