// The reconcile pass: what the assistant is handed to check every open item against the session
// and propose closing, revising or keeping each in one table. The plugin gathers the facts (the
// open items with their age and link, and the lines of the person's own messages that name them);
// judging the evidence and asking is the assistant's, as the skill describes. Nothing here touches
// `$`, so it's tested directly.

import type { Item } from '../types'
import { age, isOpen, ordered } from './queue'
import { revisedIn, splitBody } from './subject'

/** The parts of a `$.session.messages()` row this module reads. */
export type Said = { role?: string; text?: string }

/** The prompt `/decisions reconcile` sends, ahead of the brief. It names no label of its own. */
export const RECONCILE = `Reconcile this session's decision queue, as the session-decisions skill describes. Check every open item below against this session for evidence that it's settled: my own words answering it, by label or by subject; a merged pull request or closed issue it links to, which you check now; or a later item that supersedes it. Propose in one table, one row per item, close (with my words quoted, or the forge state named, as the ruling), update (the new subject, when the question or recommendation has drifted), or keep, which is the answer whenever the evidence is ambiguous. Change nothing until I answer; then apply what I approve in one message, every call together, and report what changed.`

// User-role rows the person didn't write: the plugin's own reminder and the prompts
// `/decisions reconcile` and `/decisions wrap` send, which quote the queue and earlier answers, and
// the summary compaction leaves in place of the conversation it replaced.
const NOT_THEIRS = [
  /Open items in this session's decision queue/,
  /^(?:Reconcile|Wrap up) this session's decision queue/,
  /^This session is being continued from a previous conversation/,
]
// A line that only repeats the queue: a listing line (`D3 DECIDE: ...`) or a table row.
const QUEUE_LINE = /^\s*(?:[-*]\s+)?(?:[DA]\d+ (?:DECIDE|ACTION):|\|)/
const MAX_LINE = 200

const cell = (text: string) => text.replace(/\s+/g, ' ').replace(/\|/g, '\\|').trim()

/** The label as a word of its own: `D3` but not `D31`, `D3.js` or `#D3`. */
function labelPattern(label: string): RegExp {
  return new RegExp(`(?<![\\w./#-])${label}(?![\\w-]|\\.\\w)`)
}

/**
 * The open items as a markdown table: label (marked when it's a fallback-ledger item, which closes
 * with decision_close), kind, how long it has waited and when it was last revised, its link, and
 * its text with the recommendation.
 */
export function inventory(items: readonly Item[], now: number): string {
  const open = ordered(items).filter(isOpen)
  if (open.length === 0) return 'No open decisions or actions.'
  const rows = ['| Label | Kind | Waiting | Link | Item |', '| --- | --- | --- | --- | --- |']
  for (const item of open) {
    const parts = splitBody(item.body)
    const waited = age(item.created, now).replace('waiting ', '') || 'unknown'
    const revised = revisedIn(item.description)
    const text = parts.recommendation ? `${parts.question} (recommend ${parts.recommendation})` : parts.question
    rows.push(
      `| ${item.label}${item.source === 'ledger' ? ' (ledger)' : ''} | ${item.kind} | ${waited}${revised ? `, revised ${revised}` : ''} | ${cell(parts.link ?? 'none')} | ${cell(text)} |`,
    )
  }
  return rows.join('\n')
}

/**
 * The lines of the person's own messages that name each label, oldest first, the newest `max` per
 * label; a label nothing names is left out. These are the "D3: go with X" and "A2: done" lines the
 * queue's buttons write, and anything else the person typed about an item by its label.
 */
export function mentions(messages: readonly Said[], labels: readonly string[], max = 3): Map<string, string[]> {
  const found = new Map<string, string[]>()
  // An item the hook never labelled ("D?") has no label the person could have written.
  const patterns = labels.filter(label => /^[DA]\d+$/.test(label)).map(label => [label, labelPattern(label)] as const)
  for (const message of messages) {
    if (message.role !== 'user') continue
    const text = message.text ?? ''
    if (NOT_THEIRS.some(pattern => pattern.test(text))) continue
    for (const raw of text.split('\n')) {
      const line = raw.trim()
      if (!line || QUEUE_LINE.test(line)) continue
      for (const [label, pattern] of patterns) {
        if (!pattern.test(line)) continue
        const lines = found.get(label) ?? []
        lines.push(line.length > MAX_LINE ? `${line.slice(0, MAX_LINE - 1)}…` : line)
        found.set(label, lines.slice(-max))
      }
    }
  }
  return found
}

/** What a reconcile pass starts from: the open items, then what the person wrote that names them. */
export function brief(items: readonly Item[], messages: readonly Said[], now: number): string {
  const open = ordered(items).filter(isOpen)
  if (open.length === 0) return 'No open decisions or actions.'
  const said = mentions(
    messages,
    open.map(item => item.label),
  )
  // A label two open items share (as two unlabelled "D?" items do) is listed once.
  const seen = new Set<string>()
  const lines = [`Open items (${open.length}):`, '', inventory(open, now), '']
  if (said.size === 0) {
    lines.push("The user's messages name none of these labels; look for answers by subject.")
  } else {
    lines.push("Lines of the user's messages that name them, oldest first:")
    for (const item of open) {
      if (seen.has(item.label)) continue
      seen.add(item.label)
      for (const line of said.get(item.label) ?? []) lines.push(`- ${item.label}: "${line}"`)
    }
  }
  return lines.join('\n')
}
