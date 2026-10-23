import { describe, expect, test } from 'claude-code/testing'

import { RECONCILE, brief, inventory, mentions } from '../hooks/reconcile'
import type { Item } from '../types'

const NOW = Date.UTC(2026, 9, 21, 12)
const HOUR = 60 * 60 * 1000

const item = (label: string, body: string, extra: Partial<Item> = {}): Item => ({
  id: label,
  source: 'tasks',
  label,
  kind: label.startsWith('D') ? 'DECIDE' : 'ACTION',
  body,
  status: 'pending',
  description: '',
  ...extra,
})

const user = (text: string) => ({ role: 'user', text })

describe('reconcile', () => {
  test('the inventory is a table of open items with label, kind, age, link and text', () => {
    const table = inventory(
      [
        item('A116', 'add the disks to the pool [#12]', { created: NOW - 26 * HOUR }),
        item('D141', 'btrfs or zfs? (recommend zfs) [no link]', {
          created: NOW - 2 * HOUR,
          source: 'ledger',
          description: 'Revised (2026-10-21): more disks',
        }),
        item('D9', 'old one? (recommend no) [no link]', { status: 'completed' }),
        item('D157', 'a | b? (recommend a) [https://example.com/x]'),
      ],
      NOW,
    )
    expect(table.split('\n')).toEqual([
      '| Label | Kind | Waiting | Link | Item |',
      '| --- | --- | --- | --- | --- |',
      '| D157 | DECIDE | unknown | https://example.com/x | a \\| b? (recommend a) |',
      '| A116 | ACTION | 1d 2h | #12 | add the disks to the pool |',
      '| D141 (ledger) | DECIDE | 2h 0m, revised 2026-10-21 | none | btrfs or zfs? (recommend zfs) |',
    ])
    expect(inventory([item('D9', 'x? (recommend y) [no link]', { status: 'completed' })], NOW)).toBe('No open decisions or actions.')
  })

  test("mentions are the person's own lines that name a label, the newest three", () => {
    const found = mentions(
      [
        user('D3: go with zfs'),
        { role: 'assistant', text: 'D3 is settled, I think' },
        user('A116: done\nand D31 is something else, as is D3.js'),
        // The plugin's own reminder, its prompts and a compaction summary aren't the person's words.
        user("Open items in this session's decision queue, still waiting on the user:\n- D3 DECIDE: x? (recommend y) [no link]"),
        user(`${RECONCILE}\n\n| D3 | DECIDE | 1h | none | x |\n- D3: "D3: go with zfs"`),
        user('This session is being continued from a previous conversation. D3 was answered.'),
        // A line that only repeats the queue isn't an answer.
        user('D3 DECIDE: x? (recommend y) [no link]'),
        user('A2: done'),
        user('re D3, raidz2 after all'),
        user('D3 again'),
        user('D3 last'),
      ],
      ['D3', 'A116', 'D?'],
    )
    expect(found.get('D3')).toEqual(['re D3, raidz2 after all', 'D3 again', 'D3 last'])
    expect(found.get('A116')).toEqual(['A116: done'])
    expect(found.has('D?')).toBe(false)
  })

  test('the brief lists the open items, then what the person wrote about them', () => {
    const items = [
      item('D3', 'zfs? (recommend yes) [no link]', { created: NOW - HOUR }),
      item('A1', 'log in [no link]', { created: NOW }),
    ]
    const text = brief(items, [user('D3: go with yes'), user('A1: done')], NOW)
    expect(text).toContain('Open items (2):')
    expect(text).toContain('| D3 | DECIDE | 1h 0m | none | zfs? (recommend yes) |')
    expect(text).toContain(`Lines of the user's messages that name them, oldest first:\n- D3: "D3: go with yes"\n- A1: "A1: done"`)
    expect(brief(items, [], NOW)).toContain("The user's messages name none of these labels; look for answers by subject.")
    expect(brief([], [], NOW)).toBe('No open decisions or actions.')
  })
})
