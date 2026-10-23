import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Counters, Details, Item, Kind, Loop, SessionRecord } from '../types'
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
import { type Message, type Trail, ledgerMatches, trailOf } from './lineage'
import { githubShort, isShortLink, linkHref, plainText } from './markdown'
import { MORE, detailsExtras, hasMore, moreSplit } from './more'
import { type Forge, type ForgeHosts, linkRefs, parseForgeHosts, parseOverride, refHref, resolveForge } from './forge'
import { handoffs, nudge, unknownLabels } from './handoff'
import { RECONCILE, brief, inventory } from './reconcile'
import { LETTER, formatSubject, parseDraft, problems, recommendation, revisedIn, rulingIn, shortRecommendation, splitBody } from './subject'

type $ = EngineInterface

const PANE = 'session-decisions'
const ADD_TOOL = 'mcp__session-decisions__decision_add'
const CLOSE_TOOL = 'mcp__session-decisions__decision_close'
const UPDATE_TOOL = 'mcp__session-decisions__decision_update'
const LIST_TOOL = 'mcp__session-decisions__decision_list'
// The tools whose transcript row draws as one line naming its item, and whose result draws nothing.
const CARD_TOOLS = ['TaskCreate', ADD_TOOL, UPDATE_TOOL] as const
const SAFE_ID = /^[A-Za-z0-9._-]{1,128}$/
const KEEP_RECORDS = 100
const RECORD = /^(session|list):/

const items = atom({ plugin: 'session-decisions', key: 'items' } as const, [] as Item[])
const counters = atom({ plugin: 'session-decisions', key: 'counters' } as const, { D: 0, A: 0 } as Counters)
const created = atom({ plugin: 'session-decisions', key: 'created' } as const, {} as Record<string, number>)
// Task-list items decision_close settled while TaskUpdate wasn't available, by task id.
const closed = atom({ plugin: 'session-decisions', key: 'closed' } as const, {} as Record<string, Item>)
// Subagent loops by agent id: how each was spawned, or that it has called a task tool.
const loops = atom({ plugin: 'session-decisions', key: 'loops' } as const, {} as Record<string, Loop>)
const TASK_TOOLS = ['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate'] as const
// When the person's latest prompt was sent, so the hand-off check counts the items this turn made.
const turnStarted = atom({ plugin: 'session-decisions', key: 'turnStarted' } as const, 0)
// The hand-off reminder the model gets with the person's next prompt; empty when there's none.
const handoffNudge = atom({ plugin: 'session-decisions', key: 'handoffNudge' } as const, '')
const showSettled = atom({ plugin: 'session-decisions', key: 'showSettled' } as const, false)
const paneDismissed = atom({ plugin: 'session-decisions', key: 'paneDismissed' } as const, false)
const autoOpened = atom({ plugin: 'session-decisions', key: 'autoOpened' } as const, false)
const details = atom({ plugin: 'session-decisions', key: 'details' } as const, null as Details | null)
// Whether this surface docks panes beside the transcript: fixed per session on the terminal and
// learnt from the footer's drawing. A module variable, since a drawing may not write state.
let docks = false
// Whether the queue pane is on screen, as this module last saw it open, draw or close.
let paneUp = false
// The screen's height as the last drawing saw it, so the details view can ask for a pane that
// tall; null until something has drawn on a surface that measures.
let screenRows: number | null = null
function measure(site: { viewport?: { rows: number; columns: number } }) {
  if (site.viewport && site.viewport.rows > 0) screenRows = site.viewport.rows
}
// The first row the queue list showed when it was last drawn or scrolled, so Back from the details
// view puts the list where it was; null until the list has been seen.
let listOffset: number | null = null
// While Back restores the list: the row its window started at, where the list's drawing puts a
// marker for the scroll to bring to the top. A plugin's $.ui.scroll names things, never rows.
let restoreRow: number | null = null
const ascii = atom({ plugin: 'session-decisions', key: 'ascii' } as const, false)
// Whether the last hydrate found no stored record and no transcript to look for one in, so the
// next prompt should look again. A module variable: it's about this process, not the queue.
let lineageOpen = false
// The forge the session's repository is on, read from its git config and remote at session start, so `#12`
// and `!4` can link to it; null outside a repository or on a host whose kind isn't known.
let forge: Forge | null = null

/**
 * The repository's forge: its `tracker.forge` and `tracker.web` git config when set, then the
 * `origin` remote, or the only remote when there's no `origin`; `hosts` names the kind of a host
 * the built-in rules don't know, or of one they'd get wrong. Notes name override values it can't use.
 */
async function repoForge($: $, hosts: ForgeHosts): Promise<{ forge: Forge | null; notes: string[] }> {
  const git = async (...args: string[]) => {
    const ran = await $.process.run(['git', ...args], { timeoutMs: 5000 })
    return ran.exitCode === 0 ? ran.stdout.trim() : ''
  }
  try {
    const { override, notes } = parseOverride(await git('config', '--get-regexp', '^tracker\\.(forge|web)$'))
    let url = ''
    if (!override.web) {
      url = await git('remote', 'get-url', 'origin')
      if (!url) {
        const names = (await git('remote')).split('\n').filter(Boolean)
        if (names.length === 1) url = await git('remote', 'get-url', names[0] ?? '')
      }
    }
    return { forge: resolveForge(url, hosts, override), notes }
  } catch {
    return { forge: null, notes: [] }
  }
}

const DAY = 24 * 60 * 60 * 1000
// Claude Code's own theme keys, so the cards follow whichever theme the person runs.
const ACCENT = 'claude'
const KIND_COLOR = { DECIDE: 'warning', ACTION: 'suggestion' } as const
// Columns a bordered card's frame takes: a border and a column of padding on each side.
const CARD_FRAME = 4

/**
 * The item a finished TaskCreate, decision_add or decision_update call made or changed, found by
 * the task id or label in its output; undefined for any other call or one that didn't finish.
 */
function rowItem(
  list: readonly Item[],
  call: { tool: string; isRunning?: boolean; isErrored: boolean; isInterrupted?: boolean; output?: unknown },
): Item | undefined {
  if (call.isRunning || call.isErrored || call.isInterrupted) return undefined
  if (call.tool === 'TaskCreate') {
    const id = String((call.output as { task?: { id?: unknown } } | undefined)?.task?.id ?? '')
    return id ? list.find(one => one.source === 'tasks' && one.id === id) : undefined
  }
  if (call.tool !== ADD_TOOL && call.tool !== UPDATE_TOOL) return undefined
  const label = String(call.output ?? '').split(' ')[0]
  return label ? list.find(one => one.source === 'ledger' && one.label === label) : undefined
}

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

/** Whether the main loop has TaskUpdate, loaded or deferred; false when the host can't say. */
async function taskUpdateOffered($: $): Promise<boolean> {
  try {
    return (await $.tool.list()).some(tool => tool.name === 'TaskUpdate')
  } catch {
    return false
  }
}

/**
 * Whether the loop a tool call came from has TaskUpdate. `$.tool.list()` answers for the main loop
 * even inside a subagent (Claude Code 2.1.300, lokkju/skills#37), and a background subagent gets
 * no task tools, so a subagent's loop is judged by what the plugin saw of it: one that has called
 * a task tool has them, a foreground one has what the main loop has, and a background one, or one
 * whose spawn the plugin didn't see, is taken not to.
 */
async function taskUpdateFor($: $, agentId: string | undefined): Promise<boolean> {
  if (agentId === undefined) return taskUpdateOffered($)
  const loop = (await read($, loops))[agentId]
  if (loop === 'tasks') return true
  if (loop === 'foreground') return taskUpdateOffered($)
  return false
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

/** The main conversation's transcript as `$.session.messages()` has it; empty when it can't be read. */
async function conversation($: $): Promise<readonly Message[]> {
  try {
    const rows = await $.session.messages()
    return Array.isArray(rows) ? (rows as readonly Message[]) : []
  } catch {
    return []
  }
}

/**
 * This session's stored record, and whether there was one. The first read under a shared task
 * list takes over the record this session kept under its own id. A session with no record that
 * carries decision items, in its task files or in its transcript, came from another session under
 * a new id, so it starts from that session's record (see `carriedFrom`).
 */
async function loadRecord(
  $: $,
  sid: string,
  fromFiles: readonly Item[] | null,
  trail: Trail,
): Promise<{ record: SessionRecord; isStored: boolean }> {
  const key = await recordKey($, sid)
  const stored = await $.store.get(key)
  if (stored !== undefined) return { record: asRecord(stored), isStored: true }
  const own = key === `session:${sid}` ? undefined : await $.store.get(`session:${sid}`)
  const found = own !== undefined ? asRecord(own) : key.startsWith('session:') ? await carriedFrom($, sid, fromFiles ?? [], trail) : null
  if (!found) return { record: asRecord(undefined), isStored: false }
  await $.store.set(key, { ...found, touched: await $.clock.now() })
  if (own !== undefined) await $.store.delete(`session:${sid}`)
  return { record: found, isStored: true }
}

/**
 * The record of the session this one carries on from under a new id: the most recently used one
 * that agrees with every sign of it this session has, and there has to be at least one.
 * - Task files. Backgrounding a session copies its task files under the new id, so the record's
 *   own task list holds every decision item in this one's by task id and label, and at least one
 *   of them word for word. A label survives a revised subject, so items either session changed
 *   after the fork still match; the word-for-word one keeps two short lists that both start at
 *   task 1, D1 apart.
 * - The transcript. A resume, restart or fork that changes the id keeps the conversation, and with
 *   it what decision_add, decision_update and decision_close answered, so the record's ledger
 *   holds those items (`ledgerMatches`). That is the only sign a session whose items are all in
 *   the ledger has; an unrelated session in the same project never has the same answers.
 */
async function carriedFrom($: $, sid: string, carried: readonly Item[], trail: Trail): Promise<SessionRecord | null> {
  if (carried.length === 0 && trail.ledger.size === 0) return null
  const root = await tasksRoot($)
  const covers = (record: SessionRecord) =>
    carried.every(item => record.counters[LETTER[item.kind]] >= Number(item.label.slice(1)))
  let best: SessionRecord | null = null
  for (const key of await $.store.keys()) {
    const other = key.startsWith('session:') ? safeId(key.slice('session:'.length)) : null
    if (!other || other === sid) continue
    const record = asRecord(await $.store.get(key))
    if (!covers(record) || (best && record.touched <= best.touched)) continue
    if (ledgerMatches(record, trail) === false) continue
    if (carried.length > 0) {
      const theirs = await readTaskFiles($, `${root}/${other}`)
      const same = (item: Item, exact: boolean) =>
        theirs?.some(one => one.id === item.id && one.label === item.label && (!exact || one.body === item.body))
      if (!carried.every(item => same(item, false)) || !carried.some(item => same(item, true))) continue
    }
    best = record
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
  const messages = await conversation($)
  const trail = trailOf(messages)
  const { record, isStored } = await loadRecord($, sid, fromFiles, trail)
  // A session with no record yet may be one whose transcript the engine hadn't loaded when this
  // ran; the next prompt looks again, until the record exists or the transcript has been read.
  lineageOpen = !isStored && messages.length === 0
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
  // Labels the conversation has already shown count too, so a session whose record is gone
  // still never hands out one of them again.
  const shown = trail.labels.map(label => ({ label }))
  await update($, counters, now =>
    coverLabels(coverLabels({ D: Math.max(now.D, record.counters.D), A: Math.max(now.A, record.counters.A) }, merged), shown),
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

export const register: Register = (on, options) => {
  // The forge_hosts option (plugin.json userConfig), read as the module loads.
  const forgeHosts = parseForgeHosts(options?.forge_hosts)
  const checkHandoffs = options?.handoff_check !== false
  on('session.start', async ($, e, next) => {
    // Each registration stands alone: a host that refuses one still gets the rest and the queue.
    const quietly = (work: Promise<unknown>) => work.catch(() => undefined)
    await quietly($.command.register({
      name: 'decisions',
      description: "Show this session's open decisions (D) and actions (A); 'all' adds settled ones, 'pane' opens the panel, 'reconcile' closes or revises the ones the session has settled, 'wrap' settles each before the session ends.",
      argumentHint: '[all | pane | reconcile | wrap]',
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
    await quietly($.tool.register({
      name: 'decision_list',
      description:
        "Lists this session's open decision-queue items, task-list and fallback-ledger alike (label, kind, age, link, text), and the lines of the user's messages that name them, for reconciling the queue.",
      inputSchema: { type: 'object', properties: {} },
    }))
    if (forgeHosts.notes.length) $.ui.toast(`session-decisions: forge_hosts ignores ${forgeHosts.notes.join('; ')}`)
    const found = await repoForge($, forgeHosts.hosts)
    if (found.notes.length) $.ui.toast(`session-decisions: git config ignores ${found.notes.join('; ')}`)
    forge = found.forge
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

  // The hand-off check: a reply that hands the person work without queuing it gets a toast now,
  // and the model a reminder with the person's next prompt. It never blocks the reply.
  on('classic.UserPromptSubmit', async ($, e, next) => {
    const result = await next(e)
    const now = await $.clock.now()
    await update($, turnStarted, () => now)
    if (lineageOpen) await hydrate($, e.session_id)
    const pending = await read($, handoffNudge)
    if (!pending) return result
    await update($, handoffNudge, () => '')
    return { ...result, additionalContext: [...(result.additionalContext ?? []), pending] }
  })

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (!checkHandoffs || e.stop_hook_active || result.block !== undefined) return result
    const reply = String(e.last_assistant_message ?? '')
    if (!reply) return result
    const since = await read($, turnStarted)
    const all = await read($, items)
    const queue = all.filter(item => isOpen(item) || (item.created ?? 0) >= since)
    const known = all.map(item => item.label)
    const found = handoffs(reply, queue, { known })
    if (found.length === 0) return result
    const missing = unknownLabels(reply, known)
    await update($, handoffNudge, () => nudge(found, missing))
    // Short, since a toast is narrow: the first line names the count and starts the first hand-off.
    const count = found.length === 1 ? 'A hand-off' : `${found.length} hand-offs`
    $.ui.toast(missing.length > 0 ? `${missing.join(', ')} named but not in the queue` : `${count} not queued: ${plainText(found[0] ?? '')}`)
    return result
  })

  // What decision_close and decision_update need to know about a subagent: whether it was spawned
  // in the background, which gives it no task tools. A teammate or a workflow's agent isn't
  // recorded, and counts as having none until it calls one.
  on('agent.spawn', async ($, e, next) => {
    const ran = await next(e)
    const id = ran.agentId
    if (id && !e.isTeammate && !e.workflow) {
      const loop: Loop = e.background ? 'background' : 'foreground'
      await update($, loops, (map): Record<string, Loop> => (map[id] === 'tasks' ? map : { ...map, [id]: loop }))
    }
    return ran
  })

  // A subagent that calls a task tool has them, however it was spawned.
  on('tool.call', { tool: TASK_TOOLS }, async ($, e, next) => {
    const ran = await next(e)
    const id = e.agentId
    if (id !== undefined && ran.deny === undefined && !ran.isError) {
      await update($, loops, (map): Record<string, Loop> => (map[id] === 'tasks' ? map : { ...map, [id]: 'tasks' }))
    }
    return ran
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
        deny: (await taskUpdateFor($, e.agentId))
          ? `session-decisions: ${label} is a task-list item; revise it with TaskUpdate, keeping its label, and add a Revised line to its description.${LOAD}`
          : `session-decisions: ${label} is a task-list item and TaskUpdate isn't available here; close it with decision_close as superseded and add the revised item with decision_add.`,
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
    if (target.source === 'tasks' && (await taskUpdateFor($, e.agentId))) {
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
    if (e.agentId !== undefined) {
      return {
        result: `${label} closed in the decision queue. This agent has no TaskUpdate, so the task still shows it open until the main conversation marks it completed; the queue keeps the ruling.`,
      }
    }
    return {
      result: `${label} closed in the decision queue. TaskUpdate isn't available here, so its task still shows it open; the queue keeps the ruling.`,
    }
  })

  on('tool.call', { tool: LIST_TOOL }, async $ => {
    return { result: brief(await read($, items), await conversation($), await $.clock.now()) }
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
    if (arg === 'reconcile') {
      if (!list.some(isOpen)) return { text: 'No open decisions or actions; nothing to reconcile.' }
      const text = brief(list, await conversation($), now)
      submitSoon($, `${RECONCILE}\n\n${text}`)
      return { text: inventory(list, now) }
    }
    if (arg === 'wrap') {
      const open = list.filter(isOpen)
      if (open.length === 0) return { text: 'No open decisions or actions; nothing to wrap up.' }
      submitSoon($, `${WRAP}\n\n${listing(list, now)}`)
      return { text: listing(list, now) }
    }
    return { text: listing(list, now, arg === 'all') }
  })

  // A sidebar the person closes stays closed until the queue empties; closing a pane that opened
  // only to show an item's details doesn't count as closing the sidebar.
  on('ui.close', async ($, e, next) => {
    if (e.id !== PANE) return next(e)
    const shown = await read($, details)
    if (shown) await update($, details, () => null)
    if (e.origin.kind === 'person' && !shown?.owns) {
      await update($, paneDismissed, () => true)
      await update($, autoOpened, () => false)
    }
    const closed = await next(e)
    paneUp = false
    return closed
  })

  // A pane waiting undrawn (opened unasked on a narrow terminal) isn't one the person has seen.
  on('ui.open', async ($, e, next) => {
    const opened = await next(e)
    if (e.id === PANE) paneUp = opened.value?.isPlaced === true
    return opened
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

  // The queue has its own places (the sidebar, the footer, /decisions), so the transcript keeps one
  // line where an item was queued or revised: `queued D3`, which opens it, and `✓ D3 · <ruling>`
  // once settled. The tool's result row, the labelled subject the model reads, draws nothing, so
  // the item isn't printed twice; ctrl+o still shows the call and its result in full.
  for (const tool of CARD_TOOLS) {
    on('ui.render', { component: 'ToolUse', props: { tool } }, async ($, e, next) => {
      measure(e)
      if (e.props.isExpanded) return next(e)
      const item = rowItem(await read($, items), e.props)
      if (!item) return next(e)
      return drawMention($, e, item, tool === UPDATE_TOOL ? 'revised' : 'queued')
    })
    on('ui.render', { component: 'ToolResult', props: { tool } }, async ($, e, next) => {
      if (e.props.isExpanded || !rowItem(await read($, items), e.props)) return next(e)
      const { Box } = $.ui.resolve(e)
      return <Box key="decisions-result" />
    })
  }

  // Claude Code 2.1.286 folds a TaskCreate call into a tool group with the calls around it (a
  // ToolSearch that loaded it, say), and a folded group of those draws as nothing at all, so its
  // line would show only in ctrl+o. A folded group draws as usual with its items' lines under it;
  // an unfolded one is ToolUse rows, which draw them already.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    if (e.props.isExpanded) return next(e)
    const list = await read($, items)
    const found: { item: Item; verb: 'queued' | 'revised' }[] = []
    for (const call of e.props.calls) {
      const item = rowItem(list, call)
      if (item && !found.some(one => one.item === item)) found.push({ item, verb: call.tool === UPDATE_TOOL ? 'revised' : 'queued' })
    }
    if (found.length === 0) return next(e)
    measure(e)
    const { Box } = $.ui.resolve(e)
    const below = await next(e)
    return (
      <Box flexDirection="column">
        {below}
        {await Promise.all(
          found.map(async ({ item, verb }) => (
            <Box key={`group-${item.source}-${item.id}`} flexDirection="column">
              {await drawMention($, e, item, verb)}
            </Box>
          )),
        )}
      </Box>
    )
  })

  // The list's place as the person scrolls it, for Back from the details view to return to.
  on('ui.scroll', { requestId: PANE }, async ($, e, next) => {
    const moved = await next(e)
    if (moved.deny === undefined && !(await read($, details))) listOffset = e.offset
    return moved
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    measure(e)
    paneUp = true
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = ordered(await read($, items))
    const settledOpen = await read($, showSettled)
    const plain = await read($, ascii)
    const docked = e.props.placement === 'dock'
    const shown = await read($, details)
    const focus = shown ? list.find(item => keyOf(item) === shown.item && isOpen(item)) : undefined
    if (focus) return drawDetails($, e, focus, shown?.owns === true)
    if (e.props.scroll && restoreRow === null) listOffset = e.props.scroll.offset
    const open = list.filter(isOpen)
    const settled = list.filter(item => !isOpen(item))
    const accepts = open.flatMap(item => {
      const choice = item.kind === 'DECIDE' ? recommendation(item.body) : null
      return choice ? [`${item.label}: go with ${choice}`] : []
    })

    return (
      <Box flexDirection="column">
        {restoreRow !== null && <Box key="list-place" position="absolute" top={restoreRow} left={0} width={1} height={1} />}
        <Box key="head" flexDirection="row" gap={1} marginBottom={1}>
          <Text bold>Decisions</Text>
          {open.length > 0 ? chip(Text, plain, `${open.length} open`, ACCENT) : <Text dimColor>Nothing waiting on you</Text>}
          <Box flexGrow={1} />
          {accepts.length > 1 && <Button key="accept-all" label="Accept all" variant="primary" onPress={answer($, accepts)} />}
        </Box>
        {await Promise.all(
          open.map(async (item, i) => (
            <Box key={`slot-${item.source}-${item.id}`} flexDirection="column" marginBottom={docked ? 1 : 0}>
              {await drawCard($, e, item, { bordered: !docked, isNext: i === 0, compact: false, width: e.props.bodyColumns - (docked ? 0 : CARD_FRAME) })}
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
                {await drawCard($, e, item, { bordered: false, isNext: false, compact: true, width: null })}
              </Box>
            )),
          ))}
      </Box>
    )
  })
}

/**
 * Send a prompt once the command that asked for it has answered. The host refuses a submit from
 * inside a `command.run` hook, since the prompt would wait on the turn that hook holds; a timer
 * runs it after the hook returns, and the host starts the turn once the session is idle.
 */
function submitSoon($: $, text: string): void {
  $.clock.after(0, () => {
    void $.prompt.submit({ text }).catch((err: unknown) => $.ui.toast(`session-decisions: couldn't send the prompt: ${String(err)}`))
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
 * buttons; `bordered` draws a round frame (the accent one for `isNext`). When the details view has
 * more than the card shows, the text ends with `[more]`, beside its last row when it fits in `width`
 * (the columns inside the frame) and on a row of its own otherwise. Settled, or `compact`: one dim
 * line with the ruling.
 */
async function drawCard(
  $: $,
  site: Parameters<$['ui']['resolve']>[0],
  item: Item,
  opts: { bordered: boolean; isNext: boolean; compact: boolean; width: number | null },
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
  const more = () => void showDetails($, item)
  const question = linkRefs(parts.question, forge)
  const qualifier = rec?.qualifier ? linkRefs(rec.qualifier, forge) : null
  // [more] follows whichever line of text comes last: the qualifier when there is one. It is drawn
  // in the accent color and underlined, so it reads as a control beside the question (normal text)
  // and beside the qualifier (dim text) alike.
  const ending = hasMore({ parts, description: item.description }, forge)
    ? {
        split: moreSplit(qualifier ?? question, opts.width),
        button: (
          <Button key={`more-${item.label}`} plain label={MORE} hover={{ bold: true }} onPress={more}>
            <Text color={ACCENT} underline>
              {MORE}
            </Text>
          </Button>
        ),
      }
    : null
  const line = (key: string, text: string, dim: boolean) => {
    const drawn = (k: string, t: string) => <Markdown key={k} text={t} dimColor={dim || undefined} />
    if (!ending) return drawn(key, text)
    if (!ending.split) {
      return (
        <Box key={`${key}-end`} flexDirection="column">
          {drawn(key, text)}
          {ending.button}
        </Box>
      )
    }
    const { head, tail } = ending.split
    // The tail shrinks and wraps when the width it was measured at is more than the card has.
    return (
      <Box key={`${key}-end`} flexDirection="column">
        {head && drawn(`${key}-head`, head)}
        <Box key={`${key}-tail`} flexDirection="row" gap={1}>
          <Box key={`${key}-tail-text`} flexShrink={1}>
            {drawn(head ? `${key}-tail` : key, tail)}
          </Box>
          {ending.button}
        </Box>
      </Box>
    )
  }
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
      {qualifier ? <Markdown key={`question-${item.label}`} text={question} /> : line(`question-${item.label}`, question, false)}
      {qualifier && line(`qualifier-${item.label}`, qualifier, true)}
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
        <Box flexGrow={1} />
        {short && shortLink(Link, Text, parts.link)}
      </Box>
    </Box>
  )
}

/**
 * An item's one line in the transcript, where it was queued or revised: `queued D3` (or `revised
 * D3`) beside a glyph in its kind's color, the label opening the item in the details view; once
 * settled, the dim `✓ D3 · <ruling>` line the queue draws for it.
 */
async function drawMention($: $, site: Parameters<$['ui']['resolve']>[0], item: Item, verb: 'queued' | 'revised') {
  if (!isOpen(item)) return drawCard($, site, item, { bordered: false, isNext: false, compact: true, width: null })
  const { Box, Button, Text } = $.ui.resolve(site) as Elements
  const plain = await read($, ascii)
  return (
    <Box key={`mention-${item.source}-${item.id}`} flexDirection="row" gap={1}>
      <Text color={KIND_COLOR[item.kind]}>{plain ? '*' : '●'}</Text>
      <Button
        key={`mention-${item.label}`}
        plain
        dimColor
        label={`${verb} ${item.label}`}
        hover={{ bold: true }}
        onPress={() => void showDetails($, item)}
      />
    </Box>
  )
}

/**
 * A short link at the end of the button row: `#12`, `!4` or `owner/repo#12` as a link to it on the
 * repository's forge when that's known, else as dim text, as is `no link`.
 */
function shortLink(Link: Elements['Link'], Text: Elements['Text'], token: string | null) {
  const href = refHref(token, forge)
  return (
    <Text key="link" dimColor wrap="truncate-start">
      {href && token ? <Link href={href} label={token} /> : (token ?? 'no link')}
    </Text>
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

/** How the details view names an item: its source and id, which a label alone doesn't settle. */
function keyOf(item: Item): string {
  return `${item.source}:${item.id}`
}

/**
 * `[more]`, or the card's label: the item in full in the queue pane, in place of the list. The pane
 * opens for it, with the keys, when it isn't open already, as tall as the screen allows; Claude
 * Code scrolls a pane whose content is taller than it, with the arrows, the page keys, Home, End
 * and the wheel. A pane that can't be placed leaves things as they were and says why in a toast.
 */
async function showDetails($: $, item: Item): Promise<void> {
  // Opened before anything else is awaited, so Claude Code counts it as the person's press and
  // seats it at any width; whether the queue was open already is the module's own record.
  const owns = !paneUp
  const opening = $.ui
    .open({ id: PANE, title: 'Decisions', focus: true, closeOnEscape: true, columns: 52, rows: screenRows ?? 40 })
    .catch((err: unknown) => ({ isPlaced: false as const, reason: String(err) }))
  await update($, details, () => ({ item: keyOf(item), owns }))
  const opened = await opening
  if (!opened.isPlaced) {
    await update($, details, () => null)
    $.ui.toast(`Couldn't show ${item.label}: ${opened.reason}`)
    return
  }
  await $.ui.scroll({ in: PANE, to: 'start' }).catch(() => undefined)
}

/**
 * Leave the details view: closed when the pane opened only for it, otherwise back to the list where
 * it was, the window at the row it started at and the focus on the item's label. The list draws a
 * marker at that row while this runs; focusing waits for the drawing that brings the label, so the
 * scroll after it finds the marker in the list, not in the details.
 */
async function leaveDetails($: $, item: Item, owns: boolean): Promise<void> {
  const back = owns ? null : listOffset
  restoreRow = back
  await update($, details, () => null)
  if (owns) {
    restoreRow = null
    await $.ui.close({ id: PANE }).catch(() => undefined)
    return
  }
  try {
    await $.ui.focus({ requestId: PANE, key: `label-${item.label}` }).catch(() => undefined)
    if (back !== null) await $.ui.scroll({ in: PANE, to: { key: 'list-place' }, block: 'start' }).catch(() => undefined)
  } finally {
    restoreRow = null
  }
  // An offset the engine moved since it was read (the focus ring walking the list) may not show
  // the item; this moves only when it doesn't.
  await $.ui.scroll({ in: PANE, to: { key: `slot-${item.source}-${item.id}` }, block: 'nearest' }).catch(() => undefined)
}

/**
 * The details view: the card's top row, its actions (Accept or Done, Answer, Copy link, Copy
 * <command>, and Back, or Close when the pane opened for this item), then the question, the link,
 * the whole recommendation and the description, drawn as markdown, and a last line that says the
 * item ends there. The actions come first so they're in reach however far down the text goes;
 * Accept, Done and Answer fill the prompt as the card's buttons do and leave the view.
 */
async function drawDetails($: $, site: Parameters<$['ui']['resolve']>[0], item: Item, owns: boolean) {
  const { Box, Button, Link, Markdown, Text } = $.ui.resolve(site) as Elements
  const plain = await read($, ascii)
  const now = await $.clock.now()
  const parts = splitBody(item.body)
  const choice = parts.recommendation ? shortRecommendation(plainText(parts.recommendation)).choice : null
  const { url, code } = detailsExtras(parts, item.description, forge)
  const href = linkHref(url)
  const stale = item.created !== undefined && now - item.created > DAY
  const leave = () => leaveDetails($, item, owns)
  const act = (lines: string[]) => () => {
    answer($, lines)()
    void leave()
  }
  const shownCode = code ? code.replace(/\s+/g, ' ') : ''
  const hint = plain ? 'Up/Down PgUp/PgDn scroll, Esc closes' : '↑↓ PgUp PgDn scroll · Esc closes'
  return (
    <Box key={`details-${item.source}-${item.id}`} flexDirection="column">
      <Box key="details-top" flexDirection="row" gap={1}>
        {chip(Text, plain, item.kind, KIND_COLOR[item.kind])}
        <Text bold>{item.label}</Text>
        {choice && chip(Text, plain, `recommends ${choice}`, 'success')}
        <Box flexGrow={1} flexShrink={1} justifyContent="flex-end">
          <Text key="details-hint" dimColor wrap="truncate-end">
            {hint}
          </Text>
        </Box>
        {item.created !== undefined && (
          <Text color={stale ? 'error' : undefined} dimColor={!stale}>
            {age(item.created, now).replace('waiting ', '')}
          </Text>
        )}
      </Box>
      <Box key="details-actions" flexDirection="row" flexWrap="wrap" columnGap={1}>
        {item.kind === 'DECIDE' && parts.recommendation && (
          <Button key="details-accept" label="Accept" variant="primary" onPress={act([`${item.label}: go with ${parts.recommendation}`])} />
        )}
        {item.kind === 'ACTION' && <Button key="details-done" label="Done" variant="primary" onPress={act([`${item.label}: done`])} />}
        <Button key="details-answer" label="Answer" dimColor onPress={act([`${item.label}: `])} />
        {url && <Button key="details-copy-link" label="Copy link" dimColor onPress={press => void copy($, url, press.surface)} />}
        {code && (
          <Button
            key="details-copy-code"
            label={`Copy ${shownCode.length > 30 ? `${shownCode.slice(0, 29)}…` : shownCode}`}
            dimColor
            onPress={press => void copy($, code, press.surface)}
          />
        )}
        <Button key="details-back" label={owns ? 'Close' : 'Back'} dimColor onPress={() => void leave()} />
      </Box>
      <Box key="details-body" flexDirection="column" marginTop={1}>
        <Markdown key="details-question" text={linkRefs(parts.question, forge)} />
        {url && (
          <Text key="details-link" wrap="wrap">
            {'Link: '}
            {href ? <Link href={href} label={href} /> : url}
          </Text>
        )}
        {parts.recommendation && (
          <Box key="details-recommendation" marginTop={1}>
            <Markdown key="details-recommendation-text" text={`Recommended: ${linkRefs(parts.recommendation, forge)}`} />
          </Box>
        )}
        {item.description.trim() && (
          <Box key="details-description" marginTop={1}>
            <Markdown key="details-description-text" text={linkRefs(item.description, forge)} />
          </Box>
        )}
        <Box key="details-end" marginTop={1}>
          <Text dimColor>{plain ? `-- end of ${item.label} --` : `─ end of ${item.label} ─`}</Text>
        </Box>
      </Box>
    </Box>
  )
}
