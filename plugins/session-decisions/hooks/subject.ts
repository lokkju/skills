// Parse, validate and format the subjects of decision items.
//
// A labelled subject is "<label> <KIND>: <body>", for example
// "D3 DECIDE: retire the old deploy script? (recommend yes) [#812]".
// The assistant writes "DECIDE: ..." or "ACTION: ..."; the TaskCreate hook adds the label.

import type { Kind, Letter } from '../types'

export const LETTER: Record<Kind, Letter> = { DECIDE: 'D', ACTION: 'A' }

// An optional label the assistant typed ("D12 ", "D7: ", "[D7] ", "(A2) ", "D7 - "), the kind
// keyword, a colon, then the body. The label is dropped, so the hook numbers the item itself. The
// keyword is case-sensitive, so an ordinary task titled "Action: refactor the parser" isn't a
// decision item.
const DRAFT = /^\s*(?:[[(]?[DA]\d+[\])]?[\s:.\-\u2013\u2014]*)?(DECIDE|ACTION)\s*:\s*([\s\S]*?)\s*$/
const LABELLED = /^([DA])(\d+) (DECIDE|ACTION): ([\s\S]*)$/
// The link is the bracketed token at the very end of the body, in one of the forms the skill
// lists: #123, !45, owner/repo#123 (or !45), an http(s) URL, or "no link". Any other trailing
// bracket, such as "press [Enter]", isn't a link.
const LINK =
  /\[\s*(#\d+|!\d+|[\w.-]+(?:\/[\w.-]+)+[#!]\d+|https?:\/\/[^\s[\]]+|no\s+link)\s*\]$/i
const NO_LINK = /^no\s+link$/i
// "(recommend <choice>" or "(recommend: <choice>", up to the start of the choice.
const RECOMMEND = /\(recommend(?:\s*:\s*|\s+)(?=\S)/gi

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
  if (draft.kind === 'DECIDE' && !findRecommendation(draft.body)) {
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

type Found = { start: number; end: number; choice: string; closed: boolean }

/**
 * The first "(recommend <choice>)" in a body: where it starts and ends, the choice, and whether
 * it's closed. The closing parenthesis is found by depth, so "(recommend B (cheaper, (mostly)))"
 * keeps the whole choice. An unclosed one runs to the end of the body.
 */
function findRecommendation(body: string): Found | null {
  RECOMMEND.lastIndex = 0
  for (let match = RECOMMEND.exec(body); match; match = RECOMMEND.exec(body)) {
    const from = match.index + match[0].length
    let depth = 1
    let at = from
    for (; at < body.length && depth > 0; at++) {
      if (body[at] === '(') depth++
      else if (body[at] === ')') depth--
    }
    const choice = body.slice(from, depth === 0 ? at - 1 : at).trim()
    if (choice) return { start: match.index, end: at, choice, closed: depth === 0 }
  }
  return null
}

/** The recommended choice of a DECIDE body, for the Accept button; null when there isn't one. */
export function recommendation(body: string): string | null {
  const found = findRecommendation(body)
  return found?.closed ? found.choice : null
}

/** The last "Ruling (...)" line of a description, which records how the item was settled. */
export function rulingIn(description: string): string | null {
  const lines = description.split('\n').reverse()
  return lines.find(line => line.startsWith('Ruling (')) ?? null
}

/** The date of the last "Revised (<date>): ..." line of a description; null when there's none. */
export function revisedIn(description: string): string | null {
  const lines = description.split('\n').reverse()
  const line = lines.find(one => one.startsWith('Revised ('))
  return line ? (/^Revised \(([^)]*)\)/.exec(line)?.[1] ?? null) : null
}

export type BodyParts = { question: string; recommendation: string | null; link: string | null }

/** A body split for display: the question alone, the recommended choice, and the link token. */
export function splitBody(body: string): BodyParts {
  let rest = body.trim()
  let link: string | null = null
  const linked = LINK.exec(rest)
  if (linked) {
    const token = linked[1] ?? ''
    link = NO_LINK.test(token) ? null : token
    rest = rest.slice(0, linked.index).trim()
  }
  const found = findRecommendation(rest)
  if (!found?.closed) return { question: rest, recommendation: null, link }
  rest = `${rest.slice(0, found.start)} ${rest.slice(found.end)}`.replace(/\s{2,}/g, ' ').trim()
  return { question: rest, recommendation: found.choice, link }
}

/** The first comma or semicolon outside parentheses; -1 when there's none. */
function topLevelBreak(text: string): number {
  let depth = 0
  for (let at = 0; at < text.length; at++) {
    const ch = text[at]
    if (ch === '(') depth++
    else if (ch === ')') depth = Math.max(0, depth - 1)
    else if (depth === 0 && (ch === ',' || ch === ';')) return at
  }
  return -1
}

/**
 * A recommendation as a card shows it: the choice for the tag beside the label (up to the first
 * comma or semicolon outside parentheses, at most `max` characters) and the qualifier after it, if any.
 */
export function shortRecommendation(text: string, max = 24): { choice: string; qualifier: string | null } {
  const cut = topLevelBreak(text)
  const head = (cut < 0 ? text : text.slice(0, cut)).trim()
  const qualifier = cut < 0 ? null : text.slice(cut + 1).trim() || null
  const choice = head.length > max ? `${head.slice(0, max - 1).trimEnd()}…` : head
  return { choice, qualifier: head.length > max ? text.trim() : qualifier }
}
