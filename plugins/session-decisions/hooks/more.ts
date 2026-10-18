// What the question dialog shows beyond an open card, and where a card's `[more]` goes.
// Nothing here touches `$`, so it's tested directly.

import { type Forge, refHref } from './forge'
import { codeSpans, dialogText, plainText } from './markdown'
import { type BodyParts, shortRecommendation } from './subject'

export const MORE = '[more]'

/**
 * The link and command the dialog offers for an item: the link as a URL (as written, or a
 * reference resolved on the repository's forge) for its `Link:` line and Copy link, and the first
 * code span of the question or description for its Copy option; null for each it doesn't offer.
 */
export function dialogExtras(
  parts: BodyParts,
  description: string,
  forge: Forge | null,
): { url: string | null; code: string | null } {
  const url = parts.link && /^https?:\/\//i.test(parts.link) ? parts.link : refHref(parts.link, forge)
  return { url, code: codeSpans(`${parts.question}\n${description}`)[0] ?? null }
}

/**
 * Whether a card cuts its recommendation short: the tag beside the label shortens the choice and
 * the qualifier under the question doesn't carry the whole of it. The tag is cut from the plain
 * text and the qualifier from the markdown, as the card draws them.
 */
export function recommendationCut(recommendation: string): boolean {
  // shortRecommendation makes the qualifier the whole text exactly when it cuts the choice.
  const whole = (text: string) => shortRecommendation(text).qualifier === text.trim()
  return whole(plainText(recommendation)) && !whole(recommendation)
}

/**
 * Whether the question dialog shows something an open card doesn't, so the card ends its text
 * with `[more]`: a description, a recommendation the card cuts short, a link the dialog spells out
 * as a URL and offers to copy, or a command it offers to copy. With none of them the card is the
 * whole item.
 */
export function hasMore(item: { parts: BodyParts; description: string }, forge: Forge | null): boolean {
  if (dialogText(item.description)) return true
  if (item.parts.recommendation && recommendationCut(item.parts.recommendation)) return true
  const extras = dialogExtras(item.parts, item.description, forge)
  return extras.url !== null || extras.code !== null
}

/**
 * Where `[more]` goes after a card's last line of text, `markdown`, drawn `width` columns wide: on
 * that line's last row, one space after the text, when it fits there, or null for a row of its own.
 * The line comes back split in two, `head` for the rows above (empty when it takes one row) and
 * `tail` for its last row, which the card draws with `[more]` beside it; each part is whole
 * markdown, so a split inside a span of emphasis or code gives null instead.
 *
 * The rows are worked out the way a terminal wraps plain words. A line whose drawn width can't be
 * told from its text (a link, which a terminal without hyperlinks spells out, or anything outside
 * printable ASCII), a word longer than a row, and an unknown width give null. A width that's off
 * moves where the rows break, never draws `[more]` over the text.
 */
export function moreSplit(markdown: string, width: number | null): { head: string; tail: string } | null {
  if (width === null || !Number.isFinite(width) || width <= MORE.length) return null
  const source = markdown.trim()
  const text = plainText(source)
  if (!text || text !== plainText(source, true) || !/^[\x20-\x7e]*$/.test(text)) return null
  const rows = wrapWords(text, width)
  const last = rows?.at(-1)
  if (!rows || last === undefined || last.length + 1 + MORE.length > width) return null
  if (rows.length === 1) return { head: '', tail: source }
  const words = source.split(/ +/)
  const count = last.split(' ').length
  const head = words.slice(0, -count).join(' ')
  const tail = words.slice(-count).join(' ')
  // Each part must read as its share of the line, so no marker is left open in either.
  const same = (a: string, b: string) => a.replace(/ +/g, ' ') === b.replace(/ +/g, ' ')
  return same(plainText(head), rows.slice(0, -1).join(' ')) && same(plainText(tail), last) ? { head, tail } : null
}

/** `line`'s rows when it wraps at word boundaries in `width` columns; null when a word won't fit a row. */
function wrapWords(line: string, width: number): string[] | null {
  const rows: string[] = []
  let row = ''
  for (const word of line.split(/ +/)) {
    if (!word) continue
    if (word.length > width) return null
    if (!row) row = word
    else if (row.length + 1 + word.length <= width) row += ` ${word}`
    else {
      rows.push(row)
      row = word
    }
  }
  if (row) rows.push(row)
  return rows
}
