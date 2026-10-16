import { describe, expect, test } from 'claude-code/testing'

import { handoffs, nudge } from '../hooks/handoff'
import type { Item } from '../types'

const item = (over: Partial<Item>): Item => ({
  id: '1',
  source: 'tasks',
  label: 'A1',
  kind: 'ACTION',
  body: 'merge #12 [#12]',
  status: 'pending',
  description: '',
  ...over,
})

describe('hand-offs without labels', () => {
  test('the items under a hand-off heading', () => {
    const reply = [
      'Both fixes are pushed and CI is green.',
      '',
      'Left for you:',
      '- Merge #41 and #42',
      '- Run `mise run deploy` on each host',
      '',
      'That covers the release.',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual(['Merge #41 and #42', 'Run mise run deploy on each host'])
  })

  test('headings in their other forms', () => {
    expect(handoffs('**Next steps for you:**\n1. Approve the release PR', [])).toEqual(['Approve the release PR'])
    expect(handoffs('## What you need to do\n\n- rotate the staging key\n- restart nginx', [])).toEqual([
      'rotate the staging key',
      'restart nginx',
    ])
    expect(handoffs("You'll need to:\n* log in to the registry", [])).toEqual(['log in to the registry'])
  })

  test('a line that hands a step over by itself', () => {
    expect(handoffs("The branch is ready. You'll need to merge it once CI passes.", [])).toHaveLength(1)
    expect(handoffs('I can’t reach prod, so this one is left for you.', [])).toHaveLength(1)
    expect(handoffs('Whether to drop the v1 endpoint is your call.', [])).toHaveLength(1)
    expect(handoffs('Run these on each host:', [])).toHaveLength(1)
    expect(handoffs('Please merge #12 when you have a moment.', [])).toEqual(['Please merge #12 when you have a moment.'])
  })

  test('at most five lines come back', () => {
    const reply = ['Left for you:', ...[1, 2, 3, 4, 5, 6, 7].map(n => `- step ${n}`)].join('\n')
    expect(handoffs(reply, [])).toHaveLength(5)
  })
})

describe('hand-offs that are queued', () => {
  test('a line that names a label', () => {
    const reply = 'Left for you:\n- A1: merge #41\n- D2 is the cutover date\n- (A3) run the backfill on each host'
    expect(handoffs(reply, [])).toEqual([])
    expect(handoffs("You'll need to merge #12 (A1).", [])).toEqual([])
  })

  test('a line an item in the queue already covers, by reference or by its words', () => {
    const queue = [
      item({ body: 'merge #41 [#41]' }),
      item({ id: '2', label: 'A2', body: 'rotate the staging deploy key [no link]' }),
      item({ id: '3', label: 'A3', body: 'review the docs [https://example.com/pr/9]' }),
    ]
    const reply = [
      'Left for you:',
      '- Merge #41',
      '- Rotate the staging deploy key',
      '- Look over https://example.com/pr/9',
      '- Restart the worker on box3',
    ].join('\n')
    expect(handoffs(reply, queue)).toEqual(['Restart the worker on box3'])
  })
})

describe('prose that only resembles a hand-off', () => {
  test('conditional, negated and questioning phrasing', () => {
    expect(handoffs("If you need to roll back, run `git revert HEAD`.", [])).toEqual([])
    expect(handoffs("You won't need to restart anything; the config reloads itself.", [])).toEqual([])
    expect(handoffs("You don't need to merge anything by hand.", [])).toEqual([])
    expect(handoffs('Should you need to rotate the key later, the script is in bin/.', [])).toEqual([])
    expect(handoffs('Do you want me to merge it, or will you need to review it first?', [])).toEqual([])
    expect(handoffs("It isn't your call alone; the team decides.", [])).toEqual([])
  })

  test('code blocks and quotes', () => {
    const reply = [
      'The README now says:',
      '',
      "> You'll need to run `make install` on each host.",
      '',
      '```',
      'Left for you:',
      "- you'll need to merge this",
      '```',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual([])
  })

  test("the assistant's own plan, explanations and summaries", () => {
    const reply = [
      'Done. I merged #41, ran the migrations and restarted the worker.',
      '',
      'Next steps:',
      '- I will write the changelog entry',
      '- then tag the release',
      '',
      'You can run `mise run test` to check it yourself, and the logs are on your side of the VPN.',
      'To deploy, the workflow runs `kubectl apply` for you.',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual([])
  })

  test('a hand-off heading word in an ordinary sentence opens no section', () => {
    expect(handoffs('Nothing is left for the cleanup job.\n- it runs nightly', [])).toEqual([])
    expect(handoffs('For you to compare, the two plans are:\n- A: one host\n- B: two hosts', [])).toEqual([])
  })
})

test('the nudge quotes the lines and says to ignore a false match', () => {
  const text = nudge(['Merge #41'])
  expect(text).toContain('- Merge #41')
  expect(text).toContain('TaskCreate')
  expect(text).toContain('Ignore a line that is not a hand-off')
})
