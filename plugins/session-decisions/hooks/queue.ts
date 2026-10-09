// The queue as plain data: labelling, applying task-list changes, and the text views.
// Nothing here touches `$`, so it's tested directly.

import type { Counters, Item, Kind, Status } from '../types'
import { LETTER, parseDraft, parseLabel, rulingIn } from './subject'

const OPEN: ReadonlySet<string> = new Set(['pending', 'in_progress'])

export const isOpen = (item: Item): boolean => OPEN.has(item.status)

/** The number part of a label; 0 for an item the hook didn't get to label ("D?"). */
export function labelNumber(label: string): number {
  const digits = label.slice(1)
  return /^\d+$/.test(digits) ? Number(digits) : 0
}

/** Next number for this kind: one past both the counter and any label already in use. */
export function nextNumber(counters: Counters, items: readonly Item[], kind: Kind): number {
  const letter = LETTER[kind]
  const inUse = items.filter(item => item.label[0] === letter).map(item => labelNumber(item.label))
  return Math.max(counters[letter], ...inUse) + 1
}

/** Counters raised to cover every label in `items`, so numbering never repeats one. */
export function coverLabels(counters: Counters, items: readonly Item[]): Counters {
  const out = { ...counters }
  for (const item of items) {
    const letter = item.label[0] as 'D' | 'A'
    if (letter === 'D' || letter === 'A') out[letter] = Math.max(out[letter], labelNumber(item.label))
  }
  return out
}

function asStatus(value: unknown): Status {
  return value === 'in_progress' || value === 'completed' ? value : 'pending'
}

/** A task-list task as an item, or null when it isn't a decision item. */
export function itemFromTask(task: {
  id: string
  subject: string
  description?: string
  status?: unknown
}): Item | null {
  const label = parseLabel(task.subject)
  const common = {
    id: String(task.id),
    source: 'tasks' as const,
    status: asStatus(task.status),
    description: task.description ?? '',
  }
  if (label) {
    return { ...common, label: `${label.letter}${label.number}`, kind: label.kind, body: label.body }
  }
  const draft = parseDraft(task.subject) // created while the hook wasn't running: unlabelled
  if (!draft) return null
  return { ...common, label: `${LETTER[draft.kind]}?`, kind: draft.kind, body: draft.body }
}

/** Items with `item` replacing the one of the same source and id, or appended. */
export function upsert(items: readonly Item[], item: Item): Item[] {
  const at = items.findIndex(one => one.source === item.source && one.id === item.id)
  if (at < 0) return [...items, item]
  return items.map((one, i) => (i === at ? item : one))
}

export type TaskChange = {
  taskId: string
  subject?: string
  description?: string
  status?: Status | 'deleted'
}

/** Apply a TaskUpdate that succeeded to the items; `items` itself when it touches none of them. */
export function applyUpdate(items: readonly Item[], change: TaskChange): Item[] {
  const id = String(change.taskId)
  const current = items.find(one => one.source === 'tasks' && one.id === id)
  if (change.status === 'deleted') return current ? items.filter(one => one !== current) : (items as Item[])
  if (!current) {
    if (change.subject === undefined) return items as Item[]
    const added = itemFromTask({ id, subject: change.subject, description: change.description, status: change.status })
    return added ? [...items, added] : (items as Item[])
  }
  let next: Item | null = { ...current }
  if (change.subject !== undefined) {
    const parsed = itemFromTask({ id, subject: change.subject })
    next = parsed && { ...parsed, created: current.created }
  }
  if (!next) return items.filter(one => one !== current)
  if (change.description !== undefined) next.description = change.description
  else next.description = current.description
  next.status = change.status ? asStatus(change.status) : current.status
  return items.map(one => (one === current ? (next as Item) : one))
}

/** Items in the order they were asked, oldest first; unknown times sort first, by label. */
export function ordered(items: readonly Item[]): Item[] {
  return [...items].sort(
    (a, b) =>
      (a.created ?? 0) - (b.created ?? 0) ||
      a.label[0]!.localeCompare(b.label[0]!) ||
      labelNumber(a.label) - labelNumber(b.label),
  )
}

export const subjectOf = (item: Item): string => `${item.label} ${item.kind}: ${item.body}`

export function age(created: number | undefined, now: number): string {
  if (!created) return ''
  let minutes = Math.max(0, Math.floor((now - created) / 60_000))
  if (minutes < 60) return `waiting ${minutes}m`
  let hours = Math.floor(minutes / 60)
  minutes %= 60
  if (hours < 24) return `waiting ${hours}h ${minutes}m`
  const days = Math.floor(hours / 24)
  hours %= 24
  return `waiting ${days}d ${hours}h`
}

/** The `/decisions` listing: open items, or with `all` every item and its ruling. */
export function listing(items: readonly Item[], now: number, all = false): string {
  const shown = ordered(items).filter(item => all || isOpen(item))
  if (shown.length === 0) return all ? 'No decisions or actions this session.' : 'No open decisions or actions.'
  const lines: string[] = []
  for (const item of shown) {
    const tags = item.source === 'ledger' ? ['ledger'] : []
    const tag = isOpen(item) ? age(item.created, now) : item.status
    if (tag) tags.push(tag)
    lines.push(subjectOf(item) + (tags.length ? `  (${tags.join(', ')})` : ''))
    const ruling = rulingIn(item.description)
    if (ruling) lines.push(`    ${ruling}`)
  }
  return lines.join('\n')
}

/** What the model is told after compaction or resume; null when nothing is open. */
export function reminder(items: readonly Item[]): string | null {
  const open = ordered(items).filter(isOpen)
  if (open.length === 0) return null
  const lines = ["Open items in this session's decision queue, still waiting on the user:"]
  for (const item of open) {
    lines.push(`- ${subjectOf(item)}${item.source === 'ledger' ? ' (fallback ledger)' : ''}`)
  }
  return lines.join('\n')
}

/**
 * The prompt draft with one answer line per label: `lines` replace any line already answering
 * the same label and are appended otherwise, so several presses build one message of answers.
 */
export function mergeAnswers(draft: string, lines: readonly string[]): string {
  const out = draft.replace(/\s+$/, '').split('\n').filter((line, i, all) => line !== '' || i < all.length - 1)
  if (out.length === 1 && out[0] === '') out.pop()
  for (const line of lines) {
    const label = line.slice(0, line.indexOf(':') + 1)
    const at = out.findIndex(one => one.startsWith(label))
    if (at < 0) out.push(line)
    else out[at] = line
  }
  return out.join('\n')
}
