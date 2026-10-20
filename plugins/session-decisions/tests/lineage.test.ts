import { describe, expect, test } from 'claude-code/testing'

import { ledgerMatches, trailOf } from '../hooks/lineage'
import type { Item, SessionRecord } from '../types'

const use = (tool: string, result: unknown, isError?: true) => ({ tool, result, text: typeof result === 'string' ? result : '', isError })
const ledger = (label: string, body: string, status: Item['status'] = 'pending'): Item => ({
  id: label,
  source: 'ledger',
  label,
  kind: label.startsWith('D') ? 'DECIDE' : 'ACTION',
  body,
  status,
  description: '',
})
const record = (items: Item[]): SessionRecord => ({ counters: { D: 9, A: 9 }, created: {}, ledger: items, closed: {}, touched: 1 })

describe('lineage', () => {
  test('the trail reads ledger answers, closes, task labels and the resume reminder', () => {
    const trail = trailOf([
      { toolUses: [use('mcp__session-decisions__decision_add', 'D1 DECIDE: which cluster? (recommend k3s) [no link]')] },
      { toolUses: [use('mcp__session-decisions__decision_update', 'D1 DECIDE: which cluster now? (recommend k3s) [no link]')] },
      { toolUses: [use('mcp__session-decisions__decision_add', 'A2 ACTION: approve [no link]'), use('mcp__session-decisions__decision_close', 'A2 closed')] },
      // A refused call reports nothing; a task-list close isn't a ledger close.
      { toolUses: [use('mcp__session-decisions__decision_add', 'D9 DECIDE: x? (recommend y) [no link]', true)] },
      { toolUses: [use('TaskCreate', { task: { id: '1', subject: 'A3 ACTION: log in [no link]' } })] },
      { text: "Open items in this session's decision queue, still waiting on the user:\n- D4 DECIDE: keep it? (recommend yes) [#2] (fallback ledger)\n- A3 ACTION: log in [no link]" },
    ])
    expect([...trail.ledger.keys()]).toEqual(['D1', 'A2', 'D4'])
    expect(trail.ledger.get('D1')).toEqual({
      bodies: ['which cluster? (recommend k3s) [no link]', 'which cluster now? (recommend k3s) [no link]'],
      closed: false,
    })
    expect(trail.ledger.get('A2')?.closed).toBe(true)
    expect(trail.labels.sort()).toEqual(['A2', 'A3', 'D1', 'D4'])
  })

  test('a record matches when it holds every ledger item, one word for word, closed where the trail closed it', () => {
    const trail = trailOf([
      { toolUses: [use('decision_add', 'D1 DECIDE: which cluster? (recommend k3s) [no link]')] },
      { toolUses: [use('decision_add', 'A1 ACTION: approve [no link]'), use('decision_close', 'A1 closed')] },
    ])
    expect(ledgerMatches(record([ledger('D1', 'which cluster? (recommend k3s) [no link]'), ledger('A1', 'approve [no link]', 'completed')]), trail)).toBe(true)
    // Revised elsewhere since: the label still matches, as long as one item is word for word.
    expect(ledgerMatches(record([ledger('D1', 'which cluster? (recommend k3s) [no link]'), ledger('A1', 'approve it [no link]', 'completed')]), trail)).toBe(true)
    expect(ledgerMatches(record([ledger('D1', 'other? (recommend no) [no link]'), ledger('A1', 'other [no link]', 'completed')]), trail)).toBe(false)
    expect(ledgerMatches(record([ledger('D1', 'which cluster? (recommend k3s) [no link]'), ledger('A1', 'approve [no link]')]), trail)).toBe(false)
    expect(ledgerMatches(record([ledger('D1', 'which cluster? (recommend k3s) [no link]')]), trail)).toBe(false)
    expect(ledgerMatches(record([]), trailOf([]))).toBeNull()
  })
})
