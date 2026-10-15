// Markdown in item text, for the places a card can't draw it: one-line rows, chips and toasts.
// Nothing here touches `$`, so it's tested directly.

const FENCE = /```[^\n]*\n([\s\S]*?)```/g
const INLINE_CODE = /(`+)([\s\S]*?[^`])\1(?!`)/g

/** Emphasis markers and link syntax dropped from text that holds no code. */
function stripProse(text: string): string {
  return text
    .replace(/!?\[([^\]]*)\]\([^)\s]*(?:\s+"[^"]*")?\)/g, '$1')
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '$1')
    .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1$2')
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '$1')
    .replace(/(^|[^\w*])\*(?=[^\s*])([^*]*?[^\s*])\*(?![\w*])/g, '$1$2')
    .replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, '$1$2')
}

/**
 * Markdown as the plain text it reads as: no `**`, `__`, `*`, `_` or `~~` around emphasis, no
 * backticks around code (whose contents stay as written), and `[text](url)` as `text`.
 */
export function plainText(markdown: string): string {
  // Code stands aside as private-use placeholders while the markers go, so `**`x`**` loses both.
  const code: string[] = []
  const held = markdown
    .replace(/[]/g, '')
    .replace(FENCE, (_, body: string) => `${code.push(body.trimEnd()) - 1}`)
    .replace(INLINE_CODE, (_, _ticks: string, body: string) => `${code.push(body.trim()) - 1}`)
  return stripProse(held).replace(/(\d+)/g, (_, n: string) => code[Number(n)] ?? '')
}

/** The code in markdown, fenced blocks first and then inline spans, each once and trimmed. */
export function codeSpans(markdown: string): string[] {
  const found: string[] = []
  const add = (code: string | undefined) => {
    const text = (code ?? '').trim()
    if (text && !found.includes(text)) found.push(text)
  }
  for (const match of markdown.matchAll(FENCE)) add(match[1])
  for (const match of markdown.replace(FENCE, '').matchAll(INLINE_CODE)) add(match[2])
  return found
}

/**
 * A link token as an href a `Link` element accepts (an `https:` URL or `http://localhost`, printable
 * ASCII, no `@` or credentials, at most 2048 characters), spelled as `new URL` spells it; null for
 * anything else, such as `#12` or `owner/repo#3`, which stays text.
 */
export function linkHref(token: string | null): string | null {
  if (!token || !/^https?:\/\//i.test(token)) return null
  let url: URL
  try {
    url = new URL(token)
  } catch {
    return null
  }
  const href = url.href
  const local = url.protocol === 'http:' && url.hostname === 'localhost'
  if (url.protocol !== 'https:' && !local) return null
  if (url.username || url.password || href.includes('@')) return null
  return href.length <= 2048 && /^[\x21-\x7e]+$/.test(href) ? href : null
}

/** A link token short enough to sit at the end of a card's button row: `#12`, `!4`, `owner/repo#3`. */
export function isShortLink(token: string): boolean {
  return /^(?:[\w.-]+\/[\w.-]+)?[#!]\d+$/.test(token)
}

// First path segments of github.com that aren't an owner.
const GITHUB_PAGES = new Set(['apps', 'enterprise', 'features', 'login', 'marketplace', 'notifications', 'orgs', 'search', 'settings', 'sponsors', 'topics'])

/**
 * A github.com issue or pull request URL as `owner/repo#N`, a repository URL as `owner/repo`; null
 * for any other URL, which a card shows whole.
 */
export function githubShort(href: string): string | null {
  let url: URL
  try {
    url = new URL(href)
  } catch {
    return null
  }
  if (url.protocol !== 'https:' || !/^(www\.)?github\.com$/i.test(url.hostname)) return null
  const [owner, repo, kind, number] = url.pathname.split('/').filter(Boolean)
  if (!owner || !repo || GITHUB_PAGES.has(owner.toLowerCase())) return null
  const name = `${owner}/${repo.replace(/\.git$/, '')}`
  if (kind === undefined) return name
  return (kind === 'issues' || kind === 'pull') && number && /^\d+$/.test(number) ? `${name}#${number}` : null
}

/**
 * Markdown as plain text cut to fit a dialog: at most `lines` lines and `max` characters, blank-line
 * runs squeezed to one, cut at a word with `…`.
 */
export function excerpt(markdown: string, max = 320, lines = 6): string {
  const text = plainText(markdown)
    .split('\n')
    .map(line => line.trimEnd())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  const kept = text.split('\n').slice(0, lines).join('\n')
  if (kept.length <= max && kept === text) return text
  const cut = kept.length <= max ? kept : kept.slice(0, max)
  const word = kept.length <= max ? cut : cut.replace(/\s+\S*$/, '')
  return `${(word || cut).trimEnd()}…`
}
