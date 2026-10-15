// Markdown in item text, for the places a card can't draw it: one-line rows, chips, toasts and the
// question dialog.
// Nothing here touches `$`, so it's tested directly.

const FENCE = /```[^\n]*\n([\s\S]*?)```/g
const INLINE_CODE = /(`+)([\s\S]*?[^`])\1(?!`)/g

/**
 * Emphasis markers and link syntax dropped from text that holds no code. With `urls`, a link keeps
 * its target after its text, `text (url)`, unless the text already reads as the url.
 */
function stripProse(text: string, urls = false): string {
  return text
    .replace(/!?\[([^\]]*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)/g, (_, label: string, url: string) => {
      if (!urls || !url) return label
      return label.trim() && label.trim() !== url ? `${label} (${url})` : url
    })
    .replace(/<((?:https?|mailto):[^\s<>]+)>/gi, urls ? '$1' : '$&')
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '$1')
    .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1$2')
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '$1')
    .replace(/(^|[^\w*])\*(?=[^\s*])([^*]*?[^\s*])\*(?![\w*])/g, '$1$2')
    .replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, '$1$2')
}

/**
 * Markdown as the plain text it reads as: no `**`, `__`, `*`, `_` or `~~` around emphasis, no
 * backticks around code (whose contents stay as written), and `[text](url)` as `text`, or with
 * `urls` as `text (url)`.
 */
export function plainText(markdown: string, urls = false): string {
  // Code stands aside as private-use placeholders while the markers go, so `**`x`**` loses both.
  const code: string[] = []
  const held = markdown
    .replace(/[]/g, '')
    .replace(FENCE, (_, body: string) => `${code.push(body.trimEnd()) - 1}`)
    .replace(INLINE_CODE, (_, _ticks: string, body: string) => `${code.push(body.trim()) - 1}`)
  return stripProse(held, urls).replace(/(\d+)/g, (_, n: string) => code[Number(n)] ?? '')
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
 * Markdown as the plain text a dialog shows, which keeps line breaks but draws no markdown: lines,
 * blank lines and list items (`- `, `1. `) stay where they are, `*` and `+` bullets become `- `,
 * headings lose their `#`, emphasis and code markers go, and a link reads `text (url)`. Runs of
 * blank lines squeeze to one.
 */
export function dialogText(markdown: string): string {
  let fenced = false
  const shaped = markdown
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map(line => {
      if (/^\s*```/.test(line)) fenced = !fenced
      else if (!fenced) return line.replace(/^(\s*)[*+](\s+)/, '$1-$2').replace(/^ {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$/, '$1')
      return line
    })
  return plainText(shaped.join('\n'), true)
    .split('\n')
    .map(line => line.trimEnd())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/^\n+|\s+$/g, '')
}

/** The rows a line takes when it wraps at word boundaries in `width` columns. */
export function wrappedRows(line: string, width: number): number {
  if (!Number.isFinite(width) || line.length <= width) return 1
  let rows = 1
  let used = 0
  for (const word of line.split(' ')) {
    const gap = used === 0 ? 0 : 1
    if (used + gap + word.length <= width) {
      used += gap + word.length
      continue
    }
    if (used > 0) rows++
    // A word longer than a row breaks across rows.
    let length = word.length
    while (length > width) {
      rows++
      length -= width
    }
    used = length
  }
  return rows
}

/**
 * Text cut to at most `rows` rows when its lines wrap at `width` columns (`Infinity` counts lines).
 * A cut text ends with a line saying how many of its lines aren't shown, and the rows include it;
 * a line cut part way ends with `…` and counts as not shown.
 */
export function fitRows(text: string, rows: number, width = Infinity): string {
  const lines = text.split('\n')
  if (lines.reduce((sum, line) => sum + wrappedRows(line, width), 0) <= rows) return text
  const room = Math.max(0, rows - 1)
  const kept: string[] = []
  let used = 0
  let at = 0
  for (; at < lines.length; at++) {
    const need = wrappedRows(lines[at] ?? '', width)
    if (used + need > room) break
    kept.push(lines[at] ?? '')
    used += need
  }
  // Part of the next line fills whatever room is left, cut at a word.
  const next = lines[at] ?? ''
  if (room > used && next.trim() && Number.isFinite(width)) {
    let part = next.slice(0, (room - used) * width)
    if (part.length < next.length && /\S/.test(next[part.length] ?? ' ')) part = part.replace(/\S*$/, '')
    while (part.trim() && wrappedRows(`${part.trimEnd()}…`, width) > room - used) part = part.replace(/\s*\S*$/, '')
    if (part.trim()) kept.push(`${part.trimEnd()}…`)
  }
  while (kept.length > 0 && !kept.at(-1)?.trim()) kept.pop()
  const hidden = lines.slice(at).filter(line => line.trim()).length
  kept.push(`… ${hidden} more ${hidden === 1 ? 'line' : 'lines'} not shown`)
  return kept.join('\n')
}

// Rows Claude Code's question dialog (2.1.286) draws around the question: a rule, the header and a
// blank line above it, a blank line under it, a row per option and one for "Type something", then a
// rule, "Chat about this", a blank line and the key hints. The task list under it takes three more.
const DIALOG_ROWS = 12
// The dialog draws the question behind a two-column gutter (`│ `) and wraps it at the screen's edge.
const DIALOG_GUTTER = 2
// How many rows the question may take when no drawing has measured the screen.
const UNMEASURED_ROWS = 24

/**
 * An item as the question dialog asks it, in plain text: the question, `Link: <url>` when there's a
 * link to show, a blank line,
 * `Recommended: <recommendation>`, a blank line, and the description as `dialogText` shapes it, cut
 * so that the question and its `options` fit a screen of `size`, or to 24 rows when it's unmeasured.
 */
export function dialogQuestion(
  item: { question: string; recommendation: string | null; description: string; link?: string | null },
  options: number,
  size: { rows: number; columns: number } | null,
): string {
  const head = [plainText(item.question, true)]
  if (item.link) head.push(`Link: ${item.link}`)
  if (item.recommendation) head.push('', `Recommended: ${plainText(item.recommendation, true)}`)
  const about = dialogText(item.description)
  if (!about) return head.join('\n')
  const width = size ? Math.max(10, size.columns - DIALOG_GUTTER) : Infinity
  const room = size ? size.rows - DIALOG_ROWS - options : UNMEASURED_ROWS
  const used = head.reduce((sum, line) => sum + wrappedRows(line, width), 0) + 1
  return [...head, '', fitRows(about, Math.max(2, room - used), width)].join('\n')
}
