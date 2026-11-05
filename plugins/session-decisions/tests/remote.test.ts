import { describe, expect, test } from 'claude-code/testing'

import { PUSH_MAX, TEXT_WIDTH, parseListMode, pushText, replyLine, replyNote, textListing, wrap } from '../hooks/remote'
import type { Item } from '../types'

const NOW = Date.UTC(2026, 10, 5, 12)
const HOUR = 60 * 60 * 1000
const FORGE = { kind: 'github' as const, host: 'github.com', repo: 'lokkju/skills' }

function item(label: string, body: string, extra: Partial<Item> = {}): Item {
  return {
    id: label,
    source: 'tasks',
    label,
    kind: label.startsWith('D') ? 'DECIDE' : 'ACTION',
    body,
    status: 'pending',
    description: '',
    created: NOW - 2 * HOUR,
    ...extra,
  }
}

const D1 = item('D1', 'Move the session store from SQLite to Postgres before the beta, or wait until after? (recommend Postgres now, while the schema is small) [#52]')
const A1 = item('A1', 'put the client secret in Vault so the build can restart [no link]', { created: NOW - 5 * 60 * 1000 })

describe('text listing', () => {
  test('one short block per item: label, kind, age, question, recommendation and link', () => {
    const text = textListing([A1, D1], NOW, { forge: FORGE })
    expect(text).toBe(
      [
        'Open: 1 decision, 1 action',
        '',
        'D1 · decision · 2h 0m',
        '  Move the session store from SQLite to Postgres before the beta, or',
        '  wait until after?',
        '  Recommend: Postgres now, while the schema is small',
        '  Link: https://github.com/lokkju/skills/issues/52',
        '',
        'A1 · action · 5m',
        '  put the client secret in Vault so the build can restart',
        '',
        'Answer by typing "D1: go", "D1: <your answer>", "A1: done"; one message',
        'can answer several.',
      ].join('\n'),
    )
    for (const line of text.split('\n')) expect(line.length).toBeLessThanOrEqual(TEXT_WIDTH)
  })

  test('a narrow width wraps every line to it; no width leaves wrapping to the surface', () => {
    for (const line of textListing([D1, A1], NOW, { width: 40 }).split('\n')) expect(line.length).toBeLessThanOrEqual(40)
    const loose = textListing([D1], NOW, { width: null })
    expect(loose).toContain('  Move the session store from SQLite to Postgres before the beta, or wait until after?')
    // Without a forge, a short reference stays as written.
    expect(loose).toContain('  Link: #52')
  })

  test('all adds settled items with their ruling, and says what an item replaces', () => {
    const settled = item('D2', 'keep SQLite? (recommend yes) [no link]', {
      status: 'completed',
      description: 'Ruling (2026-11-04): **yes**, for now\nSuperseded (2026-11-05) by D1: the beta needs concurrent writers',
    })
    const ruled = item('A2', 'log in to npm [no link]', { status: 'completed', description: 'Ruling (2026-11-04): done' })
    const text = textListing([D1, settled, ruled], NOW, { all: true })
    expect(text).toContain('D1 · decision · 2h 0m')
    expect(text).toContain('  Replaces D2')
    expect(text).toContain('Settled: 2')
    expect(text).toContain('D2 · decision · superseded')
    expect(text).toContain('  Superseded 2026-11-05 by D1: the beta needs concurrent writers')
    expect(text).toContain('A2 · action · settled\n  log in to npm\n  Ruling 2026-11-04: done')
    expect(textListing([settled], NOW)).toBe('No open decisions or actions.')
    expect(textListing([], NOW, { all: true })).toBe('No decisions or actions this session.')
  })

  test('the answer hint names only the kinds that are open', () => {
    expect(textListing([A1], NOW)).toContain('Answer by typing "A1: done"; one message can answer several.')
    expect(textListing([A1], NOW)).toContain('Open: 1 action')
  })

  test('wrap keeps a word longer than the width on a line of its own', () => {
    expect(wrap('see https://example.com/a/very/long/path/that/does/not/fit here', 20, '  ')).toEqual([
      '  see',
      '  https://example.com/a/very/long/path/that/does/not/fit',
      '  here',
    ])
    expect(wrap('', 20)).toEqual([])
  })
})

describe('reply list', () => {
  test('one line per open item, with the short recommendation', () => {
    expect(replyLine(D1)).toBe('- D1: Move the session store from SQLite to Postgres before the beta, or wait until after? (recommend Postgres now)')
    expect(replyLine(A1)).toBe('- A1: put the client secret in Vault so the build can restart')
  })

  test('the note says why the list is on and lists what is open', () => {
    const settled = item('D2', 'keep SQLite? (recommend yes) [no link]', { status: 'completed' })
    const note = replyNote([D1, A1, settled], 'remote')
    expect(note).toContain('Remote Control')
    expect(note).toContain('`Open items` list')
    expect(note).toContain('Open right now:\n- D1: ')
    expect(note).toContain('\n- A1: put the client secret')
    expect(note).not.toContain('D2')
    expect(replyNote([], 'asked')).toContain('turned on the reply list')
    expect(replyNote([], 'asked')).toContain('No items are open right now.')
  })

  test('the reply_list option reads as a mode, auto for anything else', () => {
    expect(parseListMode('always')).toBe('always')
    expect(parseListMode(' Never ')).toBe('never')
    expect(parseListMode('auto')).toBe('auto')
    expect(parseListMode(undefined)).toBe('auto')
    expect(parseListMode('sometimes')).toBe('auto')
  })
})

describe('push text', () => {
  test('label, kind, question and recommendation, cut to fit', () => {
    expect(pushText(A1)).toBe('A1 action: put the client secret in Vault so the build can restart')
    expect(pushText(item('D3', 'retire `kapp`? (recommend yes) [no link]'))).toBe('D3 decide: retire kapp? Recommend: yes')
    const long = pushText(item('D4', `${'why '.repeat(80)}? (recommend the second option, since it keeps the schema) [no link]`))
    expect(long.length).toBeLessThanOrEqual(PUSH_MAX)
    expect(long.startsWith('D4 decide: why why')).toBe(true)
    expect(long).toContain('… Recommend: the second option')
  })
})
