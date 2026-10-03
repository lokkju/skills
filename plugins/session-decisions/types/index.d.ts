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

/** What `$.store` keeps per session, so a resumed session continues its numbering and ledger. */
export type SessionRecord = {
  counters: Counters
  created: Record<string, number>
  ledger: Item[]
  touched: number
}

declare module 'claude-code' {
  interface PluginState {
    'session-decisions': {
      items: Item[]
      counters: Counters
      created: Record<string, number>
      showSettled: boolean
      bandHidden: boolean
      ascii: boolean
    }
  }
}
