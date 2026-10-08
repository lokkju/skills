// Parse, validate and format the subjects of decision items.
//
// A labelled subject is "<label> <KIND>: <body>", for example
// "D3 DECIDE: retire the old deploy script? (recommend yes) [#812]".
// The assistant writes "DECIDE: ..." or "ACTION: ..."; the TaskCreate hook adds the label.

import type { Kind, Letter } from '../types'

export const LETTER: Record<Kind, Letter> = { DECIDE: 'D', ACTION: 'A' }

// An optional existing label ("D12 "), the kind keyword, a colon, then the body. The keyword is
// case-sensitive, so an ordinary task titled "Action: refactor the parser" isn't a decision item.
const DRAFT = /^\s*(?:[DA]\d+\s+)?(DECIDE|ACTION)\s*:\s*([\s\S]*?)\s*$/
const LABELLED = /^([DA])(\d+) (DECIDE|ACTION): ([\s\S]*)$/
// The link is the last bracketed token, at the very end of the body.
const LINK = /\[[^[\]]*\S[^[\]]*\]$/
const RECOMMEND = /\(recommend\s+\S/i
// One level of nested parentheses, so "(recommend B (cheaper))" keeps "B (cheaper)" whole.
const CHOICE = /\(recommend\s+((?:[^()]|\([^()]*\))*?)\s*\)/i

export type Draft = { kind: Kind; body: string }
export type Label = { letter: Letter; number: number; kind: Kind; body: string }

/** The draft when the subject is a decision item; null for any other task. */
export function parseDraft(subject: string): Draft | null {
  const match = DRAFT.exec(subject ?? '')
  if (!match) return null
  return { kind: match[1] as Kind, body: match[2] ?? '' }
}

/** What's wrong with a draft, as short instructions; empty when it's well formed. */
export function problems(draft: Draft): string[] {
  if (!draft.body) return ['write the question or action after the colon']
  const found: string[] = []
  if (!LINK.test(draft.body)) {
    found.push(
      'end the subject with one link token: [#123], [!45], [owner/repo#123], [<url>] or [no link]',
    )
  }
  if (draft.kind === 'DECIDE' && !RECOMMEND.test(draft.body)) {
    found.push('a DECIDE item needs a recommendation, written as (recommend <choice>)')
  }
  return found
}

export function formatSubject(label: string, draft: Draft): string {
  return `${label} ${draft.kind}: ${draft.body}`
}

/** A subject the hook has already labelled; null for anything else. */
export function parseLabel(subject: string): Label | null {
  const match = LABELLED.exec(subject ?? '')
  if (!match) return null
  return {
    letter: match[1] as Letter,
    number: Number(match[2]),
    kind: match[3] as Kind,
    body: match[4] ?? '',
  }
}

/** The recommended choice of a DECIDE body, for the Accept button; null when there isn't one. */
export function recommendation(body: string): string | null {
  const match = CHOICE.exec(body)
  return match?.[1] ? match[1] : null
}

/** The last "Ruling (...)" line of a description, which records how the item was settled. */
export function rulingIn(description: string): string | null {
  const lines = description.split('\n').reverse()
  return lines.find(line => line.startsWith('Ruling (')) ?? null
}

export type BodyParts = { question: string; recommendation: string | null; link: string | null }

/** A body split for display: the question alone, the recommended choice, and the link token. */
export function splitBody(body: string): BodyParts {
  let rest = body.trim()
  let link: string | null = null
  const linked = LINK.exec(rest)
  if (linked) {
    const token = linked[0].slice(1, -1).trim()
    link = token.toLowerCase() === 'no link' ? null : token
    rest = rest.slice(0, linked.index).trim()
  }
  const choice = recommendation(rest)
  if (choice) rest = rest.replace(CHOICE, '').replace(/\s{2,}/g, ' ').trim()
  return { question: rest, recommendation: choice, link }
}

/**
 * A recommendation as a card shows it: the choice for the tag beside the label (up to the first
 * comma or semicolon, at most `max` characters) and the qualifier after it, if any.
 */
export function shortRecommendation(text: string, max = 24): { choice: string; qualifier: string | null } {
  const cut = text.search(/[,;]/)
  const head = (cut < 0 ? text : text.slice(0, cut)).trim()
  const qualifier = cut < 0 ? null : text.slice(cut + 1).trim() || null
  const choice = head.length > max ? `${head.slice(0, max - 1).trimEnd()}…` : head
  return { choice, qualifier: head.length > max ? text.trim() : qualifier }
}
