// What the conversation itself says about its decision queue, read from its transcript, so a
// session that comes back under a new id (a resume that changes it, a restart, a fork) can find
// the stored record it came from. Nothing here touches `$`, so it's tested directly.

import type { Item, SessionRecord } from '../types'
import { isOpen } from './queue'
import { parseLabel } from './subject'

/** The parts of a `$.session.messages()` row this module reads. */
export type Message = {
  text?: string
  toolUses?: readonly { tool: string; result?: unknown; text?: string; isError?: true }[]
}

/** One fallback-ledger item as the conversation saw it: every body it had, and whether it was closed. */
export type Seen = { bodies: string[]; closed: boolean }

/**
 * What the transcript shows of the queue: the ledger items decision_add, decision_update and
 * decision_close reported, by label, and every label the conversation has used, task-list ones
 * included, so numbering never hands one out again.
 */
export type Trail = { ledger: Map<string, Seen>; labels: string[] }

const CLOSED = /^([DA]\d+) closed$/
const REMINDER_HEAD = "Open items in this session's decision queue"
const REMINDER_LINE = /^- ([DA]\d+ (?:DECIDE|ACTION): [\s\S]*?) \(fallback ledger\)$/

/** A tool's answer as text: the stored record when it's a string, otherwise what the model read. */
function answer(use: { result?: unknown; text?: string }): string {
  return (typeof use.result === 'string' ? use.result : (use.text ?? '')).trim()
}

const named = (tool: string, name: string) => tool === name || tool.endsWith(`__${name}`)

export function trailOf(messages: readonly Message[]): Trail {
  const ledger = new Map<string, Seen>()
  const labels = new Set<string>()
  const saw = (label: string, body: string) => {
    labels.add(label)
    const seen = ledger.get(label) ?? { bodies: [], closed: false }
    if (!seen.bodies.includes(body)) seen.bodies.push(body)
    ledger.set(label, seen)
  }
  for (const message of messages) {
    const text = message.text ?? ''
    if (text.includes(REMINDER_HEAD)) {
      for (const line of text.split('\n')) {
        const found = parseLabel(REMINDER_LINE.exec(line.trim())?.[1] ?? '')
        if (found) saw(`${found.letter}${found.number}`, found.body)
      }
    }
    for (const use of message.toolUses ?? []) {
      if (use.isError) continue
      if (named(use.tool, 'decision_add') || named(use.tool, 'decision_update')) {
        const found = parseLabel(answer(use))
        if (found) saw(`${found.letter}${found.number}`, found.body)
      } else if (named(use.tool, 'decision_close')) {
        const label = CLOSED.exec(answer(use))?.[1]
        const seen = label ? ledger.get(label) : undefined
        if (seen) seen.closed = true
      } else if (use.tool === 'TaskCreate') {
        const subject = (use.result as { task?: { subject?: unknown } } | undefined)?.task?.subject
        const found = typeof subject === 'string' ? parseLabel(subject) : null
        if (found) labels.add(`${found.letter}${found.number}`)
      }
    }
  }
  return { ledger, labels: [...labels] }
}

/**
 * Whether a stored record is the one this conversation's ledger items came from: it holds every
 * ledger item the transcript reported, by label, closed wherever the transcript saw it closed, and
 * at least one word for word as the transcript has it. A label survives decision_update, so an
 * item the other session revised later still matches; the word-for-word one keeps two sessions
 * whose ledgers both start at D1 apart. Null when the transcript reports no ledger items, which
 * says nothing either way.
 */
export function ledgerMatches(record: SessionRecord, trail: Trail): boolean | null {
  if (trail.ledger.size === 0) return null
  let exact = false
  for (const [label, seen] of trail.ledger) {
    const item: Item | undefined = record.ledger.find(one => one.source === 'ledger' && one.label === label)
    if (!item || (seen.closed && isOpen(item))) return false
    if (seen.bodies.includes(item.body)) exact = true
  }
  return exact
}
