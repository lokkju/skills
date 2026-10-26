// The queue as plain data: labelling, applying task-list changes, and the text views.
// Nothing here touches `$`, so it's tested directly.

import type { Counters, Item, Kind, Status } from '../types'
import { LETTER, formatSubject, parseDraft, parseLabel, problems, revisedIn, rulingIn, rulingText, splitBody, supersededIn } from './subject'
import type { Draft } from './subject'

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
export function coverLabels(counters: Counters, items: readonly Pick<Item, 'label'>[]): Counters {
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

/** An item completed with a dated ruling line added to its description. */
export function settle(item: Item, ruling: string, date: string): Item {
  return {
    ...item,
    status: 'completed',
    description: [item.description, `Ruling (${date}): ${ruling}`].filter(Boolean).join('\n'),
  }
}

/**
 * A settled item whose ruling stopped holding, marked with a dated "Superseded" line naming the
 * item that replaces it. It stays completed; the line is what shows it superseded.
 */
export function supersede(item: Item, by: string, why: string, date: string): Item {
  const line = `Superseded (${date}) by ${by}${why ? `: ${why}` : ''}`
  return { ...item, description: [item.description, line].filter(Boolean).join('\n') }
}

/** The settled item `label` replaces, by the Superseded line on it; undefined when there's none. */
export function replaces(items: readonly Item[], label: string): Item | undefined {
  if (!/^[DA]\d+$/.test(label)) return undefined
  return items.filter(item => supersededIn(item.description)?.by === label).at(-1)
}

/** What an item replaces, as its card and the listing say it: "D2: <its ruling, or its question>". */
export function replacedText(old: Item): string {
  const ruling = rulingIn(old.description)
  return `${old.label}: ${ruling ? rulingText(ruling) : splitBody(old.body).question}`
}

/**
 * An open item revised in place: a new body if given, and a dated "Revised" line appended to the
 * description, which says what changed. A changed body is recorded on that line too, so the
 * description keeps what the user was shown before.
 */
export function revise(item: Item, change: { body?: string; note?: string }, date: string): Item {
  const body = change.body ?? item.body
  const note = change.note?.trim() || 'revised'
  const was = body !== item.body ? ` (was: ${item.body})` : ''
  return {
    ...item,
    body,
    description: [item.description, `Revised (${date}): ${note}${was}`].filter(Boolean).join('\n'),
  }
}

/**
 * The queue rebuilt at session start or after compaction, merging rather than replacing:
 * - task items come from the task files, or from `current` when they couldn't be read;
 * - an item `closed` records as settled stays settled while its task file still shows it open,
 *   keeps the ruling and Superseded line the plugin recorded, and stays in the queue if its task
 *   file has gone (Claude Code deletes a list's task files once every task in it is completed);
 * - ledger items are the stored ledger and the ones already in `current`, the latter winning.
 */
export function mergeHydrated(
  fromFiles: readonly Item[] | null,
  current: readonly Item[],
  stored: { ledger: readonly Item[]; closed: Readonly<Record<string, Item>> },
): Item[] {
  const tasks = (fromFiles ?? current.filter(item => item.source === 'tasks')).map(item => {
    const settled = stored.closed[item.id]
    if (!settled) return item
    // The task file may lack the ruling decision_close kept, and a Superseded line decision_supersede added.
    const ruling = rulingIn(settled.description)
    const marked = settled.description.split('\n').reverse().find(line => line.startsWith('Superseded ('))
    let description = item.description
    if (ruling && !rulingIn(description)) description = [description, ruling].filter(Boolean).join('\n')
    if (marked && !description.split('\n').includes(marked)) description = [description, marked].filter(Boolean).join('\n')
    return { ...item, status: isOpen(item) ? settled.status : item.status, description }
  })
  const gone = Object.values(stored.closed).filter(
    item => item.source === 'tasks' && !tasks.some(task => task.id === item.id),
  )
  const ledger = current.filter(item => item.source === 'ledger')
  for (const item of stored.ledger) {
    if (item.source === 'ledger' && !ledger.some(one => one.id === item.id)) ledger.push(item)
  }
  return [...tasks, ...gone, ...ledger]
}

/**
 * What a TaskUpdate's new subject does to the queue, decided before the tool runs:
 * - `pass`: not a decision subject; the update goes through as written.
 * - `subject`: go through with this subject, which keeps or carries the item's label.
 * - `assign`: the task becomes a new item (or changes kind), so it needs a fresh label.
 * - `deny`: refuse the update, for this reason.
 */
export type Retitle =
  | { kind: 'pass' }
  | { kind: 'subject'; subject: string }
  | { kind: 'assign'; draft: Draft }
  | { kind: 'deny'; reason: string }

export function retitle(items: readonly Item[], taskId: string, subject: string): Retitle {
  const draft = parseDraft(subject)
  if (!draft) return { kind: 'pass' }
  const found = problems(draft)
  if (found.length) return { kind: 'deny', reason: found.join('; ') }
  const id = String(taskId)
  const current = items.find(one => one.source === 'tasks' && one.id === id)
  // An item the hook never labelled ("D?") takes a label like a new one.
  const known = current && labelNumber(current.label) > 0 ? current : undefined
  const labelled = parseLabel(subject)
  if (labelled) {
    const label = `${labelled.letter}${labelled.number}`
    if (known && label !== known.label) {
      return { kind: 'deny', reason: `task ${id} is ${known.label}; keep its label or drop it and the hook adds it` }
    }
    if (labelled.letter !== LETTER[labelled.kind]) {
      return { kind: 'deny', reason: `${label} can't be a ${labelled.kind} item; drop the label and the hook adds the right one` }
    }
    const holder = items.find(one => one.label === label && !(one.source === 'tasks' && one.id === id))
    if (holder) return { kind: 'deny', reason: `${label} is already in use; drop the label and the hook adds one` }
    return { kind: 'subject', subject }
  }
  if (known && known.label[0] === LETTER[draft.kind]) return { kind: 'subject', subject: formatSubject(known.label, draft) }
  return { kind: 'assign', draft }
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
    const superseded = isOpen(item) ? null : supersededIn(item.description)
    const tag = isOpen(item) ? age(item.created, now) : superseded ? 'superseded' : item.status
    if (tag) tags.push(tag)
    const revised = isOpen(item) ? revisedIn(item.description) : null
    if (revised) tags.push(`revised ${revised}`)
    lines.push(subjectOf(item) + (tags.length ? `  (${tags.join(', ')})` : ''))
    const ruling = rulingIn(item.description)
    if (ruling) lines.push(`    ${ruling}`)
    if (superseded) lines.push(`    Superseded (${superseded.date}) by ${superseded.by}${superseded.why ? `: ${superseded.why}` : ''}`)
    const old = replaces(items, item.label)
    if (old) lines.push(`    replaces ${replacedText(old)}`)
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
