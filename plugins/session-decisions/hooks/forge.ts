// Issue and merge or pull request references (`#12`, `!4`, `owner/repo#12`) as URLs on the forge
// the session's repository lives on. Nothing here touches `$`, so it's tested directly.

import { linkHref } from './markdown'

export type ForgeKind = 'github' | 'gitlab' | 'forgejo'

/** A repository on a forge whose URL scheme is known: its kind, host, and path (`owner/repo`). */
export type Forge = { kind: ForgeKind; host: string; repo: string }

/** Hosts the person named a forge kind for (the `forge_hosts` option), lowercase. */
export type ForgeHosts = Readonly<Record<string, ForgeKind>>

const KINDS: Readonly<Record<string, ForgeKind>> = { github: 'github', gitlab: 'gitlab', forgejo: 'forgejo', gitea: 'forgejo' }
const HOST = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$/

/**
 * The `forge_hosts` option, `host=kind` entries split by commas or whitespace, as hosts and their
 * kinds (`gitea` is read as `forgejo`, whose URLs it shares), with a note for each entry it can't
 * use. A host named twice takes its last kind.
 */
export function parseForgeHosts(option: unknown): { hosts: ForgeHosts; notes: string[] } {
  const hosts: Record<string, ForgeKind> = {}
  const notes: string[] = []
  const text = Array.isArray(option) ? option.join(',') : typeof option === 'string' ? option : ''
  for (const entry of text.replace(/\s*=\s*/g, '=').split(/[\s,]+/).filter(Boolean)) {
    const at = entry.indexOf('=')
    const host = (at < 0 ? '' : entry.slice(0, at)).toLowerCase()
    const name = entry.slice(at + 1).toLowerCase()
    const kind = Object.hasOwn(KINDS, name) ? KINDS[name] : undefined
    if (at < 0) notes.push(`"${entry}" is not host=kind`)
    else if (!HOST.test(host)) notes.push(`"${entry}": "${host}" is not a host name`)
    else if (!kind) notes.push(`"${entry}": the kind is github, gitlab, forgejo or gitea`)
    else hosts[host] = kind
  }
  return { hosts, notes }
}

// Only hosts whose kind can be told from the name alone: a self-hosted forge on any other name
// could be any of the three, and a wrong guess makes a link that goes somewhere else.
function kindOf(host: string): ForgeKind | null {
  if (host === 'github.com') return 'github'
  if (host === 'gitlab.com' || host.startsWith('gitlab.')) return 'gitlab'
  if (host === 'codeberg.org' || host.startsWith('forgejo.') || host.startsWith('gitea.')) return 'forgejo'
  return null
}

const SEGMENT = /^[\w.-]+$/

/** A repository path as `a/b` when each segment is safe and the forge allows that many. */
function repoPath(kind: ForgeKind, path: string): string | null {
  const segments = path.replace(/^\/+|\/+$/g, '').replace(/\.git$/i, '').split('/')
  if (segments.some(one => !SEGMENT.test(one) || one === '.' || one === '..')) return null
  // GitLab nests groups; GitHub and Forgejo have an owner and a name.
  if (kind === 'gitlab' ? segments.length < 2 : segments.length !== 2) return null
  return segments.join('/')
}

/**
 * The forge a git remote URL points at: `https://host/owner/repo(.git)`, `ssh://[user@]host[:port]/owner/repo`
 * or `user@host:owner/repo`. `hosts` names the kind of a host it lists, before the built-in rules.
 * Null for a host neither names the kind of, or a path it can't use.
 */
export function forgeOf(remote: string, hosts: ForgeHosts = {}): Forge | null {
  const text = remote.trim()
  let host: string
  let path: string
  const scp = /^(?:[\w.-]+@)?([\w.-]+):(?!\/\/)(.+)$/.exec(text)
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
    let url: URL
    try {
      url = new URL(text)
    } catch {
      return null
    }
    if (!['https:', 'http:', 'ssh:', 'git:', 'git+ssh:', 'ssh+git:'].includes(url.protocol)) return null
    host = url.hostname
    path = url.pathname
  } else if (scp) {
    host = scp[1] ?? ''
    path = scp[2] ?? ''
  } else {
    return null
  }
  host = host.toLowerCase()
  const kind = (Object.hasOwn(hosts, host) ? hosts[host] : undefined) ?? kindOf(host)
  if (!kind) return null
  const repo = repoPath(kind, path)
  return repo ? { kind, host, repo } : null
}

const REF = /^(?:([\w.-]+(?:\/[\w.-]+)+))?([#!])(\d+)$/

/**
 * A reference as its URL on `forge`: `#N` an issue (GitHub and Forgejo send an issue URL on to a
 * pull request with that number), `!N` a merge or pull request, and `owner/repo#N` the same in
 * another repository on that host. Null with no forge, or for anything else.
 */
export function refHref(token: string | null, forge: Forge | null): string | null {
  if (!token || !forge) return null
  const match = REF.exec(token)
  if (!match) return null
  const repo = match[1] ? repoPath(forge.kind, match[1]) : forge.repo
  if (!repo) return null
  const isIssue = match[2] === '#'
  const number = match[3]
  const where =
    forge.kind === 'github'
      ? isIssue ? 'issues' : 'pull'
      : forge.kind === 'gitlab'
        ? isIssue ? '-/issues' : '-/merge_requests'
        : isIssue ? 'issues' : 'pulls'
  return linkHref(`https://${forge.host}/${repo}/${where}/${number}`)
}

// What a reference in prose must not be part of: code, a markdown link, an autolink or a bare URL,
// each kept as it is. Then a reference standing on its own: not inside a word, a path, an HTML
// entity (`&#123;`) or a markdown image (`![`).
const PROSE_REF =
  /(```[\s\S]*?```|(`+)[\s\S]*?\2|!?\[[^\]]*\]\([^)]*\)|<[^>\s]+>|https?:\/\/[^\s<>]+)|(?<![\w/&#!\]])((?:[\w.-]+\/[\w.-]+)?[#!]\d+)(?![\w#!/[])/g

/** Markdown with each reference in its prose written as a link to it on `forge`. */
export function linkRefs(markdown: string, forge: Forge | null): string {
  if (!forge) return markdown
  return markdown.replace(PROSE_REF, (whole: string, kept: string | undefined, _ticks, ref: string | undefined) => {
    if (kept !== undefined || !ref) return whole
    const href = refHref(ref, forge)
    return href ? `[${ref}](${href})` : whole
  })
}
