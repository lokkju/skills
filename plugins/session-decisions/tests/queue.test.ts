import { describe, expect, test } from 'claude-code/testing'

import { age, applyUpdate, itemFromTask, listing, mergeAnswers, mergeHydrated, nextNumber, reminder, retitle, revise, settle } from '../hooks/queue'
import { parseDraft, parseLabel, problems, recommendation, revisedIn, rulingIn, shortRecommendation, splitBody } from '../hooks/subject'
import { codeSpans, descriptionText, githubShort, isShortLink, linkHref, plainText } from '../hooks/markdown'
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
    expect(recommendation('Which plan? (recommend B (the cheaper one)) [no link]')).toBe('B (the cheaper one)')
    expect(splitBody('Which plan? (recommend B (cheaper)) [no link]').question).toBe('Which plan?')
    expect(rulingIn('context\nRuling (2026-10-01): no\nRuling (2026-10-03): yes')).toBe('Ruling (2026-10-03): yes')

  })

  test('a recommendation may have a colon and nested parentheses at any depth', () => {
    const body =
      'what to work on next in lokkju/skills? (recommend: ship release #16 now, then a session-decisions hardening release (#14 rest, #4 skill wording, #20, triage #15 and #13), then settle #10 and write the forge-release skill #11, then forge-tracker) [https://github.com/lokkju/skills/issues]'
    const choice =
      'ship release #16 now, then a session-decisions hardening release (#14 rest, #4 skill wording, #20, triage #15 and #13), then settle #10 and write the forge-release skill #11, then forge-tracker'
    expect(problems({ kind: 'DECIDE', body })).toEqual([])
    expect(problems({ kind: 'DECIDE', body: body.replace('(recommend: ', '(recommend ') })).toEqual([])
    expect(recommendation(body)).toBe(choice)
    expect(splitBody(body)).toEqual({ question: 'what to work on next in lokkju/skills?', recommendation: choice, link: 'https://github.com/lokkju/skills/issues' })
    expect(shortRecommendation(choice)).toEqual({ choice: 'ship release #16 now', qualifier: choice.slice('ship release #16 now, '.length) })
    expect(recommendation('Which? (recommend B (cheaper, (mostly))) [no link]')).toBe('B (cheaper, (mostly))')
    expect(splitBody('Which? (recommend B (x (y))) and when [no link]').question).toBe('Which? and when')
    expect(shortRecommendation('B (cheaper, faster); later')).toEqual({ choice: 'B (cheaper, faster)', qualifier: 'later' })
    // Not a recommendation: an empty one, "recommendation", or one never closed (still accepted, with no Accept choice).
    expect(problems({ kind: 'DECIDE', body: 'ship? (recommend ) [no link]' })).toHaveLength(1)
    expect(problems({ kind: 'DECIDE', body: 'ship? (recommendation yes) [no link]' })).toHaveLength(1)
    expect(problems({ kind: 'DECIDE', body: 'ship? (recommend yes [no link]' })).toEqual([])
    expect(recommendation('ship? (recommend yes [no link]')).toBeNull()
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
    // An update to a task that isn't a decision item leaves the list as it was, so nothing is persisted.
    expect(applyUpdate(list, { taskId: '9', status: 'completed' })).toBe(list)
    expect(applyUpdate(list, { taskId: '9', status: 'deleted' })).toBe(list)
    expect(applyUpdate(list, { taskId: '9', subject: 'refactor the parser' })).toBe(list)
    expect(applyUpdate(list, { taskId: '1', subject: 'Just a task now' })).toEqual([])
    expect(applyUpdate(list, { taskId: '9', status: 'completed' })).toEqual(list)
  })

  test('a retitle keeps the label, checks a typed one, and labels a new item', () => {
    const list = [item({}), item({ id: '2', label: 'A1', kind: 'ACTION', body: 'log in [no link]' })]
    // Retitle without a label keeps the item's label.
    expect(retitle(list, '1', 'DECIDE: ship it today? (recommend yes) [#4]')).toEqual({ kind: 'subject', subject: 'D1 DECIDE: ship it today? (recommend yes) [#4]' })
    // Retitle with the matching label goes through as written.
    expect(retitle(list, '1', 'D1 DECIDE: ship it today? (recommend yes) [#4]')).toEqual({ kind: 'subject', subject: 'D1 DECIDE: ship it today? (recommend yes) [#4]' })
    // A different label is refused, whether free or another item's.
    expect(retitle(list, '1', 'D7 DECIDE: ship it today? (recommend yes) [#4]')).toEqual({ kind: 'deny', reason: 'task 1 is D1; keep its label or drop it and the hook adds it' })
    expect(retitle(list, '1', 'A1 ACTION: ship it [#4]')).toMatchObject({ kind: 'deny' })
    // A revised DECIDE still needs its recommendation and link.
    expect(retitle(list, '1', 'DECIDE: ship it today? [#4]')).toEqual({ kind: 'deny', reason: 'a DECIDE item needs a recommendation, written as (recommend <choice>)' })
    expect(retitle(list, '2', 'ACTION: log in again')).toMatchObject({ kind: 'deny' })
    // A task that wasn't an item, an unlabelled one, or a change of kind gets a new label.
    expect(retitle(list, '9', 'ACTION: rotate the key [no link]')).toEqual({ kind: 'assign', draft: { kind: 'ACTION', body: 'rotate the key [no link]' } })
    expect(retitle([item({ label: 'D?' })], '1', 'DECIDE: x? (recommend y) [no link]')).toMatchObject({ kind: 'assign' })
    expect(retitle(list, '1', 'ACTION: ship it [#4]')).toMatchObject({ kind: 'assign' })
    // A typed label on a new item is taken if it's free and fits the kind.
    expect(retitle(list, '9', 'D5 DECIDE: x? (recommend y) [no link]')).toMatchObject({ kind: 'subject' })
    expect(retitle(list, '9', 'A1 ACTION: x [no link]')).toMatchObject({ kind: 'deny' })
    expect(retitle(list, '9', 'A5 DECIDE: x? (recommend y) [no link]')).toMatchObject({ kind: 'deny' })
    // Anything else passes through.
    expect(retitle(list, '1', 'Just a task now')).toEqual({ kind: 'pass' })
  })

  test('a re-read merges task files with the stored ledger and rulings', () => {
    const file = item({ id: '3', label: 'A1', kind: 'ACTION', body: 'log in [no link]', description: 'context' })
    const ruled = settle(file, 'done', '2026-10-03')
    expect(ruled).toMatchObject({ status: 'completed', description: 'context\nRuling (2026-10-03): done' })
    const kept = item({ id: 'A2', source: 'ledger', label: 'A2', kind: 'ACTION', body: 'approve [no link]' })
    const stored = item({ id: 'A3', source: 'ledger', label: 'A3', kind: 'ACTION', body: 'sign [no link]' })
    const gone = settle(item({ id: '8', label: 'D4' }), 'no', '2026-10-02')
    const merged = mergeHydrated([file, item({})], [kept], { ledger: [stored, { ...kept, status: 'completed' }], closed: { '3': ruled, '8': gone } })
    // The stored ruling wins over a task file still showing the item open.
    expect(merged.find(one => one.id === '3')).toMatchObject({ status: 'completed', description: 'context\nRuling (2026-10-03): done' })
    // A settled item whose task file has gone stays settled; ledger items from state win over the store.
    expect(merged.map(one => `${one.label}:${one.status}`)).toEqual(['A1:completed', 'D1:pending', 'D4:completed', 'A2:pending', 'A3:pending'])
    // A task file that already records a ruling keeps its own.
    const fileRuled = { ...file, status: 'completed' as const, description: 'Ruling (2026-10-04): later' }
    expect(mergeHydrated([fileRuled], [], { ledger: [], closed: { '3': ruled } })[0]?.description).toBe('Ruling (2026-10-04): later')
    // Unreadable task files fall back to what's in state.
    expect(mergeHydrated(null, [item({ id: '5' })], { ledger: [], closed: {} }).map(one => one.id)).toEqual(['5'])
  })

  test('listing and reminder show what is open', () => {
    const list = [item({ created: 0 + 1 }), item({ id: '2', label: 'A1', kind: 'ACTION', body: 'log in [no link]', status: 'completed', description: 'Ruling (2026-10-03): done' })]
    expect(listing(list, 1 + 90 * 60_000)).toBe('D1 DECIDE: ship it? (recommend yes) [no link]  (waiting 1h 30m)')
    expect(listing(list, 0, true)).toContain('    Ruling (2026-10-03): done')
    expect(reminder(list)).toContain('- D1 DECIDE:')
    expect(reminder([])).toBeNull()
    expect(age(undefined, 5)).toBe('')
  })

  test('a revision appends a dated Revised line and keeps what was shown', () => {
    const before = item({ description: 'context' })
    const changed = revise(before, { body: 'ship it? (recommend no) [no link]', note: 'the canary failed' }, '2026-10-14')
    expect(changed).toMatchObject({ label: 'D1', status: 'pending', body: 'ship it? (recommend no) [no link]' })
    expect(changed.description).toBe('context\nRevised (2026-10-14): the canary failed (was: ship it? (recommend yes) [no link])')
    expect(revise(item({}), { note: 'more context' }, '2026-10-14').description).toBe('Revised (2026-10-14): more context')
    expect(revisedIn(changed.description)).toBe('2026-10-14')
    expect(revisedIn('Revised (2026-10-01): a\nRevised (2026-10-02): b')).toBe('2026-10-02')
    expect(revisedIn('context')).toBeNull()
    // /decisions tags a revised open item with the date.
    expect(listing([{ ...changed, created: 1 }], 1)).toBe('D1 DECIDE: ship it? (recommend no) [no link]  (waiting 0m, revised 2026-10-14)')
  })

  test('answers merge into the draft one line per label', () => {
    expect(mergeAnswers('', ['D1: go with yes'])).toBe('D1: go with yes')
    expect(mergeAnswers('D1: go with yes', ['A2: done'])).toBe('D1: go with yes\nA2: done')
    expect(mergeAnswers('D1: go with yes\nA2: done\n', ['D1: '])).toBe('D1: \nA2: done')
    expect(mergeAnswers('also, rebase first', ['D1: go with yes', 'D2: go with no'])).toBe('also, rebase first\nD1: go with yes\nD2: go with no')
    expect(mergeAnswers('D10: go with x', ['D1: go with y'])).toBe('D10: go with x\nD1: go with y')
  })
})

describe('markdown', () => {
  test('plainText drops emphasis, backticks and link syntax', () => {
    expect(plainText('run **`mise run core`** or read [the guide](https://example.com/a_b) first?')).toBe(
      'run mise run core or read the guide first?',
    )
    expect(plainText('__bold__, *it*, _it_ and ~~gone~~')).toBe('bold, it, it and gone')
    expect(plainText('keep snake_case_names, 2 * 3 * 4 and `a_b*c*`')).toBe('keep snake_case_names, 2 * 3 * 4 and a_b*c*')
    expect(plainText('``code with ` inside``')).toBe('code with ` inside')
    expect(plainText('see:\n```sh\nmake test\n```')).toBe('see:\nmake test')
  })

  test('codeSpans lists fenced blocks then inline code, once each', () => {
    expect(codeSpans('run `a` then `b`, or `a`\n```\nc d\n```')).toEqual(['c d', 'a', 'b'])
    expect(codeSpans('no code here')).toEqual([])
  })

  test('linkHref accepts https URLs a Link can draw and nothing else', () => {
    expect(linkHref('https://github.com/lokkju/skills/issues/20')).toBe('https://github.com/lokkju/skills/issues/20')
    expect(linkHref('https://example.com')).toBe('https://example.com/')
    expect(linkHref('http://localhost:8080/x')).toBe('http://localhost:8080/x')
    expect(linkHref('https://example.com/a b')).toBe('https://example.com/a%20b')
    for (const token of [null, '#12', 'owner/repo#3', '!45', 'http://example.com', 'https://me@example.com', 'ftp://x']) {
      expect(linkHref(token)).toBeNull()
    }
  })

  test('isShortLink and githubShort pick what a card shows for a link', () => {
    for (const token of ['#12', '!45', 'owner/repo#3', 'lokkju/skills.v2#20']) expect(isShortLink(token)).toBe(true)
    for (const token of ['https://x.dev', 'owner/repo', '#x', 'a/b/c#1']) expect(isShortLink(token)).toBe(false)
    expect(githubShort('https://github.com/lokkju/skills/issues/20')).toBe('lokkju/skills#20')
    expect(githubShort('https://github.com/lokkju/skills/pull/33/files#diff-1')).toBe('lokkju/skills#33')
    expect(githubShort('https://github.com/lokkju/skills/issues/20#issuecomment-1')).toBe('lokkju/skills#20')
    expect(githubShort('https://github.com/lokkju/skills')).toBe('lokkju/skills')
    expect(githubShort('https://github.com/lokkju/skills.git')).toBe('lokkju/skills')
    for (const href of ['https://github.com/lokkju', 'https://github.com/orgs/lokkju/projects', 'https://github.com/lokkju/skills/blob/main/README.md', 'https://gitlab.com/a/b/-/issues/1', 'not a url']) {
      expect(githubShort(href)).toBeNull()
    }
  })

  test('descriptionText keeps paragraphs and list items as plain text', () => {
    expect(descriptionText('Needs **`sudo`**.\n\n\n\nThen run it.')).toBe('Needs sudo.\n\nThen run it.')
    expect(descriptionText('')).toBe('')
    expect(descriptionText('\n\n  \nOne.\n\n')).toBe('One.')
    expect(descriptionText('## Options\nPick _one_:\n* Redis, `REDIS_URL`\n+ Memcached\n  - nested\n\n1. Pick.\n2) Ship.')).toBe(
      'Options\nPick one:\n- Redis, REDIS_URL\n- Memcached\n  - nested\n\n1. Pick.\n2) Ship.',
    )
    expect(descriptionText('See [the runbook](https://x.dev/run) or [https://x.dev](https://x.dev), <https://y.dev>.')).toBe(
      'See the runbook (https://x.dev/run) or https://x.dev, https://y.dev.',
    )
    // A fence keeps its lines as written, bullets and markers included.
    expect(descriptionText('Run:\n```sh\n* not a bullet **x**\n```\nDone.')).toBe('Run:\n* not a bullet **x**\nDone.')
    // plainText for one-line rows still drops the url.
    expect(plainText('[the runbook](https://x.dev/run)')).toBe('the runbook')
  })
})

describe('link and label forms', () => {
  test('only the listed link forms count as the link', () => {
    for (const link of ['#12', '!45', 'lokkju/skills#13', 'group/sub/repo!7', 'https://example.com/a?b=1', 'http://x.test', 'no link', 'No Link', ' #3 ']) {
      expect(problems({ kind: 'ACTION', body: `log in [${link}]` })).toEqual([])
    }
    for (const body of ['press [Enter]', 'log in [see above]', 'log in [12]', 'log in [ftp://x.test]', 'log in [#12] now', 'log in []']) {
      expect(problems({ kind: 'ACTION', body })).toEqual([
        'end the subject with one link token: [#123], [!45], [owner/repo#123], [<url>] or [no link]',
      ])
    }
  })

  test('the link token splits off a body', () => {
    expect(splitBody('press [Enter] to confirm [#4]')).toEqual({ question: 'press [Enter] to confirm', recommendation: null, link: '#4' })
    expect(splitBody('rotate the key [ lokkju/skills#13 ]').link).toBe('lokkju/skills#13')
    expect(splitBody('rotate the key [No Link]')).toEqual({ question: 'rotate the key', recommendation: null, link: null })
    expect(splitBody('press [Enter]')).toEqual({ question: 'press [Enter]', recommendation: null, link: null })
  })

  test('self-numbered forms parse as drafts with the label dropped', () => {
    const body = 'ship it? (recommend yes) [#1]'
    for (const prefix of ['D7 ', 'D7: ', 'D7. ', '[D7] ', '(D7) ', 'D7 - ', 'D7 — ', '[D7]: ']) {
      expect(parseDraft(`${prefix}DECIDE: ${body}`)).toEqual({ kind: 'DECIDE', body })
    }
    expect(parseDraft('[A3] ACTION: log in [no link]')).toEqual({ kind: 'ACTION', body: 'log in [no link]' })
    // Only the canonical "D7 DECIDE: ..." is a label the hook checks; the other forms aren't.
    expect(parseLabel('D7: DECIDE: x')).toBeNull()
    expect(parseLabel('[D7] DECIDE: x')).toBeNull()
    expect(parseDraft('Task D7: DECIDE: x')).toBeNull()
    expect(parseDraft('X7 DECIDE: x')).toBeNull()
  })

  test('a retitle with a self-numbered prefix keeps the item label, or assigns a fresh one', () => {
    const list = [item({})]
    expect(retitle(list, '1', '[D7] DECIDE: ship it today? (recommend yes) [#4]')).toEqual({ kind: 'subject', subject: 'D1 DECIDE: ship it today? (recommend yes) [#4]' })
    expect(retitle(list, '1', 'D1: DECIDE: ship it today? (recommend yes) [#4]')).toEqual({ kind: 'subject', subject: 'D1 DECIDE: ship it today? (recommend yes) [#4]' })
    expect(retitle(list, '9', 'A5 - ACTION: rotate the key [no link]')).toEqual({ kind: 'assign', draft: { kind: 'ACTION', body: 'rotate the key [no link]' } })
    expect(retitle(list, '1', 'DECIDE: ship it? (recommend yes) [Enter]')).toMatchObject({ kind: 'deny' })
  })
})
