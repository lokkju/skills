export type Kind = 'DECIDE' | 'ACTION'
export type Letter = 'D' | 'A'
export type Status = 'pending' | 'in_progress' | 'completed'

/**
 * One decision or action. `id` is the task id for a task-list item and the label for a ledger
 * item. `created` is milliseconds since the epoch, absent for items made before this plugin saw
 * them.
 */
export type Item = {
  id: string
  source: 'tasks' | 'ledger'
  label: string
  kind: Kind
  body: string
  status: Status
  description: string
  created?: number
}

export type Counters = { D: number; A: number }

/**
 * The item the queue pane shows in full, by `<source>:<id>`, in place of the list; `owns` is true
 * when the pane opened for it, so its actions close the pane instead of going back to the list.
 */
export type Details = { item: string; owns: boolean }

/**
 * What the plugin knows of a subagent's loop, by its agent id: spawned in the background or the
 * foreground (as its `agent.spawn` said), or seen calling a task tool. `$.tool.list()` answers the
 * main loop's tools inside a subagent, so this is how decision_close tells whether the loop that
 * called it has TaskUpdate.
 */
export type Loop = 'background' | 'foreground' | 'tasks'

/**
 * When replies end with an `Open items` list: while the person writes over Remote Control (`auto`),
 * always, or never. The reply_list option sets it; `/decisions list on|off|auto` overrides it for
 * the session.
 */
export type ListMode = 'auto' | 'always' | 'never'

/**
 * What `$.store` keeps per session, so a resumed session continues its numbering and ledger: under
 * `list:<CLAUDE_CODE_TASK_LIST_ID>` when that is set, `session:<session id>` otherwise. `closed` holds the settled task-list items by task id: completed with TaskUpdate, settled by
 * `decision_close` while TaskUpdate wasn't available (their task files still show them open), or
 * marked by `decision_supersede`. It keeps them after Claude Code deletes the task files of a list
 * whose tasks are all completed.
 */
export type SessionRecord = {
  counters: Counters
  created: Record<string, number>
  ledger: Item[]
  closed: Record<string, Item>
  touched: number
}

declare module 'claude-code' {
  interface PluginState {
    'session-decisions': {
      items: Item[]
      counters: Counters
      created: Record<string, number>
      closed: Record<string, Item>
      loops: Record<string, Loop>
      turnStarted: number
      handoffNudge: string
      handoffFlagged: { labels: readonly string[]; lines: readonly string[] }
      labelsSeen: string[]
      showSettled: boolean
      paneDismissed: boolean
      autoOpened: boolean
      details: Details | null
      ascii: boolean
      remote: boolean
      listMode: ListMode | null
      pushed: Record<string, string>
    }
  }
}
