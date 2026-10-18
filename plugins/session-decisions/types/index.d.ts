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
 * What `$.store` keeps per session, so a resumed session continues its numbering and ledger: under
 * `list:<CLAUDE_CODE_TASK_LIST_ID>` when that is set, `session:<session id>` otherwise. `closed` holds the task-list items `decision_close` settled while TaskUpdate wasn't available,
 * by task id, since their task files still show them open.
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
      showSettled: boolean
      paneDismissed: boolean
      autoOpened: boolean
      details: Details | null
      ascii: boolean
    }
  }
}
