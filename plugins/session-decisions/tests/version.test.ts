import { describe, expect, test } from 'claude-code/testing'

import { MIN_CLAUDE_CODE, isOlder, parseVersion, refusal } from '../hooks/version'

describe('version', () => {
  test('releases and development builds parse to their release', () => {
    expect(parseVersion('2.1.300')).toEqual([2, 1, 300])
    expect(parseVersion('2.1.300-dev')).toEqual([2, 1, 300])
    expect(parseVersion('2.1.280-dev.20260920.t101500.sha1a2b3c4')).toEqual([2, 1, 280])
    expect(parseVersion('2.1.306 (Claude Code)')).toEqual([2, 1, 306])
    expect(parseVersion('main')).toBeNull()
    expect(parseVersion('2.1')).toBeNull()
  })

  test('compares by number, not by text', () => {
    expect(isOlder('2.1.289', '2.1.300')).toBe(true)
    expect(isOlder('2.1.300', '2.1.300')).toBe(false)
    expect(isOlder('2.1.300-dev', '2.1.300')).toBe(false)
    expect(isOlder('2.1.1000', '2.1.300')).toBe(false)
    expect(isOlder('2.2.0', '2.1.300')).toBe(false)
    expect(isOlder('1.9.999', '2.1.300')).toBe(true)
    expect(isOlder('source', '2.1.300')).toBeNull()
  })

  test('an older build is refused by name, a newer or unspelled one runs', () => {
    expect(refusal('2.1.289')).toContain(`needs Claude Code ${MIN_CLAUDE_CODE} or newer; this is 2.1.289`)
    expect(refusal(MIN_CLAUDE_CODE)).toBeNull()
    expect(refusal('9.0.0')).toBeNull()
    expect(refusal('local build')).toBeNull()
    expect(refusal(null)).toContain("couldn't read this one's version")
  })
})
