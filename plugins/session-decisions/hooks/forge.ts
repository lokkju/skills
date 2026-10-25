// Issue and merge or pull request references (`#12`, `!4`, `owner/repo#12`) as URLs on the forge
// the session's repository lives on. Nothing here touches `$`, so it's tested directly.

import { linkHref } from './markdown'

export type ForgeKind = 'github' | 'gitlab' | 'forgejo'

/**
 * A repository on a forge whose URL scheme is known: its kind, host, and path (`owner/repo`).
 * `root` is the forge's web root when it isn't `https://<host>` (another scheme, a port, a subpath).
 */
export type Forge = { kind: ForgeKind; host: string; repo: string; root?: string }

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
 * or `user@host:owner/repo`. `hosts` names the kind of a host it lists, before the built-in rules,
 * and `kind`, when given, wins over both. Null for a host none of them names the kind of, or a
 * path it can't use.
 */
export function forgeOf(remote: string, hosts: ForgeHosts = {}, kind?: ForgeKind): Forge | null {
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
  const named = kind ?? hostKind(host, hosts)
  if (!named) return null
  const repo = repoPath(named, path)
  return repo ? { kind: named, host, repo } : null
}

function hostKind(host: string, hosts: ForgeHosts): ForgeKind | null {
  return (Object.hasOwn(hosts, host) ? hosts[host] : undefined) ?? kindOf(host)
}

/**
 * The per-repository override, as git config keys (set per clone with `git config`). The names
 * aren't this plugin's own so that forge-tracker, which asks the same question, can read them too.
 */
export const OVERRIDE_KEYS = { forge: 'tracker.forge', web: 'tracker.web' } as const

/** A repository's own answer to which forge its references link to. */
export type ForgeOverride = { kind?: ForgeKind; web?: string }

/**
 * The override from `git config --get-regexp` output (`key value` lines), with a note for each
 * value it can't use. A key set twice takes its last value, as `git config --get` does.
 */
export function parseOverride(config: string): { override: ForgeOverride; notes: string[] } {
  const values = new Map<string, string>()
  for (const line of config.split('\n')) {
    const match = /^(\S+)[ \t]*(.*)$/.exec(line.trim())
    if (match?.[1]) values.set(match[1].toLowerCase(), (match[2] ?? '').trim())
  }
  const override: ForgeOverride = {}
  const notes: string[] = []
  const forge = values.get(OVERRIDE_KEYS.forge)
  if (forge !== undefined) {
    const name = forge.toLowerCase()
    const kind = Object.hasOwn(KINDS, name) ? KINDS[name] : undefined
    if (kind) override.kind = kind
    else notes.push(`${OVERRIDE_KEYS.forge} "${forge}": the kind is github, gitlab, forgejo or gitea`)
  }
  const web = values.get(OVERRIDE_KEYS.web)
  if (web !== undefined) {
    if (webParts(web)) override.web = web
    else notes.push(`${OVERRIDE_KEYS.web} "${web}" is not the https URL of a repository`)
  }
  return { override, notes }
}

/** A web URL as its parsed form and its path segments, when each segment is safe; else null. */
function webParts(web: string): { url: URL; segments: string[] } | null {
  let url: URL
  try {
    url = new URL(web.trim())
  } catch {
    return null
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null
  if (!HOST.test(url.hostname)) return null
  const segments = url.pathname.replace(/^\/+|\/+$/g, '').replace(/\.git$/i, '').split('/')
  if (segments.length < 2 || segments.some(one => !SEGMENT.test(one) || one === '.' || one === '..')) return null
  return { url, segments }
}

/**
 * The forge a repository's web URL names, as `kind`. GitHub and Forgejo take the last two path
 * segments as the repository and anything before them as the forge's subpath. GitLab's groups
 * nest, so there the whole path is the project and the forge sits at the host's root.
 */
export function webForge(web: string, kind: ForgeKind): Forge | null {
  const parts = webParts(web)
  if (!parts) return null
  const { url, segments } = parts
  const split = kind === 'gitlab' ? 0 : segments.length - 2
  const host = url.hostname
  const repo = segments.slice(split).join('/')
  const root = [url.origin, ...segments.slice(0, split)].join('/')
  return root === `https://${host}` ? { kind, host, repo } : { kind, host, repo, root }
}

/**
 * The session repository's forge. With `web` overridden, that URL is the repository and `kind` (or
 * else what `hosts` and the built-in rules say of its host) the forge. With only `kind`, the
 * remote's host and path on that kind of forge. With neither, the remote as `forgeOf` reads it.
 */
export function resolveForge(remote: string, hosts: ForgeHosts, override: ForgeOverride = {}): Forge | null {
  if (override.web) {
    const host = webParts(override.web)?.url.hostname ?? ''
    const kind = override.kind ?? hostKind(host, hosts)
    return kind ? webForge(override.web, kind) : null
  }
  return remote ? forgeOf(remote, hosts, override.kind) : null
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
        ? isIssue ? '-/work_items' : '-/merge_requests'
        : isIssue ? 'issues' : 'pulls'
  return linkHref(`${forge.root ?? `https://${forge.host}`}/${repo}/${where}/${number}`)
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
