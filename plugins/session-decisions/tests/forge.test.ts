import { describe, expect, test } from 'claude-code/testing'

import { forgeOf, linkRefs, parseForgeHosts, parseOverride, refHref, resolveForge, webForge } from '../hooks/forge'

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

  test('the forge_hosts option names hosts the built-in rules do not, and overrides them', () => {
    const { hosts, notes } = parseForgeHosts('git.lab.lokkju.com=forgejo, Code.Example.com = gitlab\ngit.example.net=gitea github.com=gitlab')
    expect(hosts).toEqual({ 'git.lab.lokkju.com': 'forgejo', 'code.example.com': 'gitlab', 'git.example.net': 'forgejo', 'github.com': 'gitlab' })
    expect(notes).toEqual([])
    const lab = forgeOf('ssh://git@git.lab.lokkju.com/lokkju/demo.git', hosts)
    expect(lab).toEqual({ kind: 'forgejo', host: 'git.lab.lokkju.com', repo: 'lokkju/demo' })
    expect(refHref('#12', lab)).toBe('https://git.lab.lokkju.com/lokkju/demo/issues/12')
    expect(refHref('!4', lab)).toBe('https://git.lab.lokkju.com/lokkju/demo/pulls/4')
    const nested = forgeOf('git@code.example.com:group/sub/proj.git', hosts)
    expect(refHref('#12', nested)).toBe('https://code.example.com/group/sub/proj/-/issues/12')
    expect(refHref('!4', nested)).toBe('https://code.example.com/group/sub/proj/-/merge_requests/4')
    expect(forgeOf('https://git.example.net/me/tool', hosts)?.kind).toBe('forgejo')
    expect(forgeOf('https://github.com/group/sub/proj', hosts)).toEqual({ kind: 'gitlab', host: 'github.com', repo: 'group/sub/proj' })
    expect(refHref('#12', forgeOf('git@github.com:lokkju/skills.git', parseForgeHosts('github.com=github').hosts))).toBe(
      'https://github.com/lokkju/skills/issues/12',
    )
    expect(forgeOf('https://gitlab.corp.example/a/b', hosts)?.kind).toBe('gitlab')
    expect(forgeOf('https://git.lab.example.org/a/b', hosts)).toBeNull()
    expect(parseForgeHosts(['a.example=github', 'b.example=gitlab']).hosts).toEqual({ 'a.example': 'github', 'b.example': 'gitlab' })
    expect(parseForgeHosts(undefined)).toEqual({ hosts: {}, notes: [] })
    expect(parseForgeHosts('')).toEqual({ hosts: {}, notes: [] })
  })

  test('entries forge_hosts cannot use are skipped with a note, and the rest still apply', () => {
    const { hosts, notes } = parseForgeHosts('git.lab.lokkju.com, https://x.example=github, y.example=bitbucket, z.example=constructor, ok.example=github')
    expect(hosts).toEqual({ 'ok.example': 'github' })
    expect(notes).toEqual([
      '"git.lab.lokkju.com" is not host=kind',
      '"https://x.example=github": "https://x.example" is not a host name',
      '"y.example=bitbucket": the kind is github, gitlab, forgejo or gitea',
      '"z.example=constructor": the kind is github, gitlab, forgejo or gitea',
    ])
    expect(forgeOf('https://constructor/a/b', parseForgeHosts('').hosts)).toBeNull()
  })

  test('the git config override is read from --get-regexp output, and values it cannot use get a note', () => {
    expect(parseOverride('')).toEqual({ override: {}, notes: [] })
    expect(parseOverride('tracker.forge Gitea\ntracker.web https://git.lab.lokkju.com/lokkju/demo\n')).toEqual({
      override: { kind: 'forgejo', web: 'https://git.lab.lokkju.com/lokkju/demo' },
      notes: [],
    })
    expect(parseOverride('tracker.forge gitlab\ntracker.forge github').override).toEqual({ kind: 'github' })
    expect(parseOverride('tracker.forge bitbucket\ntracker.web http://git.example/a/b').notes).toEqual([
      'tracker.forge "bitbucket": the kind is github, gitlab, forgejo or gitea',
      'tracker.web "http://git.example/a/b" is not the https URL of a repository',
    ])
    for (const web of ['https://h.example/one', 'https://u:p@h.example/a/b', 'https://h.example/a/b?x=1', 'https://h.example/a/..', 'git@h.example:a/b', 'tracker']) {
      expect(parseOverride(`tracker.web ${web}`).override).toEqual({})
    }
  })

  test('a web URL names the repository and the forge root, subpath included', () => {
    expect(webForge('https://git.lab.lokkju.com/lokkju/demo', 'forgejo')).toEqual({ kind: 'forgejo', host: 'git.lab.lokkju.com', repo: 'lokkju/demo' })
    expect(webForge('https://GitHub.com/lokkju/skills.git/', 'github')).toEqual(GITHUB)
    const sub = webForge('https://code.example.org/forge/me/tool', 'forgejo')
    expect(sub).toEqual({ kind: 'forgejo', host: 'code.example.org', repo: 'me/tool', root: 'https://code.example.org/forge' })
    expect(refHref('#12', sub)).toBe('https://code.example.org/forge/me/tool/issues/12')
    expect(refHref('other/repo#7', sub)).toBe('https://code.example.org/forge/other/repo/issues/7')
    const port = webForge('https://git.example:8443/me/tool', 'forgejo')
    expect(refHref('!4', port)).toBe('https://git.example:8443/me/tool/pulls/4')
    expect(webForge('https://gitlab.example/group/sub/proj', 'gitlab')).toEqual({ kind: 'gitlab', host: 'gitlab.example', repo: 'group/sub/proj' })
    expect(webForge('https://h.example/only', 'github')).toBeNull()
  })

  test('the override wins over forge_hosts and the built-in rules; without one the remote decides as before', () => {
    const hosts = parseForgeHosts('homelab-git=github, git.lab.lokkju.com=gitlab').hosts
    const web = 'https://git.lab.lokkju.com/lokkju/demo'
    // An SSH alias: the web base gives the real host.
    expect(refHref('#12', resolveForge('homelab-git:lokkju/demo.git', hosts, { kind: 'forgejo', web }))).toBe(`${web}/issues/12`)
    // Only the web base: its host's kind comes from forge_hosts or the built-in rules.
    expect(resolveForge('', hosts, { web })?.kind).toBe('gitlab')
    expect(resolveForge('', {}, { web: 'https://github.com/lokkju/skills' })).toEqual(GITHUB)
    expect(resolveForge('', {}, { web })).toBeNull()
    // A mirror on GitHub whose tracker is elsewhere.
    expect(refHref('!4', resolveForge('git@github.com:lokkju/demo.git', {}, { kind: 'forgejo', web }))).toBe(`${web}/pulls/4`)
    // Only the kind: the remote's host and path, on that kind of forge.
    expect(resolveForge('git@github.com:lokkju/skills.git', hosts, { kind: 'forgejo' })).toEqual({ kind: 'forgejo', host: 'github.com', repo: 'lokkju/skills' })
    expect(resolveForge('ssh://git@git.example.org/a/b', {}, { kind: 'forgejo' })?.kind).toBe('forgejo')
    expect(resolveForge('', {}, { kind: 'forgejo' })).toBeNull()
    // No override.
    expect(resolveForge('git@github.com:lokkju/skills.git', {})).toEqual(GITHUB)
    expect(resolveForge('git@github.com:lokkju/skills.git', {}, {})).toEqual(GITHUB)
    expect(resolveForge('https://git.lab.example.org/a/b', {})).toBeNull()
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
