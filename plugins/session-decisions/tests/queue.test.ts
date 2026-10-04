import { describe, expect, test } from 'claude-code/testing'

import { age, applyUpdate, itemFromTask, listing, mergeAnswers, nextNumber, reminder } from '../hooks/queue'
import { parseDraft, parseLabel, problems, recommendation, rulingIn, shortRecommendation, splitBody } from '../hooks/subject'
import type { Item } from '../types'

const item = (over: Partial<Item>): Item => ({
  id: '1',
  source: 'tasks',
  label: 'D1',
  kind: 'DECIDE',
  body: 'ship it? (recommend yes) [no link]',
  status: 'pending',
  description: '',
  ...over,
})

describe('subject', () => {
  test('a draft needs the kind keyword in capitals', () => {
    expect(parseDraft('DECIDE: ship it? (recommend yes) [#1]')).toEqual({ kind: 'DECIDE', body: 'ship it? (recommend yes) [#1]' })
    expect(parseDraft('D4 ACTION: log in [no link]')).toEqual({ kind: 'ACTION', body: 'log in [no link]' })
    expect(parseDraft('Action: refactor the parser')).toBeNull()
  })

  test('problems name the missing link and recommendation', () => {
    expect(problems({ kind: 'DECIDE', body: 'ship it?' })).toHaveLength(2)
    expect(problems({ kind: 'ACTION', body: 'log in [https://example.com]' })).toEqual([])
    expect(problems({ kind: 'ACTION', body: '' })).toEqual(['write the question or action after the colon'])
  })

  test('labels, recommendations and rulings parse', () => {
    expect(parseLabel('A12 ACTION: run gh auth login [no link]')).toEqual({ letter: 'A', number: 12, kind: 'ACTION', body: 'run gh auth login [no link]' })
    expect(parseLabel('DECIDE: no label yet')).toBeNull()
    expect(recommendation('Postgres or SQLite? (recommend SQLite) [#3]')).toBe('SQLite')
    expect(rulingIn('context\nRuling (2026-10-01): no\nRuling (2026-10-03): yes')).toBe('Ruling (2026-10-03): yes')

  })

  test('a recommendation shortens to a tag and a qualifier', () => {
    expect(shortRecommendation('SQLite')).toEqual({ choice: 'SQLite', qualifier: null })
    expect(shortRecommendation("yes, after you've tried the dev panel; solo repo")).toEqual({ choice: 'yes', qualifier: "after you've tried the dev panel; solo repo" })
    expect(shortRecommendation('the managed Postgres tier with read replicas')).toEqual({ choice: 'the managed Postgres ti…', qualifier: 'the managed Postgres tier with read replicas' })
  })

  test('a body splits into question, recommendation and link for a card', () => {
    expect(splitBody('Postgres or SQLite? (recommend SQLite) [#3]')).toEqual({ question: 'Postgres or SQLite?', recommendation: 'SQLite', link: '#3' })
    expect(splitBody('Merge it (recommend yes, after a test; solo repo) and push [no link]')).toEqual({ question: 'Merge it and push', recommendation: 'yes, after a test; solo repo', link: null })
    expect(splitBody('run gh auth login [https://example.com/x]')).toEqual({ question: 'run gh auth login', recommendation: null, link: 'https://example.com/x' })
  })
})

describe('queue', () => {
  test('numbering continues past the counter and any label in use', () => {
    expect(nextNumber({ D: 2, A: 0 }, [item({ label: 'D5' })], 'DECIDE')).toBe(6)
    expect(nextNumber({ D: 2, A: 0 }, [item({ label: 'D5' })], 'ACTION')).toBe(1)
  })

  test('an unlabelled decision task shows as D?', () => {
    expect(itemFromTask({ id: '7', subject: 'DECIDE: x (recommend y) [no link]' })?.label).toBe('D?')
    expect(itemFromTask({ id: '7', subject: 'Fix the build' })).toBeNull()
  })

  test('updates complete, relabel and delete items', () => {
    const list = [item({})]
    const done = applyUpdate(list, { taskId: '1', status: 'completed', description: 'Ruling (2026-10-03): yes' })
    expect(done[0]).toMatchObject({ status: 'completed', description: 'Ruling (2026-10-03): yes' })
    expect(applyUpdate(list, { taskId: '1', status: 'deleted' })).toEqual([])
    expect(applyUpdate(list, { taskId: '1', subject: 'Just a task now' })).toEqual([])
    expect(applyUpdate(list, { taskId: '9', status: 'completed' })).toEqual(list)
  })

  test('listing and reminder show what is open', () => {
    const list = [item({ created: 0 + 1 }), item({ id: '2', label: 'A1', kind: 'ACTION', body: 'log in [no link]', status: 'completed', description: 'Ruling (2026-10-03): done' })]
    expect(listing(list, 1 + 90 * 60_000)).toBe('D1 DECIDE: ship it? (recommend yes) [no link]  (waiting 1h 30m)')
    expect(listing(list, 0, true)).toContain('    Ruling (2026-10-03): done')
    expect(reminder(list)).toContain('- D1 DECIDE:')
    expect(reminder([])).toBeNull()
    expect(age(undefined, 5)).toBe('')
  })

  test('answers merge into the draft one line per label', () => {
    expect(mergeAnswers('', ['D1: go with yes'])).toBe('D1: go with yes')
    expect(mergeAnswers('D1: go with yes', ['A2: done'])).toBe('D1: go with yes\nA2: done')
    expect(mergeAnswers('D1: go with yes\nA2: done\n', ['D1: '])).toBe('D1: \nA2: done')
    expect(mergeAnswers('also, rebase first', ['D1: go with yes', 'D2: go with no'])).toBe('also, rebase first\nD1: go with yes\nD2: go with no')
    expect(mergeAnswers('D10: go with x', ['D1: go with y'])).toBe('D10: go with x\nD1: go with y')
  })
})
