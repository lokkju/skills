#!/usr/bin/env bash
# Records docs/demo.gif: a real Claude Code session in a 150x42 tmux pane, driven by keystrokes
# and mouse clicks, captured with `tmux capture-pane` and rendered with agg
# (https://github.com/asciinema/agg). Run it from a normal terminal, not from inside a Claude Code
# session: child sessions don't get TaskCreate, and the recording would show the fallback ledger.
#
#   bash plugins/session-decisions/docs/record-demo.sh [out.gif]
#
# Needs tmux, jq, claude and agg on PATH. It spends one short Sonnet conversation.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin="$(dirname "$here")"
out="${1:-$here/demo.gif}"
model="${DEMO_MODEL:-sonnet}"
cols=150 rows=42
sock="sd-demo-$$"

for tool in tmux jq claude agg; do
  command -v "$tool" >/dev/null || { echo "record-demo: $tool is not on PATH" >&2; exit 1; }
done

work="$(mktemp -d)"
cast="$work/demo.cast"
capture_pid=""
cleanup() {
  [[ -z "$capture_pid" ]] || kill "$capture_pid" 2>/dev/null || true
  tmux -L "$sock" kill-server 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# A two-file project with an obvious migration to plan, so the questions come naturally.
proj="$work/tidepool-api"
mkdir -p "$proj/src"
printf '# tidepool-api\nA small Flask service that stores tide-gauge readings in SQLite.\n' >"$proj/README.md"
cat >"$proj/src/app.py" <<'EOF'
import sqlite3
from flask import Flask, jsonify
app = Flask(__name__)
DB = "tides.db"

@app.get("/readings/<station>")
def readings(station):
    rows = sqlite3.connect(DB).execute("select ts, level from readings where station=?", (station,)).fetchall()
    return jsonify(rows)
EOF
git -C "$proj" init -q
git -C "$proj" add -A
git -C "$proj" -c user.name=demo -c user.email=demo@example.invalid commit -qm init

# The installed copy of the plugin is switched off so the recording shows this checkout's code.
cat >"$work/settings.json" <<'EOF'
{
  "tui": "fullscreen",
  "enabledPlugins": { "session-decisions@lokkju": false },
  "statusLine": { "type": "command", "command": "printf 'tidepool-api'" }
}
EOF

t() { tmux -L "$sock" "$@"; }
screen() { t capture-pane -t demo -p; }

# No update notices in the frame. For models newer than Claude 4.x, Claude Code turns the task
# tools (TaskCreate and the rest) on only for background jobs, a launch tool list that names them,
# or CLAUDE_CODE_ENABLE_TODO_TOOLS; without one of those, every card falls back to the ledger.
t -f /dev/null new-session -d -s demo -x "$cols" -y "$rows" -c "$proj" \
  env DISABLE_AUTOUPDATER=1 CLAUDE_CODE_ENABLE_TODO_TOOLS=1 claude --model "$model" --permission-mode default --settings "$work/settings.json" --plugin-dir "$plugin"
t set -g window-size manual
t set -g mouse on
t resize-window -t demo -x "$cols" -y "$rows"

# Capture: a frame whenever the screen changes. While the assistant is working, time runs at
# 1/8 speed in the cast, so a minute of thinking plays in a few seconds.
busy_flag="$work/busy"
capture() {
  local last="" now frame clock=0 prev
  prev="$(date +%s.%N)"
  printf '{"version": 2, "width": %d, "height": %d}\n' "$cols" "$rows" >"$cast"
  while [[ ! -e "$work/stop" ]]; do
    now="$(date +%s.%N)"
    if [[ -e "$busy_flag" ]]; then
      clock="$(echo "$clock + ($now - $prev) / 8" | bc -l)"
    else
      clock="$(echo "$clock + ($now - $prev)" | bc -l)"
    fi
    prev="$now"
    frame="$(t capture-pane -t demo -e -p 2>/dev/null || true)"
    if [[ "$frame" != "$last" ]]; then
      # agg's usual fonts lack U+23BF, Claude Code's result elbow (U+2514 looks the same), and
      # draw its no-break spaces as junk.
      jq -cn --argjson at "$clock" --arg f "$frame" \
        '[$at, "o", ("\u001b[H\u001b[2J" + ($f | gsub("\u23bf"; "\u2514") | gsub("\u00a0"; " ") | gsub("\n"; "\r\n")))]' >>"$cast"
      last="$frame"
    fi
    sleep 0.1
  done
}

wait_for() { # wait_for <regex> [seconds]
  local deadline=$((SECONDS + ${2:-90}))
  until screen | grep -qE "$1"; do
    ((SECONDS < deadline)) || { echo "record-demo: timed out waiting for /$1/" >&2; screen >&2; exit 1; }
    sleep 0.3
  done
}

# Busy: the spinner line, "(12s · ↓ 674 tokens · thinking)", or the older interrupt hint.
busy_re='\([0-9]+m? ?[0-9]*s · |esc to interrupt'

# Idle: no busy marker for three consecutive seconds.
wait_idle() {
  local quiet=0 deadline=$((SECONDS + ${1:-240}))
  touch "$busy_flag"
  while ((quiet < 6)); do
    ((SECONDS < deadline)) || { echo "record-demo: the assistant never went idle" >&2; exit 1; }
    if screen | grep -qE "$busy_re"; then quiet=0; else quiet=$((quiet + 1)); fi
    sleep 0.5
  done
  rm -f "$busy_flag"
}

type_slowly() {
  local text="$1" i
  for ((i = 0; i < ${#text}; i++)); do
    t send-keys -t demo -l "${text:i:1}"
    sleep 0.03
  done
}

# Click the first occurrence of <text> on screen, by writing an SGR mouse press and release.
click() {
  local row col
  read -r row col < <(screen | awk -v s="$1" '{ i = index($0, s); if (i) { print NR, i + 1; exit } }')
  [[ -n "${row:-}" ]] || { echo "record-demo: no \"$1\" on screen" >&2; screen >&2; exit 1; }
  t send-keys -t demo -l $'\e[<0;'"$col;$row"'M'
  sleep 0.05
  t send-keys -t demo -l $'\e[<0;'"$col;$row"'m'
}

# The trust dialog comes first in a fresh directory.
wait_for 'trust this folder|❯' 30
if screen | grep -q 'trust this folder'; then
  # Keys sent before the dialog takes input land on "No, exit".
  sleep 1.5
  t send-keys -t demo Down
  wait_for '❯ Yes, I trust' 10
  t send-keys -t demo Enter
fi
wait_for 'tidepool-api$' 30
sleep 1

capture &
capture_pid=$!
sleep 1.5

type_slowly "We're moving tidepool-api from SQLite to Postgres. Don't write code yet: read the repo, then queue the decisions you need from me and anything only I can do."
sleep 0.8
t send-keys -t demo Enter
sleep 2
wait_idle
sleep 3

# Answer everything from the sidebar: Accept all fills the prompt, then send it.
click 'Accept all'
sleep 2.5
t send-keys -t demo Enter
sleep 2
wait_idle
sleep 4

touch "$work/stop"
wait "$capture_pid"

agg --idle-time-limit 2 --last-frame-duration 4 --font-size 14 "$cast" "$out"
echo "record-demo: wrote $out"

# Which path the recorded session took: TaskCreate, or the fallback ledger (a `ledger` tag on
# every card). The transcript lives under the config dir, named after the project path.
transcripts="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf '%s' "$proj" | sed 's|[/.]|-|g')"
if jq -e 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use" and .name == "TaskCreate")' \
  "$transcripts"/*.jsonl >/dev/null 2>&1; then
  echo "record-demo: the session queued its items with TaskCreate"
else
  echo "record-demo: warning: the session never called TaskCreate, so the cards show the ledger tag" >&2
fi
