import { describe, expect, test } from 'claude-code/testing'

import { detailsExtras, hasMore, moreSplit, recommendationCut } from '../hooks/more'
import { splitBody } from '../hooks/subject'

const GITHUB = { kind: 'github', host: 'github.com', repo: 'lokkju/skills' } as const
const item = (body: string, description = '') => ({ parts: splitBody(body), description })

describe('hasMore', () => {
  test('a bare item has nothing more than its card', () => {
    expect(hasMore(item('Keep SQLite for tests? (recommend yes) [#13]'), null)).toBe(false)
    expect(hasMore(item('log in [no link]'), null)).toBe(false)
    expect(hasMore(item('log in [no link]', '  \n\n '), null)).toBe(false)
    // A qualifier the card draws in full is no reason.
    expect(hasMore(item('Which driver? (recommend psycopg 3, for async) [#5]'), null)).toBe(false)
    // A long choice is cut on the tag, but the qualifier line then carries all of it.
    expect(hasMore(item('Which plan? (recommend the incremental migration with a dual-write window) [#5]'), null)).toBe(false)
  })

  test('a description is more', () => {
    expect(hasMore(item('Keep SQLite for tests? (recommend yes) [#13]', 'It runs in memory.'), null)).toBe(true)
  })

  test('a link the details view spells out as a URL is more', () => {
    // A short reference resolved on the forge, which the card shows only as #13.
    expect(hasMore(item('Keep SQLite? (recommend yes) [#13]'), GITHUB)).toBe(true)
    // A URL: the card draws it on its own line, and the details view offers to copy it.
    expect(hasMore(item('read the runbook [https://example.com/runbook]'), null)).toBe(true)
    expect(hasMore(item('review it [https://github.com/lokkju/skills/pull/20]'), null)).toBe(true)
    // A reference no forge resolves gives the details view nothing to add.
    expect(hasMore(item('see it [gitlab/group/proj#4]'), null)).toBe(false)
  })

  test('a command the details view offers to copy is more', () => {
    expect(hasMore(item('run `uv sync` first? (recommend yes) [#2]'), null)).toBe(true)
  })

  test('a recommendation counts as cut only when no line of the card carries all of it', () => {
    // A choice too long for the tag puts the whole recommendation on the qualifier line, and a
    // shorter one leaves the qualifier the rest, so today's card always shows all of it.
    expect(recommendationCut('psycopg 3, for async')).toBe(false)
    expect(recommendationCut('the incremental migration with a dual-write window')).toBe(false)
    expect(recommendationCut('yes')).toBe(false)
  })
})

describe('detailsExtras', () => {
  test('the URL and the first command the details view offers', () => {
    expect(detailsExtras(splitBody('ship? (recommend yes) [#3]'), 'Run `make` then `make test`.', GITHUB)).toEqual({
      url: 'https://github.com/lokkju/skills/issues/3',
      code: 'make',
    })
    expect(detailsExtras(splitBody('ship? (recommend yes) [#3]'), '', null)).toEqual({ url: null, code: null })
    expect(detailsExtras(splitBody('open [http://grafana.lab/d/x]'), '', null).url).toBe('http://grafana.lab/d/x')
  })
})

describe('moreSplit', () => {
  test('a line that leaves room on its one row keeps [more] beside it', () => {
    expect(moreSplit('Keep SQLite for tests?', 48)).toEqual({ head: '', tail: 'Keep SQLite for tests?' })
    // 41 characters, a space and [more] fill 48 columns exactly.
    expect(moreSplit('a'.repeat(20) + ' ' + 'b'.repeat(20), 48)).toEqual({ head: '', tail: `${'a'.repeat(20)} ${'b'.repeat(20)}` })
    expect(moreSplit('a'.repeat(20) + ' ' + 'b'.repeat(21), 48)).toBeNull()
  })

  test('a wrapped line splits so [more] follows its last row', () => {
    const line = 'Run the suite on Postgres in CI as well as on SQLite locally?'
    expect(moreSplit(line, 48)).toEqual({ head: 'Run the suite on Postgres in CI as well as on', tail: 'SQLite locally?' })
    // Markdown survives the split when no span crosses it, and counts at its drawn width.
    expect(moreSplit('Run the **suite** on Postgres in CI as well as on `SQLite` locally?', 48)).toEqual({
      head: 'Run the **suite** on Postgres in CI as well as on',
      tail: '`SQLite` locally?',
    })
  })

  test('a row of its own when the text fills the last row, or the split is unsafe', () => {
    // The last row is full, so [more] can't sit beside it.
    expect(moreSplit('one two three four five sixsix', 16)).toBeNull()
    // A span of emphasis across the split.
    expect(moreSplit('Run the suite on Postgres in CI as well as **on SQLite locally?**', 48)).toBeNull()
    // A link, whose drawn width depends on the terminal; non-ASCII; a word longer than a row.
    expect(moreSplit('see [the guide](https://example.com)', 48)).toBeNull()
    expect(moreSplit('Keep SQLite für tests?', 48)).toBeNull()
    expect(moreSplit('x'.repeat(60), 48)).toBeNull()
    // An unknown or tiny width.
    expect(moreSplit('Keep SQLite?', null)).toBeNull()
    expect(moreSplit('Keep SQLite?', Number.NaN)).toBeNull()
    expect(moreSplit('Keep SQLite?', 6)).toBeNull()
  })
})
