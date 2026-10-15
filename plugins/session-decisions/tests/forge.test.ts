import { describe, expect, test } from 'claude-code/testing'

import { forgeOf, linkRefs, refHref } from '../hooks/forge'

const GITHUB = { kind: 'github', host: 'github.com', repo: 'lokkju/skills' } as const
const GITLAB = { kind: 'gitlab', host: 'gitlab.com', repo: 'group/sub/proj' } as const
const CODEBERG = { kind: 'forgejo', host: 'codeberg.org', repo: 'me/tool' } as const

describe('forge', () => {
  test('a remote URL names its forge in https, ssh and scp forms', () => {
    expect(forgeOf('https://github.com/lokkju/skills.git')).toEqual(GITHUB)
    expect(forgeOf('git@github.com:lokkju/skills.git')).toEqual(GITHUB)
    expect(forgeOf('ssh://git@github.com:22/lokkju/skills')).toEqual(GITHUB)
    expect(forgeOf('https://user:token@GitHub.com/lokkju/skills/')).toEqual(GITHUB)
    expect(forgeOf('git@gitlab.com:group/sub/proj.git')).toEqual(GITLAB)
    expect(forgeOf('https://codeberg.org/me/tool')).toEqual(CODEBERG)
    expect(forgeOf('https://git.lab.example.org/a/b')).toBeNull()
    expect(forgeOf('git@github.com-work:lokkju/skills.git')).toBeNull()
    expect(forgeOf('/srv/git/skills.git')).toBeNull()
    expect(forgeOf('https://github.com/lokkju/skills/extra')).toBeNull()
    expect(forgeOf('https://github.com/lokkju/..')).toBeNull()
    expect(forgeOf('https://forgejo.example.org/me/tool')?.kind).toBe('forgejo')
    expect(forgeOf('https://gitlab.corp.example/a/b')?.kind).toBe('gitlab')
  })

  test('a reference becomes its URL on the forge', () => {
    expect(refHref('#12', GITHUB)).toBe('https://github.com/lokkju/skills/issues/12')
    expect(refHref('!4', GITHUB)).toBe('https://github.com/lokkju/skills/pull/4')
    expect(refHref('other/repo#7', GITHUB)).toBe('https://github.com/other/repo/issues/7')
    expect(refHref('#12', GITLAB)).toBe('https://gitlab.com/group/sub/proj/-/issues/12')
    expect(refHref('!4', GITLAB)).toBe('https://gitlab.com/group/sub/proj/-/merge_requests/4')
    expect(refHref('#12', CODEBERG)).toBe('https://codeberg.org/me/tool/issues/12')
    expect(refHref('!4', CODEBERG)).toBe('https://codeberg.org/me/tool/pulls/4')
    expect(refHref('#12', null)).toBeNull()
    expect(refHref('no link', GITHUB)).toBeNull()
    expect(refHref(null, GITHUB)).toBeNull()
    expect(refHref('a/b/c#3', GITHUB)).toBeNull()
  })

  test('references in prose become links; code, links and URLs keep theirs', () => {
    expect(linkRefs('Close #12 after !4 lands (see other/repo#7).', GITHUB)).toBe(
      'Close [#12](https://github.com/lokkju/skills/issues/12) after [!4](https://github.com/lokkju/skills/pull/4) lands (see [other/repo#7](https://github.com/other/repo/issues/7)).',
    )
    const kept = 'Run `git show #12`, read [#3](https://x.example/3), https://x.example/a#12, &#123; and ![img](a.png) or a#1.'
    expect(linkRefs(kept, GITHUB)).toBe(kept)
    expect(linkRefs('Close #12', null)).toBe('Close #12')
  })
})
