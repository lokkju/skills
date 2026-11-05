// The queue as text, for surfaces that don't draw the plugin's pane, cards and toasts: the mobile
// app and claude.ai/code over Remote Control. Nothing here touches `$`, so it's tested directly.

import type { Item, ListMode } from '../types'
import { type Forge, refHref } from './forge'
import { plainText } from './markdown'
import { age, isOpen, ordered, replaces } from './queue'
import { revisedIn, rulingIn, rulingText, shortRecommendation, splitBody, supersededIn } from './subject'

/** The reply_list option as a mode; anything it doesn't name is `auto`. */
export function parseListMode(value: unknown): ListMode {
  const text = String(value ?? '').trim().toLowerCase()
  return text === 'always' || text === 'never' ? text : 'auto'
}

/** The widest a hard-wrapped line of the text listing gets, indent included. */
export const TEXT_WIDTH = 72

/** `text` broken at spaces into lines of at most `width` columns after `indent`; a longer word keeps its own line. */
export function wrap(text: string, width: number | null, indent = ''): string[] {
  const words = text.split(/\s+/).filter(Boolean)
  if (words.length === 0) return []
  if (width === null) return [indent + words.join(' ')]
  const room = Math.max(10, width - indent.length)
  const lines: string[] = []
  let line = ''
  for (const word of words) {
    if (line && line.length + 1 + word.length > room) {
      lines.push(indent + line)
      line = word
    } else {
      line = line ? `${line} ${word}` : word
    }
  }
  if (line) lines.push(indent + line)
  return lines
}

/** An item's link as something a phone can open: the forge URL for `#12` when the forge is known. */
function linkText(token: string | null, forge: Forge | null): string | null {
  if (!token) return null
  return refHref(token, forge) ?? token
}

/**
 * One item as a short block: a head line (label, kind, age or how it was settled), then the
 * question, the recommendation and the link on lines of their own, indented two spaces.
 */
function block(item: Item, items: readonly Item[], now: number, width: number | null, forge: Forge | null): string[] {
  const parts = splitBody(item.body)
  const kind = item.kind === 'DECIDE' ? 'decision' : 'action'
  const head = [item.label, kind]
  if (isOpen(item)) {
    const waiting = age(item.created, now).replace(/^waiting /, '')
    if (waiting) head.push(waiting)
    const revised = revisedIn(item.description)
    if (revised) head.push(`revised ${revised}`)
  } else {
    head.push(supersededIn(item.description) ? 'superseded' : 'settled')
  }
  const lines = [head.join(' · ')]
  lines.push(...wrap(plainText(parts.question), width, '  '))
  if (parts.recommendation) lines.push(...wrap(`Recommend: ${plainText(parts.recommendation)}`, width, '  '))
  const link = linkText(parts.link, forge)
  if (link) lines.push(`  Link: ${link}`)
  if (!isOpen(item)) {
    const superseded = supersededIn(item.description)
    const ruling = rulingIn(item.description)
    if (superseded) {
      const why = superseded.why ? `: ${superseded.why}` : ''
      lines.push(...wrap(`Superseded ${superseded.date} by ${superseded.by}${why}`, width, '  '))
    } else if (ruling) {
      const date = /^Ruling \(([^)]*)\)/.exec(ruling)?.[1]
      lines.push(...wrap(`Ruling${date ? ` ${date}` : ''}: ${plainText(rulingText(ruling))}`, width, '  '))
    }
  }
  const old = isOpen(item) ? replaces(items, item.label) : undefined
  if (old) lines.push(`  Replaces ${old.label}`)
  return lines
}

/** How to answer by typing, with the labels of the first open decision and action. */
function answerHint(open: readonly Item[]): string {
  const decision = open.find(item => item.kind === 'DECIDE')
  const action = open.find(item => item.kind === 'ACTION')
  const examples = [
    ...(decision ? [`"${decision.label}: go"`, `"${decision.label}: <your answer>"`] : []),
    ...(action ? [`"${action.label}: done"`] : []),
  ]
  return `Answer by typing ${examples.join(', ')}; one message can answer several.`
}

/**
 * The queue as plain text, one short block per item: what `/decisions list` prints, and
 * `/decisions` over Remote Control. `width` hard-wraps each line; null leaves wrapping to the
 * surface (a phone wraps to its own width).
 */
export function textListing(
  items: readonly Item[],
  now: number,
  opts: { all?: boolean; width?: number | null; forge?: Forge | null } = {},
): string {
  const width = opts.width === undefined ? TEXT_WIDTH : opts.width
  const forge = opts.forge ?? null
  const sorted = ordered(items)
  const open = sorted.filter(isOpen)
  const settled = opts.all ? sorted.filter(item => !isOpen(item)) : []
  if (open.length === 0 && settled.length === 0) {
    return opts.all ? 'No decisions or actions this session.' : 'No open decisions or actions.'
  }
  const count = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`
  const decisions = open.filter(item => item.kind === 'DECIDE').length
  const head =
    open.length === 0
      ? 'Nothing open.'
      : `Open: ${[decisions && count(decisions, 'decision'), open.length - decisions && count(open.length - decisions, 'action')].filter(Boolean).join(', ')}`
  const out = [head]
  for (const item of open) out.push('', ...block(item, items, now, width, forge))
  if (settled.length > 0) {
    out.push('', `Settled: ${settled.length}`)
    for (const item of settled) out.push('', ...block(item, items, now, width, forge))
  }
  if (open.length > 0) out.push('', ...wrap(answerHint(open), width))
  return out.join('\n')
}

/** One open item as the reply list writes it: `- D3: <question> (recommend <choice>)`. */
export function replyLine(item: Item): string {
  const parts = splitBody(item.body)
  const choice = parts.recommendation ? shortRecommendation(plainText(parts.recommendation), 40).choice : null
  return `- ${item.label}: ${plainText(parts.question)}${choice ? ` (recommend ${choice})` : ''}`
}

/** Why the reply list is on: the person writes over Remote Control, or turned it on. */
export type ListReason = 'remote' | 'asked'

/**
 * What the model reads beside the person's prompt while the reply list is on: keep an `Open items`
 * list at the end of replies, with the open items as they stand.
 */
export function replyNote(items: readonly Item[], reason: ListReason): string {
  const why =
    reason === 'remote'
      ? "The user is writing over Remote Control (the mobile app or claude.ai/code), where the decision queue's pane, cards and toasts don't show."
      : 'The user turned on the reply list for the decision queue.'
  const open = ordered(items).filter(isOpen)
  const lines = [
    `session-decisions: ${why} End every reply that leaves queue items open with an \`Open items\` list, one line per item: its label, the question or action, and the recommendation, such as \`- D3: retire the old deploy script? (recommend yes)\`. Keep it to those lines; the full items are in \`/decisions list\`. The user answers by typing "D3: go" or "A2: done".`,
  ]
  if (open.length === 0) lines.push('No items are open right now.')
  else lines.push('Open right now:', ...open.map(replyLine))
  return lines.join('\n')
}

/** The longest push message; mobile systems cut what's past it. */
export const PUSH_MAX = 180

/** A new item as a push notification: label and kind, the question cut to fit, and the recommendation. */
export function pushText(item: Item): string {
  const parts = splitBody(item.body)
  const kind = item.kind === 'DECIDE' ? 'decide' : 'action'
  const head = `${item.label} ${kind}: `
  const rec = parts.recommendation ? ` Recommend: ${shortRecommendation(plainText(parts.recommendation), 60).choice}` : ''
  const question = plainText(parts.question).replace(/\s+/g, ' ').trim()
  const room = PUSH_MAX - head.length - rec.length
  const cut = question.length > room ? `${question.slice(0, Math.max(0, room - 1)).trimEnd()}…` : question
  return `${head}${cut}${rec}`
}
