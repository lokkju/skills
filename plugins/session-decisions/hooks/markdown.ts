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
