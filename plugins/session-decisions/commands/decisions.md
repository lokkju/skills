---
description: Show this session's open decisions (D) and actions (A). "wrap" settles each open item before the session ends; "statusline" shows how to add the open count to a statusline script.
argument-hint: "[wrap | statusline]"
allowed-tools: Bash(python3:*)
---

Current queue for this session (task list plus fallback ledger):

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/decisions.py" list --session "${CLAUDE_SESSION_ID}" --data "${CLAUDE_PLUGIN_DATA}"`

Arguments: $ARGUMENTS

- **No arguments:** show the list above to the user as it is, then stop.
- **`wrap`:** for every open item, ask the user in one message what to do with each: escalate it
  (create it in the project's tracker as a question for the right person, and link it), carry it
  forward (it stays open and goes in the handoff), or drop it. Then apply the answers, completing
  each settled item with a `Ruling (<YYYY-MM-DD>): ...` line, and report what changed.
- **`statusline`:** tell the user to add this to their statusline script, which receives the
  session JSON on stdin as `$input`; it prints `N open`, or nothing when the queue is empty:

  ```bash
  sd_bin="${CLAUDE_PLUGIN_DATA}/bin/decisions-status"
  if [ -x "$sd_bin" ]; then
      sd=$(printf '%s' "$input" | "$sd_bin")
      [ -n "$sd" ] && printf ' %s' "$sd"
  fi
  ```

  Show the path with `${CLAUDE_PLUGIN_DATA}` already filled in. The file appears after the next
  session start.
