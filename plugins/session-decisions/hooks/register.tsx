import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Counters, Item, Kind, SessionRecord } from '../types'
import {
  applyUpdate,
  age,
  coverLabels,
  isOpen,
  itemFromTask,
  listing,
  mergeAnswers,
  mergeHydrated,
  nextNumber,
  ordered,
  reminder,
  retitle,
  revise,
  settle,
  subjectOf,
  upsert,
} from './queue'
import { codeSpans, dialogQuestion, githubShort, isShortLink, linkHref, plainText } from './markdown'
import { LETTER, formatSubject, parseDraft, problems, recommendation, revisedIn, rulingIn, shortRecommendation, splitBody } from './subject'

type $ = EngineInterface

const PANE = 'session-decisions'
const ADD_TOOL = 'mcp__session-decisions__decision_add'
const CLOSE_TOOL = 'mcp__session-decisions__decision_close'
const UPDATE_TOOL = 'mcp__session-decisions__decision_update'
const SAFE_ID = /^[A-Za-z0-9._-]{1,128}$/
const KEEP_RECORDS = 100
const RECORD = /^(session|list):/

const items = atom({ plugin: 'session-decisions', key: 'items' } as const, [] as Item[])
const counters = atom({ plugin: 'session-decisions', key: 'counters' } as const, { D: 0, A: 0 } as Counters)
const created = atom({ plugin: 'session-decisions', key: 'created' } as const, {} as Record<string, number>)
// Task-list items decision_close settled while TaskUpdate wasn't available, by task id.
const closed = atom({ plugin: 'session-decisions', key: 'closed' } as const, {} as Record<string, Item>)
const showSettled = atom({ plugin: 'session-decisions', key: 'showSettled' } as const, false)
const paneDismissed = atom({ plugin: 'session-decisions', key: 'paneDismissed' } as const, false)
const autoOpened = atom({ plugin: 'session-decisions', key: 'autoOpened' } as const, false)
// Whether this surface docks panes beside the transcript: fixed per session on the terminal and
// learnt from the footer's drawing. A module variable, since a drawing may not write state.
let docks = false
// The screen's size as the last drawing saw it, so More can fit the dialog to it; null until
// something has drawn on a surface that measures.
let screen: { rows: number; columns: number } | null = null
function measure(site: { viewport?: { rows: number; columns: number } }) {
  if (site.viewport && site.viewport.rows > 0 && site.viewport.columns > 0) screen = { rows: site.viewport.rows, columns: site.viewport.columns }
}
const ascii = atom({ plugin: 'session-decisions', key: 'ascii' } as const, false)

const DAY = 24 * 60 * 60 * 1000
// Claude Code's own theme keys, so the cards follow whichever theme the person runs.
const ACCENT = 'claude'
const KIND_COLOR = { DECIDE: 'warning', ACTION: 'suggestion' } as const

function safeId(id: unknown): string | null {
  return typeof id === 'string' && SAFE_ID.test(id) && id !== '.' && id !== '..' ? id : null
}

async function sessionId($: $): Promise<string | null> {
  return safeId(await $.session.id())
}

/** The task list this session shares with others, when CLAUDE_CODE_TASK_LIST_ID names one. */
async function sharedList($: $): Promise<string | null> {
  return safeId(await $.env.get('CLAUDE_CODE_TASK_LIST_ID'))
}

async function tasksRoot($: $): Promise<string> {
  return `${(await $.env.get('CLAUDE_CONFIG_DIR')) || `${await $.env.get('HOME')}/.claude`}/tasks`
}

/** The task list Claude Code keeps for this session (read only; the format is undocumented). */
async function tasksDir($: $, sid: string): Promise<string> {
  return `${await tasksRoot($)}/${(await sharedList($)) ?? sid}`
}

/**
 * Where this session's record lives in `$.store`: under the shared task list when there is one,
 * since a fork or a resume can change the session id while the list stays; otherwise under the
 * session, whose id is its task list's name.
 */
async function recordKey($: $, sid: string): Promise<string> {
  const list = await sharedList($)
  return list ? `list:${list}` : `session:${sid}`
}

/** The decision items in the task files, or null when the directory can't be read. */
async function readTaskFiles($: $, dir: string): Promise<Item[] | null> {
  let entries
  try {
    entries = await $.fs.list(dir)
  } catch {
    return (await $.fs.exists(dir).catch(() => true)) ? null : []
  }
  const found: Item[] = []
  for (const entry of entries) {
    if (entry.kind !== 'file' || !entry.name.endsWith('.json')) continue
    try {
      const data = JSON.parse(await $.fs.read(`${dir}/${entry.name}`))
      if (data && typeof data === 'object' && typeof data.subject === 'string') {
        const item = itemFromTask({ ...data, id: String(data.id ?? entry.name.replace(/\.json$/, '')) })
        if (item) found.push(item)
      }
    } catch {
      // unreadable or half-written: skip it, as the task list itself would
    }
  }
  return found
}

// $.tool.list() names a deferred tool before the model loads it (Claude Code 2.1.286), so a
// refusal that points at TaskUpdate says how to reach it.
const LOAD = ' If TaskUpdate is deferred, load it with ToolSearch first.'

/** Whether TaskUpdate exists in this session, loaded or deferred; false when the host can't say. */
async function taskUpdateOffered($: $): Promise<boolean> {
  try {
    return (await $.tool.list()).some(tool => tool.name === 'TaskUpdate')
  } catch {
    return false
  }
}

function asRecord(value: unknown): SessionRecord {
  const raw = value as Partial<SessionRecord> | undefined
  return {
    counters: { D: Number(raw?.counters?.D) || 0, A: Number(raw?.counters?.A) || 0 },
    created: raw?.created && typeof raw.created === 'object' ? raw.created : {},
    ledger: Array.isArray(raw?.ledger) ? raw.ledger : [],
    closed: raw?.closed && typeof raw.closed === 'object' ? raw.closed : {},
    touched: Number(raw?.touched) || 0,
  }
}

/**
 * This session's stored record. The first read under a shared task list takes over the record
 * this session kept under its own id. A session with no record whose task files hold decision
 * items was forked from another (backgrounding a session gives it a new id and a copy of the task
 * files), so it starts from that session's record.
 */
async function loadRecord($: $, sid: string, fromFiles: readonly Item[] | null): Promise<SessionRecord> {
  const key = await recordKey($, sid)
  const stored = await $.store.get(key)
  if (stored !== undefined) return asRecord(stored)
  const own = key === `session:${sid}` ? undefined : await $.store.get(`session:${sid}`)
  const found = own !== undefined ? asRecord(own) : key.startsWith('session:') ? await forkedFrom($, sid, fromFiles ?? []) : null
  if (!found) return asRecord(undefined)
  await $.store.set(key, { ...found, touched: await $.clock.now() })
  if (own !== undefined) await $.store.delete(`session:${sid}`)
  return found
}

/**
 * The record of the session this one was forked from: the most recently used one whose task list
 * holds every decision item in this one's by task id and label, and at least one of them word for
 * word. A label survives a revised subject, so items either session changed after the fork still
 * match; the word-for-word one keeps two short lists that both start at task 1, D1 apart.
 */
async function forkedFrom($: $, sid: string, carried: readonly Item[]): Promise<SessionRecord | null> {
  if (carried.length === 0) return null
  const root = await tasksRoot($)
  const covers = (record: SessionRecord) =>
    carried.every(item => record.counters[LETTER[item.kind]] >= Number(item.label.slice(1)))
  let best: SessionRecord | null = null
  for (const key of await $.store.keys()) {
    const other = key.startsWith('session:') ? safeId(key.slice('session:'.length)) : null
    if (!other || other === sid) continue
    const record = asRecord(await $.store.get(key))
    if (!covers(record) || (best && record.touched <= best.touched)) continue
    const theirs = await readTaskFiles($, `${root}/${other}`)
    const same = (item: Item, exact: boolean) =>
      theirs?.some(one => one.id === item.id && one.label === item.label && (!exact || one.body === item.body))
    if (carried.every(item => same(item, false)) && carried.some(item => same(item, true))) best = record
  }
  return best
}

/**
 * Write this session's record. Sessions on one shared task list write one record, so what another
 * wrote since is kept: the higher counters, every creation time, and every ledger item, with the
 * settled copy of an item either side settled.
 */
async function persist($: $): Promise<void> {
  const sid = await sessionId($)
  if (!sid) return
  const key = await recordKey($, sid)
  const stored = asRecord(await $.store.get(key))
  const mine = await read($, counters)
  const ledger = (await read($, items)).filter(item => item.source === 'ledger')
  const theirs = stored.ledger.filter(item => {
    const own = ledger.find(one => one.id === item.id)
    return !own || (isOpen(own) && !isOpen(item))
  })
  const record: SessionRecord = {
    counters: { D: Math.max(mine.D, stored.counters.D), A: Math.max(mine.A, stored.counters.A) },
    created: { ...stored.created, ...(await read($, created)) },
    ledger: [...ledger.filter(item => !theirs.some(one => one.id === item.id)), ...theirs],
    closed: await read($, closed),
    touched: await $.clock.now(),
  }
  await $.store.set(key, record)
}

/** Drop all but the most recently used records, session and list alike, to stay under the store's cap. */
async function prune($: $): Promise<void> {
  const keys = (await $.store.keys()).filter(key => RECORD.test(key))
  if (keys.length <= KEEP_RECORDS) return
  const touched = await Promise.all(
    keys.map(async key => [key, Number(((await $.store.get(key)) as SessionRecord)?.touched) || 0] as const),
  )
  touched.sort((a, b) => b[1] - a[1])
  await Promise.all(touched.slice(KEEP_RECORDS).map(([key]) => $.store.delete(key)))
}

/**
 * Rebuild the items from the task files, the stored ledger and stored rulings, keeping what's
 * already in state; the one read this plugin makes. `given` is the session id a hook's input
 * names, which a resume settles before `$.session.id()` does.
 */
async function hydrate($: $, given?: string): Promise<void> {
  const sid = safeId(given) ?? (await sessionId($))
  if (!sid) return
  const fromFiles = await readTaskFiles($, await tasksDir($, sid))
  const record = await loadRecord($, sid, fromFiles)
  const current = await read($, items)
  const settled = { ...record.closed, ...(await read($, closed)) }
  const known = { ...record.created, ...(await read($, created)) }
  const merged = mergeHydrated(fromFiles, current, { ledger: record.ledger, closed: settled }).map(item => ({
    ...item,
    created: item.created ?? current.find(one => one.id === item.id && one.source === item.source)?.created ?? known[item.label],
  }))
  await update($, items, () => merged)
  await update($, created, () => known)
  await update($, closed, () => settled)
  await update($, counters, now =>
    coverLabels({ D: Math.max(now.D, record.counters.D), A: Math.max(now.A, record.counters.A) }, merged),
  )
  await publish($, merged)
}

/** After the queue changes: close a sidebar this plugin opened once nothing is left open. */
async function publish($: $, list: readonly Item[]): Promise<void> {
  if (list.some(isOpen)) return
  await update($, paneDismissed, () => false)
  if (await read($, autoOpened)) {
    await update($, autoOpened, () => false)
    await $.ui.close({ id: PANE }).catch(() => undefined)
  }
}

/** Open the queue as a sidebar, unasked, where the surface docks panes and the person hasn't closed it. */
async function offerSidebar($: $): Promise<void> {
  if (!docks || (await read($, paneDismissed))) return
  const opened = await $.ui.open({ id: PANE, title: 'Decisions', columns: 52 }).catch(() => null)
  if (opened) await update($, autoOpened, () => true)
}

/** Open the queue because the person asked: it seats at any width and takes the keys. */
async function openPane($: $): Promise<void> {
  await update($, paneDismissed, () => false)
  await update($, autoOpened, () => false)
  await $.ui.open({ id: PANE, title: 'Decisions', focus: true, closeOnEscape: true, columns: 52 }).catch(() => undefined)
}

async function change($: $, fn: (list: Item[]) => Item[]): Promise<Item[]> {
  let after: Item[] = []
  await update($, items, list => (after = fn(list)))
  await publish($, after)
  return after
}

/** Assign the next label for `kind`; the versioned update keeps parallel creates apart. */
async function assign($: $, kind: Kind): Promise<string> {
  const list = await read($, items)
  let number = 0
  await update($, counters, now => {
    number = nextNumber(now, list, kind)
    return { ...now, [LETTER[kind]]: number }
  })
  const label = `${LETTER[kind]}${number}`
  const at = await $.clock.now()
  await update($, created, map => ({ ...map, [label]: at }))
  return label
}

async function announce($: $, item: Item): Promise<void> {
  $.ui.toast(`${item.label} added: ${plainText(splitBody(item.body).question)}`)
  await offerSidebar($)
}

const WRAP = `Wrap up this session's decision queue, as the session-decisions skill describes: for every open item below, ask me in one message whether to escalate it (create it in the project's tracker as a question for the right person, and link it), carry it forward (it stays open and goes in the handoff), or drop it. Then apply my answers, completing each settled item with a "Ruling (<YYYY-MM-DD>): ..." line, and report what changed.`

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // Each registration stands alone: a host that refuses one still gets the rest and the queue.
    const quietly = (work: Promise<unknown>) => work.catch(() => undefined)
    await quietly($.command.register({
      name: 'decisions',
      description: "Show this session's open decisions (D) and actions (A); 'all' adds settled ones, 'pane' opens the panel, 'wrap' settles each before the session ends.",
      argumentHint: '[all | pane | wrap]',
      immediate: true,
    }))
    await quietly($.tool.register({
      name: 'decision_add',
      description:
        'Fallback for the session-decisions queue, only when TaskCreate is not available: adds a DECIDE or ACTION item and returns its labelled subject.',
      inputSchema: {
        type: 'object',
        properties: {
          kind: { type: 'string', enum: ['DECIDE', 'ACTION'] },
          text: { type: 'string', description: '<question> (recommend <choice>) [<link>], or <what the user has to do> [<link>]' },
          description: { type: 'string', description: 'The context the user needs to answer.' },
        },
        required: ['kind', 'text'],
      },
    }))
    await quietly($.tool.register({
      name: 'decision_update',
      description:
        "Revises an open fallback-ledger item from decision_add when its question, options, recommendation or context change, keeping its label. Task-list items are revised with TaskUpdate instead.",
      inputSchema: {
        type: 'object',
        properties: {
          label: { type: 'string', description: 'The item label, such as D3.' },
          text: { type: 'string', description: 'The new subject after the colon, in the form decision_add takes; omit to keep it.' },
          description: { type: 'string', description: 'What changed and why, recorded as a dated Revised line.' },
        },
        required: ['label'],
      },
    }))
    await quietly($.tool.register({
      name: 'decision_close',
      description:
        "Settles a fallback-ledger item from decision_add with the user's ruling. Task-list items are settled with TaskUpdate instead, unless TaskUpdate is not available in this session.",
      inputSchema: {
        type: 'object',
        properties: {
          label: { type: 'string', description: 'The item label, such as D3.' },
          ruling: { type: 'string', description: 'The answer, as it should be recorded.' },
        },
        required: ['label', 'ruling'],
      },
    }))
    const term = await $.env.get('TERM')
    const noColor = await $.env.get('NO_COLOR')
    await update($, ascii, () => Boolean(noColor) || term === 'dumb')
    // 1.0 pinned the open count as a status notice; this version shows it in the footer instead.
    $.ui.status(undefined)
    await hydrate($)
    await quietly(prune($))
    return next(e)
  })

  // After compaction or resume, remind the model of what is still waiting on the user.
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if (e.source !== 'compact' && e.source !== 'resume') return result
    // Under --continue, $.session.id() still answers a placeholder here; the input has the real id.
    await hydrate($, e.session_id)
    const text = reminder(await read($, items))
    if (!text) return result
    return { ...result, additionalContext: [...(result.additionalContext ?? []), text] }
  })

  on('tool.call', { tool: 'TaskCreate' }, async ($, e, next) => {
    const draft = parseDraft(String(e.subject ?? ''))
    if (!draft) return next(e)
    const found = problems(draft)
    if (found.length) return { deny: `session-decisions: ${found.join('; ')}` }
    const label = await assign($, draft.kind)
    const ran = await next({ ...e, subject: formatSubject(label, draft) })
    if (ran.deny !== undefined || ran.isError) return ran
    const task = ran.result.task
    const item = itemFromTask({ id: task.id, subject: task.subject, description: String(e.description ?? '') })
    if (item) {
      const at = (await read($, created))[item.label] ?? (await $.clock.now())
      await change($, list => upsert(list, { ...item, created: at }))
      await update($, counters, now => coverLabels(now, [item]))
      await persist($)
      await announce($, item)
    }
    return ran
  })

  // A new subject keeps the item's label: the assistant writes drafts, and the hook relabels them
  // the way TaskCreate does, so a revised question never loses its D<n> or A<n>.
  on('tool.call', { tool: 'TaskUpdate' }, async ($, e, next) => {
    const taskId = String(e.taskId)
    let subject = e.subject
    let assigned: string | null = null
    if (subject !== undefined) {
      const plan = retitle(await read($, items), taskId, String(subject))
      if (plan.kind === 'deny') return { deny: `session-decisions: ${plan.reason}` }
      if (plan.kind === 'subject') subject = plan.subject
      if (plan.kind === 'assign') {
        assigned = await assign($, plan.draft.kind)
        subject = formatSubject(assigned, plan.draft)
      }
    }
    const ran = await next(subject === e.subject ? e : { ...e, subject })
    if (ran.deny !== undefined || ran.isError) return ran
    const before = await read($, items)
    const after = await change($, list =>
      applyUpdate(list, { taskId, subject, description: e.description, status: e.status }),
    )
    // A status set through TaskUpdate is in the task file now, so a ruling decision_close kept
    // for this task gives way to it.
    const handBack = e.status !== undefined && (await read($, closed))[taskId] !== undefined
    if (handBack) {
      await update($, closed, map => {
        const { [taskId]: _, ...rest } = map
        return rest
      })
    }
    if (after === before) {
      if (handBack) await persist($)
      return ran
    }
    await update($, counters, now => coverLabels(now, after))
    if (assigned) {
      const at = (await read($, created))[assigned] ?? (await $.clock.now())
      const fresh = await change($, list =>
        list.map(one => (one.source === 'tasks' && one.id === taskId ? { ...one, created: one.created ?? at } : one)),
      )
      const item = fresh.find(one => one.source === 'tasks' && one.id === taskId)
      if (item) await announce($, item)
    }
    await persist($)
    return ran
  })

  on('tool.call', { tool: ADD_TOOL }, async ($, e) => {
    const kind = e.kind === 'ACTION' ? 'ACTION' : e.kind === 'DECIDE' ? 'DECIDE' : null
    if (!kind) return { deny: 'session-decisions: kind must be DECIDE or ACTION' }
    const draft = { kind, body: String(e.text ?? '').trim() } as const
    const found = problems(draft)
    if (found.length) return { deny: `session-decisions: ${found.join('; ')}` }
    const label = await assign($, kind)
    const item: Item = {
      id: label,
      source: 'ledger',
      label,
      kind,
      body: draft.body,
      status: 'pending',
      description: String(e.description ?? ''),
      created: (await read($, created))[label],
    }
    await change($, list => [...list, item])
    await persist($)
    await announce($, item)
    return { result: subjectOf(item) }
  })

  on('tool.call', { tool: UPDATE_TOOL }, async ($, e) => {
    const label = String(e.label ?? '').trim().toUpperCase()
    const text = e.text === undefined ? undefined : String(e.text).trim()
    const note = e.description === undefined ? undefined : String(e.description).trim()
    if (!text && !note) return { deny: 'session-decisions: give the new text, a description of what changed, or both.' }
    const list = await read($, items)
    const matches = list.filter(item => item.label === label)
    const target = matches.find(item => item.source === 'ledger') ?? matches.find(isOpen) ?? matches[0]
    if (!target) return { deny: `session-decisions: ${label} isn't in this session's decision queue.` }
    if (target.source === 'tasks') {
      return {
        deny: (await taskUpdateOffered($))
          ? `session-decisions: ${label} is a task-list item; revise it with TaskUpdate, keeping its label, and add a Revised line to its description.${LOAD}`
          : `session-decisions: ${label} is a task-list item and TaskUpdate isn't available; close it with decision_close as superseded and add the revised item with decision_add.`,
      }
    }
    if (!isOpen(target)) return { deny: `session-decisions: ${label} is settled; add a new item with decision_add instead.` }
    if (text) {
      const found = problems({ kind: target.kind, body: text })
      if (found.length) return { deny: `session-decisions: ${found.join('; ')}` }
    }
    const date = new Date(await $.clock.now()).toISOString().slice(0, 10)
    const done = revise(target, { body: text || undefined, note }, date)
    await change($, now => now.map(item => (item.source === 'ledger' && item.id === target.id ? done : item)))
    await persist($)
    return { result: subjectOf(done) }
  })

  on('tool.call', { tool: CLOSE_TOOL }, async ($, e) => {
    const label = String(e.label ?? '').trim().toUpperCase()
    const ruling = String(e.ruling ?? '').trim()
    const list = await read($, items)
    const matches = list.filter(item => item.label === label)
    // A ledger item first; of two task items with one label, the open one.
    const target =
      matches.find(item => item.source === 'ledger') ?? matches.find(isOpen) ?? matches[0]
    if (!target) return { deny: `session-decisions: ${label} isn't in this session's decision queue.` }
    if (target.source === 'tasks' && (await taskUpdateOffered($))) {
      return {
        deny: `session-decisions: ${label} is a task-list item; mark it completed with TaskUpdate and add the ruling to its description.${LOAD}`,
      }
    }
    const date = new Date(await $.clock.now()).toISOString().slice(0, 10)
    const done = settle(target, ruling, date)
    await change($, now => now.map(item => (item.source === target.source && item.id === target.id ? done : item)))
    if (target.source === 'tasks') await update($, closed, map => ({ ...map, [target.id]: done }))
    await persist($)
    if (target.source === 'ledger') return { result: `${label} closed` }
    return {
      result: `${label} closed in the decision queue. TaskUpdate isn't available, so its task still shows it open; the queue keeps the ruling.`,
    }
  })

  on('command.run', { command: 'decisions' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const list = await read($, items)
    const now = await $.clock.now()
    if (arg === 'pane') {
      await update($, paneDismissed, () => false)
      const opened = await $.ui
        .open({ id: PANE, title: 'Decisions', focus: true, closeOnEscape: true, columns: 52 })
        .catch((err: unknown) => ({ isPlaced: false as const, reason: String(err) }))
      return { text: opened.isPlaced ? 'Decisions pane opened.' : `${listing(list, now)}\n\n(The pane can't be shown here: ${opened.reason})` }
    }
    if (arg === 'wrap') {
      const open = list.filter(isOpen)
      if (open.length === 0) return { text: 'No open decisions or actions; nothing to wrap up.' }
      void $.prompt.submit({ text: `${WRAP}\n\n${listing(list, now)}` }).catch(() => undefined)
      return { text: listing(list, now) }
    }
    return { text: listing(list, now, arg === 'all') }
  })

  // A sidebar the person closes stays closed until the queue empties.
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE && e.origin.kind === 'person') {
      await update($, paneDismissed, () => true)
      await update($, autoOpened, () => false)
    }
    return next(e)
  })

  // The footer: a count beside the mode labels that opens the queue. It also learns whether
  // this surface docks panes, which decides whether the sidebar may open unasked.
  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    docks = e.viewport?.isFullscreen === true
    measure(e)
    const open = (await read($, items)).filter(isOpen).length
    if (open === 0) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const plain = await read($, ascii)
    const below = await next(e)
    return (
      <Box flexDirection="row" gap={1}>
        {below}
        <Box key="decisions-footer" flexDirection="row">
          <Text color="warning">{plain ? '*' : '●'} </Text>
          <Button
            key="decisions-count"
            plain
            label={`${open} ${open === 1 ? 'decision' : 'decisions'}`}
            onPress={() => void openPane($)}
          />
        </Box>
      </Box>
    )
  })

  // In the transcript, a finished TaskCreate, decision_add or decision_update row draws as its item's card.
  on('ui.render', { component: 'ToolUse', props: { tool: 'TaskCreate' } }, async ($, e, next) => {
    measure(e)
    if (e.props.isRunning || e.props.isErrored || e.props.isInterrupted) return next(e)
    const id = String((e.props.output as { task?: { id?: unknown } } | undefined)?.task?.id ?? '')
    const item = (await read($, items)).find(one => one.source === 'tasks' && one.id === id)
    if (!item) return next(e)
    return drawCard($, e, item, { bordered: true, isNext: isOpen(item), compact: !isOpen(item) })
  })
  for (const tool of [ADD_TOOL, UPDATE_TOOL]) {
    on('ui.render', { component: 'ToolUse', props: { tool } }, async ($, e, next) => {
      measure(e)
      if (e.props.isRunning || e.props.isErrored || e.props.isInterrupted) return next(e)
      const label = String(e.props.output ?? '').split(' ')[0]
      const item = (await read($, items)).find(one => one.source === 'ledger' && one.label === label)
      if (!item) return next(e)
      return drawCard($, e, item, { bordered: true, isNext: isOpen(item), compact: !isOpen(item) })
    })
  }

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    measure(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = ordered(await read($, items))
    const settledOpen = await read($, showSettled)
    const plain = await read($, ascii)
    const docked = e.props.placement === 'dock'
    const open = list.filter(isOpen)
    const settled = list.filter(item => !isOpen(item))
    const accepts = open.flatMap(item => {
      const choice = item.kind === 'DECIDE' ? recommendation(item.body) : null
      return choice ? [`${item.label}: go with ${choice}`] : []
    })

    return (
      <Box flexDirection="column">
        <Box key="head" flexDirection="row" gap={1} marginBottom={1}>
          <Text bold>Decisions</Text>
          {open.length > 0 ? chip(Text, plain, `${open.length} open`, ACCENT) : <Text dimColor>Nothing waiting on you</Text>}
          <Box flexGrow={1} />
          {accepts.length > 1 && <Button key="accept-all" label="Accept all" variant="primary" onPress={answer($, accepts)} />}
        </Box>
        {await Promise.all(
          open.map(async (item, i) => (
            <Box key={`slot-${item.source}-${item.id}`} flexDirection="column" marginBottom={docked ? 1 : 0}>
              {await drawCard($, e, item, { bordered: !docked, isNext: i === 0, compact: false })}
            </Box>
          )),
        )}
        {settled.length > 0 && (
          <Button
            key="settled"
            plain
            dimColor
            label={`${settledOpen ? (plain ? 'v' : '⌄') : plain ? '>' : '›'} Settled (${settled.length})`}
            onPress={() => update($, showSettled, value => !value)}
          />
        )}
        {settledOpen &&
          (await Promise.all(
            settled.map(async item => (
              <Box key={`slot-${item.source}-${item.id}`} flexDirection="column">
                {await drawCard($, e, item, { bordered: false, isNext: false, compact: true })}
              </Box>
            )),
          ))}
      </Box>
    )
  })
}

/** Each press adds or replaces its item's line in the draft, so one message can answer several. */
function answer($: $, lines: string[]) {
  return () =>
    void $.prompt
      .read()
      .then(box => $.prompt.fill({ text: mergeAnswers(box.text, lines) }))
      .catch(() => undefined)
}

/** Copy a command from a card; a surface with no clipboard path says so in a toast. */
async function copy($: $, text: string, surface: Surface): Promise<void> {
  const done = await $.ui.copy({ text, surface }).catch((err: unknown) => ({ isCopied: false as const, reason: String(err) }))
  $.ui.toast(done.isCopied ? 'Copied' : `Couldn't copy: ${done.reason}`)
}

type Elements = ReturnType<$['ui']['resolve']>

function chip(Text: Elements['Text'], plain: boolean, text: string, color: string, dim = false) {
  return (
    <Text bold backgroundColor={dim ? undefined : color} color={dim ? undefined : 'inverseText'} dimColor={dim}>
      {plain ? `[${text}]` : ` ${text} `}
    </Text>
  )
}

/**
 * One item. Open: kind chip, label, recommendation tag, age, the question, the qualifier, and its
 * buttons; `bordered` draws a round frame (the accent one for `isNext`). Settled, or `compact`: one
 * dim line with the ruling.
 */
async function drawCard(
  $: $,
  site: Parameters<$['ui']['resolve']>[0],
  item: Item,
  opts: { bordered: boolean; isNext: boolean; compact: boolean },
) {
  const { Box, Button, Link, Markdown, Text } = $.ui.resolve(site) as Elements
  const plain = await read($, ascii)
  const now = await $.clock.now()
  const parts = splitBody(item.body)

  if (!isOpen(item) || opts.compact) {
    const ruling = (rulingIn(item.description) ?? '').replace(/^Ruling \(([^)]*)\):\s*/, '$1 · ')
    const moot = /·\s*moot\b/i.test(ruling)
    return (
      <Box key={`card-${item.source}-${item.id}`} flexDirection="row" gap={1}>
        <Text color={moot ? undefined : 'success'} dimColor={moot}>
          {moot ? (plain ? 'x' : '✗') : plain ? '+' : '✓'}
        </Text>
        <Text bold dimColor>
          {item.label}
        </Text>
        <Box flexShrink={1}>
          <Text dimColor wrap="truncate-end">
            {plainText(ruling || parts.question)}
          </Text>
        </Box>
      </Box>
    )
  }

  const rec = parts.recommendation ? shortRecommendation(parts.recommendation) : null
  const choice = parts.recommendation ? shortRecommendation(plainText(parts.recommendation)).choice : null
  const short = parts.link === null || isShortLink(parts.link)
  const stale = item.created !== undefined && now - item.created > DAY
  const revised = revisedIn(item.description)
  const frame = opts.bordered
    ? { borderStyle: 'round', borderColor: opts.isNext ? ACCENT : 'inactive', hover: { borderColor: ACCENT }, paddingX: 1 }
    : {}
  const more = (press: { surface: Surface }) => void popup($, item, press.surface)
  return (
    <Box key={`card-${item.source}-${item.id}`} flexDirection="column" {...frame}>
      <Box key="top" flexDirection="row" gap={1}>
        {chip(Text, plain, item.kind, KIND_COLOR[item.kind])}
        <Button key={`label-${item.label}`} plain label={item.label} hover={{ bold: true }} onPress={more} />
        {choice && chip(Text, plain, `recommends ${choice}`, 'success')}
        <Box flexGrow={1} />
        {revised && <Text dimColor>{`revised ${revised}`}</Text>}
        {item.created !== undefined && (
          <Text color={stale ? 'error' : undefined} dimColor={!stale}>
            {age(item.created, now).replace('waiting ', '')}
          </Text>
        )}
      </Box>
      <Markdown key={`question-${item.label}`} text={parts.question} />
      {rec?.qualifier && <Markdown key={`qualifier-${item.label}`} text={rec.qualifier} dimColor />}
      {!short && parts.link !== null && linkLine(Link, Text, parts.link)}
      <Box key="actions" flexDirection="row" gap={1}>
        {rec && item.kind === 'DECIDE' && (
          <Button
            key={`accept-${item.label}`}
            label="Accept"
            variant="primary"
            onPress={answer($, [`${item.label}: go with ${parts.recommendation}`])}
          />
        )}
        {item.kind === 'ACTION' && (
          <Button key={`done-${item.label}`} label="Done" variant="primary" onPress={answer($, [`${item.label}: done`])} />
        )}
        <Button key={`answer-${item.label}`} label="Answer" dimColor onPress={answer($, [`${item.label}: `])} />
        <Button key={`more-${item.label}`} label="More" dimColor onPress={more} />
        <Box flexGrow={1} />
        {short && (
          <Text dimColor wrap="truncate-start">
            {parts.link ?? 'no link'}
          </Text>
        )}
      </Box>
    </Box>
  )
}

/**
 * A link too long for the button row, on its own line: a GitHub issue, pull request or repository
 * by its short name, any other URL whole and wrapped, and what a `Link` can't take as dim text.
 */
function linkLine(Link: Elements['Link'], Text: Elements['Text'], token: string) {
  const href = linkHref(token)
  if (!href) {
    return (
      <Text dimColor wrap="wrap">
        {token}
      </Text>
    )
  }
  return (
    <Text wrap="wrap">
      <Link href={href} label={githubShort(href) ?? href} />
    </Text>
  )
}

type Surface = Parameters<$['ui']['copy']>[0]['surface']

/**
 * More, or the card's label: the item in Claude Code's question dialog. Accept and Done fill the
 * prompt as the card's buttons do, the copy options copy, and text typed under Other becomes the
 * item's answer. A dismissed dialog changes nothing.
 */
async function popup($: $, item: Item, surface: Surface): Promise<void> {
  const parts = splitBody(item.body)
  const choices: { label: string; run: () => void }[] = []
  if (item.kind === 'DECIDE' && parts.recommendation) {
    const choice = shortRecommendation(plainText(parts.recommendation)).choice
    choices.push({ label: `Accept: ${choice}`, run: answer($, [`${item.label}: go with ${parts.recommendation}`]) })
  }
  if (item.kind === 'ACTION') choices.push({ label: 'Done', run: answer($, [`${item.label}: done`]) })
  const url = parts.link && /^https?:\/\//i.test(parts.link) ? parts.link : null
  if (url) choices.push({ label: 'Copy link', run: () => void copy($, url, surface) })
  const code = codeSpans(`${parts.question}\n${item.description}`)[0]
  if (code && choices.length < 3) {
    const shown = code.replace(/\s+/g, ' ')
    choices.push({ label: `Copy ${shown.length > 30 ? `${shown.slice(0, 29)}…` : shown}`, run: () => void copy($, code, surface) })
  }
  choices.push({ label: 'Close', run: () => undefined })
  let picked: string
  try {
    const question = dialogQuestion({ ...parts, description: item.description }, choices.length, screen)
    picked = (await $.ui.ask(question, { header: item.label, options: choices.map(one => one.label) })).trim()
  } catch {
    return
  }
  const chosen = choices.find(one => one.label === picked)
  if (chosen) chosen.run()
  else if (picked) answer($, [`${item.label}: ${picked}`])()
}
