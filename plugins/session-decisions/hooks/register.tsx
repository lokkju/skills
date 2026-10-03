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
  nextNumber,
  ordered,
  reminder,
  subjectOf,
  upsert,
} from './queue'
import { LETTER, formatSubject, parseDraft, problems, recommendation, rulingIn } from './subject'

type $ = EngineInterface

const PANE = 'session-decisions'
const ADD_TOOL = 'mcp__session-decisions__decision_add'
const CLOSE_TOOL = 'mcp__session-decisions__decision_close'
const SAFE_ID = /^[A-Za-z0-9._-]{1,128}$/
const KEEP_SESSIONS = 100

const items = atom({ plugin: 'session-decisions', key: 'items' } as const, [] as Item[])
const counters = atom({ plugin: 'session-decisions', key: 'counters' } as const, { D: 0, A: 0 } as Counters)
const created = atom({ plugin: 'session-decisions', key: 'created' } as const, {} as Record<string, number>)
const showSettled = atom({ plugin: 'session-decisions', key: 'showSettled' } as const, false)
const bandHidden = atom({ plugin: 'session-decisions', key: 'bandHidden' } as const, false)

async function sessionId($: $): Promise<string | null> {
  const sid = await $.session.id()
  return SAFE_ID.test(sid) && sid !== '.' && sid !== '..' ? sid : null
}

/** The task list Claude Code keeps for this session (read only; the format is undocumented). */
async function tasksDir($: $, sid: string): Promise<string> {
  const listId = await $.env.get('CLAUDE_CODE_TASK_LIST_ID')
  const config = (await $.env.get('CLAUDE_CONFIG_DIR')) || `${await $.env.get('HOME')}/.claude`
  return `${config}/tasks/${listId && SAFE_ID.test(listId) ? listId : sid}`
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

async function loadRecord($: $, sid: string): Promise<SessionRecord> {
  const raw = (await $.store.get(`session:${sid}`)) as Partial<SessionRecord> | undefined
  return {
    counters: { D: Number(raw?.counters?.D) || 0, A: Number(raw?.counters?.A) || 0 },
    created: raw?.created && typeof raw.created === 'object' ? raw.created : {},
    ledger: Array.isArray(raw?.ledger) ? raw.ledger : [],
    touched: Number(raw?.touched) || 0,
  }
}

async function persist($: $): Promise<void> {
  const sid = await sessionId($)
  if (!sid) return
  const record: SessionRecord = {
    counters: await read($, counters),
    created: await read($, created),
    ledger: (await read($, items)).filter(item => item.source === 'ledger'),
    touched: await $.clock.now(),
  }
  await $.store.set(`session:${sid}`, record)
}

/** Drop the records of all but the most recently used sessions, to stay under the store's cap. */
async function prune($: $): Promise<void> {
  const keys = (await $.store.keys()).filter(key => key.startsWith('session:'))
  if (keys.length <= KEEP_SESSIONS) return
  const touched = await Promise.all(
    keys.map(async key => [key, Number(((await $.store.get(key)) as SessionRecord)?.touched) || 0] as const),
  )
  touched.sort((a, b) => b[1] - a[1])
  await Promise.all(touched.slice(KEEP_SESSIONS).map(([key]) => $.store.delete(key)))
}

/** Rebuild the items from the task files and the stored ledger; the one read this plugin makes. */
async function hydrate($: $): Promise<void> {
  const sid = await sessionId($)
  if (!sid) return
  const record = await loadRecord($, sid)
  const fromFiles = await readTaskFiles($, await tasksDir($, sid))
  const current = await read($, items)
  const tasks = fromFiles ?? current.filter(item => item.source === 'tasks')
  const known = { ...record.created, ...(await read($, created)) }
  const merged = [...tasks, ...record.ledger].map(item => ({
    ...item,
    created: item.created ?? current.find(one => one.id === item.id && one.source === item.source)?.created ?? known[item.label],
  }))
  await update($, items, () => merged)
  await update($, created, () => known)
  await update($, counters, now =>
    coverLabels({ D: Math.max(now.D, record.counters.D), A: Math.max(now.A, record.counters.A) }, merged),
  )
  publish($, merged)
}

function publish($: $, list: readonly Item[]): void {
  const open = list.filter(isOpen).length
  $.ui.status(open ? `${open} open` : undefined)
}

async function change($: $, fn: (list: Item[]) => Item[]): Promise<Item[]> {
  let after: Item[] = []
  await update($, items, list => (after = fn(list)))
  publish($, after)
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
  await update($, bandHidden, () => false)
  $.ui.toast(`${item.label} added: ${item.body}`)
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
      name: 'decision_close',
      description: 'Settles a fallback-ledger item from decision_add with the user\'s ruling. Task-list items are settled with TaskUpdate instead.',
      inputSchema: {
        type: 'object',
        properties: {
          label: { type: 'string', description: 'The item label, such as D3.' },
          ruling: { type: 'string', description: 'The answer, as it should be recorded.' },
        },
        required: ['label', 'ruling'],
      },
    }))
    await hydrate($)
    await quietly(prune($))
    return next(e)
  })

  // After compaction or resume, remind the model of what is still waiting on the user.
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if (e.source !== 'compact' && e.source !== 'resume') return result
    await hydrate($)
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

  on('tool.call', { tool: 'TaskUpdate' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError) return ran
    const before = await read($, items)
    const after = await change($, list =>
      applyUpdate(list, { taskId: e.taskId, subject: e.subject, description: e.description, status: e.status }),
    )
    if (after !== before) await persist($)
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

  on('tool.call', { tool: CLOSE_TOOL }, async ($, e) => {
    const label = String(e.label ?? '').trim().toUpperCase()
    const ruling = String(e.ruling ?? '').trim()
    const list = await read($, items)
    if (!list.some(item => item.source === 'ledger' && item.label === label)) {
      return {
        deny: `session-decisions: ${label} isn't in the fallback ledger. If it's a task-list item, mark it completed with TaskUpdate and add the ruling to its description.`,
      }
    }
    const date = new Date(await $.clock.now()).toISOString().slice(0, 10)
    await change($, now =>
      now.map(item =>
        item.source === 'ledger' && item.label === label
          ? {
              ...item,
              status: 'completed',
              description: [item.description, `Ruling (${date}): ${ruling}`].filter(Boolean).join('\n'),
            }
          : item,
      ),
    )
    await persist($)
    return { result: `${label} closed` }
  })

  on('command.run', { command: 'decisions' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const list = await read($, items)
    const now = await $.clock.now()
    if (arg === 'pane') {
      const opened = await $.ui.open({ id: PANE, title: 'Decisions' })
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

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, items)
    const open = ordered(list).filter(isOpen)
    const hidden = await read($, bandHidden)
    if (e.props.hasSurvey || open.length === 0 || hidden) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const first = open[0]!
    const band = (
      <Box key="decisions-band" flexDirection="row" width={e.props.bodyColumns} gap={1}>
        <Text bold color="yellow">
          {open.length} waiting on you
        </Text>
        <Text dimColor wrap="truncate-end">
          {subjectOf(first)}
        </Text>
        <Button key="show" label="Show" onPress={() => void $.ui.open({ id: PANE, title: 'Decisions' }).catch(() => undefined)} />
        <Button key="hide" label="Hide" role="dismiss" onPress={() => update($, bandHidden, () => true)} />
      </Box>
    )
    const below = await next(e)
    return below ? (
      <Box flexDirection="column">
        {below}
        {band}
      </Box>
    ) : (
      band
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = ordered(await read($, items))
    const settled = await read($, showSettled)
    const now = await $.clock.now()
    const open = list.filter(isOpen)
    const shown = settled ? list : open

    // Each press adds or replaces its item's line in the draft, so one message can answer several.
    const answer = (lines: string[]) => () =>
      void $.prompt
        .read()
        .then(box => $.prompt.fill({ text: mergeAnswers(box.text, lines) }))
        .catch(() => undefined)
    const accepts = open.flatMap(item => {
      const choice = item.kind === 'DECIDE' ? recommendation(item.body) : null
      return choice ? [`${item.label}: go with ${choice}`] : []
    })

    return (
      <Box flexDirection="column" gap={1}>
        <Box key="head" flexDirection="row" gap={1}>
          <Text bold>
            {open.length ? `${open.length} waiting on you` : 'Nothing waiting on you'}
          </Text>
          {accepts.length > 1 && (
            <Button key="accept-all" label="Accept all" variant="primary" onPress={answer(accepts)} />
          )}
          <Button
            key="settled"
            label={settled ? 'Hide settled' : 'Show settled'}
            onPress={() => update($, showSettled, value => !value)}
          />
        </Box>
        {shown.length === 0 && (
          <Text dimColor>
            No decisions or actions this session.
          </Text>
        )}
        {shown.map(item => {
          const choice = item.kind === 'DECIDE' ? recommendation(item.body) : null
          const ruling = rulingIn(item.description)
          const live = isOpen(item)
          return (
            <Box key={`item-${item.source}-${item.id}`} flexDirection="column">
              <Text bold={live} dimColor={!live}>
                {item.label} {item.kind}
                {item.source === 'ledger' ? ' (ledger)' : ''}
                {live && item.created ? `  ${age(item.created, now)}` : ''}
              </Text>
              <Text dimColor={!live}>
                {item.body}
              </Text>
              {ruling && (
                <Text dimColor italic>
                  {ruling}
                </Text>
              )}
              {live && (
                <Box key="actions" flexDirection="row" gap={1}>
                  {choice && (
                    <Button
                      key={`accept-${item.label}`}
                      label="Accept"
                      variant="primary"
                      onPress={answer([`${item.label}: go with ${choice}`])}
                    />
                  )}
                  {item.kind === 'ACTION' && (
                    <Button key={`done-${item.label}`} label="Done" variant="primary" onPress={answer([`${item.label}: done`])} />
                  )}
                  <Button key={`answer-${item.label}`} label="Answer" onPress={answer([`${item.label}: `])} />
                </Box>
              )}
            </Box>
          )
        })}
      </Box>
    )
  })
}
