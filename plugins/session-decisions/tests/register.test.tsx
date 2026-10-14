import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PLUGIN = 'session-decisions'
const SURFACES = ['terminal', 'desktop', 'vscode', 'mobile'] as const
const SID = 'test-session'
const ROOT = '/home/me/.claude/tasks'
const RUN = { origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } } as const

type Task = { subject: string; description: string; status: string }

/** What an earlier process left behind: the plugin's store, and task lists by name. */
type Seed = { store?: Record<string, unknown>; lists?: Record<string, Record<string, Task>> }

/**
 * The engine's own behaviour beneath the plugin: a clock, a store, task lists in memory that the
 * plugin's reads see as files, a prompt box, and the UI it reports to. The session's own list is
 * `tasks/<CLAUDE_CODE_TASK_LIST_ID>` when that is set and `tasks/<session id>` otherwise.
 */
function engine(on: On, env: Record<string, string> = {}, seed: Seed = {}) {
  const lists = new Map<string, Map<string, Task>>()
  for (const [name, list] of Object.entries(seed.lists ?? {})) lists.set(`${ROOT}/${name}`, new Map(Object.entries(list)))
  const listOf = (name: string) => {
    const dir = `${ROOT}/${name}`
    if (!lists.has(dir)) lists.set(dir, new Map())
    return lists.get(dir)!
  }
  const session = { id: SID }
  const own = () => listOf(env.CLAUDE_CODE_TASK_LIST_ID ?? session.id)
  const tasks = own()
  // The tools the model is offered; a test empties it to take the task tools away.
  const offered = { tools: ['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate'] }
  on('tool.list', () => ({ value: offered.tools.map(name => ({ name, description: '', mcp: false })) }))
  const filled: string[] = []
  let draft = ''
  const submitted: string[] = []
  const status: (string | undefined)[] = []
  const clock = mock.clock(on, { now: Date.UTC(2026, 9, 3, 12) })
  const store = new Map(Object.entries(seed.store ?? {}))
  on('store.get', (_$, e) => ({ value: store.get(e.key) }))
  on('store.set', (_$, e) => {
    store.set(e.key, JSON.parse(JSON.stringify(e.value)))
    return { value: undefined }
  })
  on('store.delete', (_$, e) => {
    store.delete(e.key)
    return { value: undefined }
  })
  on('store.keys', () => ({ value: [...store.keys()] }))
  mock.env(on, { HOME: '/home/me', ...env })
  on('session.id', () => ({ value: session.id }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('tool.register', (_$, e) => ({ value: { tool: `mcp__${PLUGIN}__${e.name}` } }))
  on('ui.status', (_$, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  const copied: string[] = []
  on('ui.copy', (_$, e) => {
    copied.push(e.text)
    return { value: { isCopied: true as const } }
  })
  const opened: { id: string; focus?: true }[] = []
  on('ui.open', (_$, e) => {
    opened.push({ id: e.id, focus: e.focus })
    return { value: { isPlaced: true } }
  })
  on('ui.close', () => ({ value: undefined }))
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('fs.list', (_$, e) => {
    const list = lists.get(e.path)
    if (!list) throw new Error(`ENOENT: ${e.path}`)
    return { value: [...list.keys()].map(id => ({ name: `${id}.json`, kind: 'file' as const, size: 1, mtimeMs: 0, isLink: false })) }
  })
  on('fs.exists', (_$, e) => ({ value: lists.has(e.path) }))
  on('fs.read', (_$, e) => {
    const cut = e.path.lastIndexOf('/')
    const id = e.path.slice(cut + 1).replace(/\.json$/, '')
    const task = lists.get(e.path.slice(0, cut))?.get(id)
    if (!task) throw new Error(`ENOENT: ${e.path}`)
    return { value: JSON.stringify({ id, ...task, blocks: [], blockedBy: [] }) }
  })
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box key="engine" />
  })
  on('tool.call', { tool: 'TaskCreate' }, (_$, e) => {
    const list = own()
    const id = String(Math.max(0, ...[...list.keys()].map(Number)) + 1)
    list.set(id, { subject: String(e.subject), description: String(e.description ?? ''), status: 'pending' })
    return { result: { task: { id, subject: String(e.subject) } } }
  })
  on('tool.call', { tool: 'TaskUpdate' }, (_$, e) => {
    const task = own().get(String(e.taskId))
    if (!task) return { deny: `Task ${e.taskId} not found` }
    if (e.subject !== undefined) task.subject = String(e.subject)
    if (e.status) task.status = String(e.status)
    if (e.description !== undefined) task.description = String(e.description)
    return { result: { success: true, taskId: String(e.taskId), updatedFields: [] } }
  })
  on('prompt.fill', (_$, e) => {
    filled.push(e.text)
    draft = e.text
    return { isFilled: true }
  })
  on('prompt.read', () => ({ value: { text: draft, cursor: draft.length } }))
  return { tasks, lists, store, filled, submitted, status, clock, opened, offered, session, copied, toasts }
}

async function start($: any) {
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
}

const create = ($: any, subject: string, description = 'context') =>
  $.tool.call({ tool: 'TaskCreate', tool_use_id: `tu-${subject.length}-${Math.random()}`, subject, description })

describe('labelling', () => {
  test('TaskCreate gets D and A labels in order', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'DECIDE: retire kapp? (recommend yes) [no link]')
    await create($, 'Fix the flaky test')
    expect([...tasks.values()].map(t => t.subject)).toEqual([
      'D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]',
      'A1 ACTION: run gh auth login [no link]',
      'D2 DECIDE: retire kapp? (recommend yes) [no link]',
      'Fix the flaky test',
    ])
  })

  test('creates in one message never share a label', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    await Promise.all([1, 2, 3, 4].map(n => create($, `DECIDE: option ${n}? (recommend a) [no link]`)))
    const labels = [...tasks.values()].map(t => t.subject.split(' ')[0]).sort()
    expect(labels).toEqual(['D1', 'D2', 'D3', 'D4'])
  })

  test('TaskUpdate keeps the label when the subject changes', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const update = (subject: string) => $.tool.call({ tool: 'TaskUpdate', tool_use_id: `u-${Math.random()}`, taskId: '1', subject })
    // Written as a draft, the way the skill writes subjects: the hook puts D1 back.
    await update('DECIDE: Postgres, SQLite or DuckDB? (recommend SQLite) [#3]')
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: Postgres, SQLite or DuckDB? (recommend SQLite) [#3]')
    // With the matching label it goes through as written.
    await update('D1 DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]')
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]')
    const listed = await $.command.run({ command: 'decisions', args: '', ...RUN })
    expect(listed.text).toContain('D1 DECIDE: Postgres or DuckDB?')
    // A mistyped label and a missing recommendation are refused, and the task is left alone.
    const wrong = await update('D2 DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]')
    expect(wrong.deny).toBe('session-decisions: task 1 is D1; keep its label or drop it and the hook adds it')
    const vague = await update('DECIDE: Postgres or DuckDB? [#3]')
    expect(vague.deny).toContain('needs a recommendation')
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]')
  })

  test('a task retitled into a draft becomes a new item', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'Rotate the deploy key')
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '2', subject: 'ACTION: rotate the deploy key [#9]' })
    expect(tasks.get('2')?.subject).toBe('A2 ACTION: rotate the deploy key [#9]')
    const listed = await $.command.run({ command: 'decisions', args: '', ...RUN })
    expect(listed.text).toContain('A2 ACTION: rotate the deploy key [#9]')
    await create($, 'ACTION: approve the release [no link]')
    expect(tasks.get('3')?.subject).toBe('A3 ACTION: approve the release [no link]')
  })

  test('a recommendation written with a colon and nested parentheses is labelled and accepted whole', async ($, on) => {
    const { tasks, filled } = engine(on)
    await start($)
    const ran = await create($, 'DECIDE: next? (recommend: ship #16 now, then a release (#14, #4 and #20), then #11) [#16]')
    expect(ran.deny).toBeUndefined()
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: next? (recommend: ship #16 now, then a release (#14, #4 and #20), then #11) [#16]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Text', text: 'recommends ship #16 now' })).toBeDefined()
    expect((await ui.find({ key: 'question-D1' }))?.text).toBe('next?')
    await ui.press({ key: 'accept-D1' })
    expect(filled.at(-1)).toBe('D1: go with ship #16 now, then a release (#14, #4 and #20), then #11')
  })

  test('a malformed item is refused with the reason', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    const ran = await create($, 'DECIDE: ship it?')
    expect(ran.deny).toContain('needs a recommendation')
    expect(tasks.size).toBe(0)
  })
})

describe('queue', () => {
  test('/decisions lists open items, and all adds rulings', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, 'ACTION: run gh auth login [no link]')
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '2', status: 'completed', description: 'Ruling (2026-10-03): done' })
    const open = await $.command.run({ command: 'decisions', args: '', ...RUN })
    expect(open.text).toContain('D1 DECIDE: Postgres or SQLite?')
    expect(open.text).not.toContain('A1')
    const all = await $.command.run({ command: 'decisions', args: 'all', ...RUN })
    expect(all.text).toContain('Ruling (2026-10-03): done')
  })

  test('the fallback ledger adds and closes items', async ($, on) => {
    engine(on)
    await start($)
    const added = await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'ACTION', text: 'approve the deploy [no link]' })
    expect(added.result).toBe('A1 ACTION: approve the deploy [no link]')
    const closed = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l2', label: 'a1', ruling: 'approved' })
    expect(closed.result).toBe('A1 closed')
    const missing = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l3', label: 'D9', ruling: 'x' })
    expect(missing.deny).toBe("session-decisions: D9 isn't in this session's decision queue.")
  })

  test('a ledger item draws like a task item, and only /decisions tags it', async ($, on) => {
    engine(on)
    await start($)
    await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'DECIDE', text: 'Postgres or SQLite? (recommend SQLite) [#3]' })
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: PLUGIN, props: {} as never })
      expect(await ui.find({ type: 'Text', text: 'recommends SQLite' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'ledger' })).toBeUndefined()
      await ui.unmount()
    }
    const listed = await $.command.run({ command: 'decisions', args: '', ...RUN })
    expect(listed.text).toBe('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]  (ledger, waiting 0m)')
  })

  test('decision_update revises a ledger item in place and keeps its label', async ($, on) => {
    const { offered } = engine(on)
    await start($)
    offered.tools = []
    const call = (tool: string, args: Record<string, unknown>) =>
      $.tool.call({ tool: `mcp__session-decisions__${tool}`, tool_use_id: `l-${Math.random()}`, ...args })
    await call('decision_add', { kind: 'DECIDE', text: 'serve it as protobuf? (recommend yes) [no link]', description: 'so Cloudflare compresses it' })
    const revised = await call('decision_update', { label: 'd1', text: 'serve it pre-gzipped? (recommend yes) [no link]', description: 'the user asked for an honest content type' })
    expect(revised.result).toBe('D1 DECIDE: serve it pre-gzipped? (recommend yes) [no link]')
    const listed = await $.command.run({ command: 'decisions', args: '', ...RUN })
    expect(listed.text).toBe('D1 DECIDE: serve it pre-gzipped? (recommend yes) [no link]  (ledger, waiting 0m, revised 2026-10-03)')
    // The card marks it revised.
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Text', text: 'revised 2026-10-03' })).toBeDefined()
    await ui.unmount()
    // Its transcript row draws as the revised card.
    const props = { tool_use_id: 'r1', tool: 'mcp__session-decisions__decision_update', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: revised.result } as never
    const row = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 'r1', props })
    expect((await row.find({ key: 'question-D1' }))?.text).toBe('serve it pre-gzipped?')
    await row.unmount()
    // Validated like decision_add, and refused with nothing to change.
    expect((await call('decision_update', { label: 'D1', text: 'serve it pre-gzipped? [no link]' })).deny).toContain('needs a recommendation')
    expect((await call('decision_update', { label: 'D1' })).deny).toContain('give the new text')
    expect((await call('decision_update', { label: 'D9', description: 'x' })).deny).toBe("session-decisions: D9 isn't in this session's decision queue.")
    // A description alone adds a Revised line; a settled item can't be revised.
    expect((await call('decision_update', { label: 'D1', description: 'cached for a day' })).result).toBe('D1 DECIDE: serve it pre-gzipped? (recommend yes) [no link]')
    await call('decision_close', { label: 'D1', ruling: 'superseded by D2' })
    expect((await call('decision_update', { label: 'D1', description: 'x' })).deny).toContain('is settled')
  })

  test('decision_update points task-list items at TaskUpdate', async ($, on) => {
    const { offered } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const update = () =>
      $.tool.call({ tool: 'mcp__session-decisions__decision_update', tool_use_id: `u-${Math.random()}`, label: 'D1', description: 'x' })
    expect((await update()).deny).toContain('revise it with TaskUpdate')
    offered.tools = []
    expect((await update()).deny).toContain('close it with decision_close as superseded')
  })

  test('a TaskUpdate that adds a Revised line marks the card', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await $.tool.call({
      tool: 'TaskUpdate',
      tool_use_id: 'u1',
      taskId: '1',
      subject: 'DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]',
      description: 'context\nRevised (2026-10-04): analytics workload, so DuckDB',
    })
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Text', text: 'revised 2026-10-04' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'recommends DuckDB' })).toBeDefined()
  })

  test('decision_close settles a task-list item once TaskUpdate is gone', async ($, on) => {
    const { tasks, offered } = engine(on)
    on('classic.SessionStart', () => ({}))
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const close = (label: string, ruling: string) =>
      $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: `c-${Math.random()}`, label, ruling })
    // While TaskUpdate is offered, it stays the way to close a task item.
    const refused = await close('A1', 'done')
    expect(refused.deny).toContain('mark it completed with TaskUpdate')
    // A restart drops the task tools: decision_close settles it in the queue instead.
    offered.tools = []
    const closed = await close('A1', 'done')
    expect(closed.result).toContain('A1 closed in the decision queue')
    expect(tasks.get('1')?.status).toBe('pending')
    // The task file still shows it open; the stored ruling wins when the files are read again.
    const after = await $.classic.SessionStart({ source: 'resume', session_id: SID })
    const text = after.additionalContext?.join('\n') ?? ''
    expect(text).toContain('D1 DECIDE')
    expect(text).not.toContain('A1')
    const all = await $.command.run({ command: 'decisions', args: 'all', ...RUN })
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
    expect(all.text).toContain('Ruling (2026-10-03): done')
    // Unknown labels are still refused.
    const missing = await close('A7', 'x')
    expect(missing.deny).toBe("session-decisions: A7 isn't in this session's decision queue.")
  })

  test('ledger items and stored rulings survive a re-read, and stay closable when the tools return', async ($, on) => {
    // The task list keeps its id across the resume; the session doesn't.
    const { offered, session } = engine(on, { CLAUDE_CODE_TASK_LIST_ID: SID })
    on('classic.SessionStart', () => ({}))
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    offered.tools = []
    const added = await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'ACTION', text: 'approve the deploy [no link]' })
    expect(added.result).toBe('A2 ACTION: approve the deploy [no link]')
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l2', label: 'A1', ruling: 'done' })
    // Resumed under a new session id on the same task list, which keys the stored record.
    session.id = 'resumed-session'
    offered.tools = ['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate']
    const after = await $.classic.SessionStart({ source: 'resume', session_id: session.id })
    expect(after.additionalContext?.join('\n')).toBe(
      "Open items in this session's decision queue, still waiting on the user:\n- A2 ACTION: approve the deploy [no link] (fallback ledger)",
    )
    const closed = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l3', label: 'A2', ruling: 'approved' })
    expect(closed.result).toBe('A2 closed')
    const all = await $.command.run({ command: 'decisions', args: 'all', ...RUN })
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
    expect(all.text).toContain('A2 ACTION: approve the deploy [no link]  (ledger, completed)')
  })

  test('compaction re-shows open items to the model', async ($, on) => {
    engine(on)
    on('classic.SessionStart', () => ({}))
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const after = await $.classic.SessionStart({ source: 'compact', session_id: SID })
    expect(after.additionalContext?.join('\n')).toContain('- D1 DECIDE: Postgres or SQLite?')
    const fresh = await $.classic.SessionStart({ source: 'startup' })
    expect(fresh.additionalContext).toBeUndefined()
  })
})

const task = (subject: string, status = 'pending', description = 'context'): Task => ({ subject, description, status })
const ledgerItem = (label: string, body: string, status = 'pending', description = '') => ({
  id: label,
  source: 'ledger',
  label,
  kind: label.startsWith('D') ? 'DECIDE' : 'ACTION',
  body,
  status,
  description,
})
const listAll = ($: any) => $.command.run({ command: 'decisions', args: 'all', ...RUN })

describe('resume and fork', () => {
  test('on a shared task list, the record is kept under the list', async ($, on) => {
    const { store, offered } = engine(on, { CLAUDE_CODE_TASK_LIST_ID: 'shared' })
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    offered.tools = []
    await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'ACTION', text: 'approve the deploy [no link]' })
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l2', label: 'A1', ruling: 'done' })
    expect([...store.keys()]).toEqual(['list:shared'])
    const record = store.get('list:shared') as any
    expect(record.counters).toEqual({ D: 0, A: 2 })
    expect(record.ledger.map((item: any) => item.label)).toEqual(['A2'])
    expect(record.closed['1'].description).toContain('Ruling (2026-10-03): done')
    // Another session on the list writes the record in between: its items and numbers are kept.
    store.set('list:shared', { ...record, counters: { D: 3, A: 2 }, ledger: [...record.ledger, ledgerItem('D3', 'theirs? (recommend yes) [no link]')] })
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l3', label: 'A2', ruling: 'approved' })
    const merged = store.get('list:shared') as any
    expect(merged.counters).toEqual({ D: 3, A: 2 })
    expect(merged.ledger.map((item: any) => [item.label, item.status])).toEqual([['A2', 'completed'], ['D3', 'pending']])
  })

  test('a new session id on the same task list keeps counters, ledger and rulings', async ($, on) => {
    const { session, tasks } = engine(
      on,
      { CLAUDE_CODE_TASK_LIST_ID: 'shared' },
      {
        lists: { shared: { '1': task('A1 ACTION: run gh auth login [no link]'), '2': task('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]') } },
        store: {
          'list:shared': {
            counters: { D: 1, A: 2 },
            created: { A1: 1, D1: 2, A2: 3 },
            ledger: [ledgerItem('A2', 'approve the deploy [no link]')],
            closed: { '1': { id: '1', source: 'tasks', label: 'A1', kind: 'ACTION', body: 'run gh auth login [no link]', status: 'completed', description: 'context\nRuling (2026-10-03): done' } },
            touched: 3,
          },
        },
      },
    )
    session.id = 'resumed-session'
    await start($)
    const all = await listAll($)
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
    expect(all.text).toContain('Ruling (2026-10-03): done')
    expect(all.text).toContain('A2 ACTION: approve the deploy [no link]  (ledger')
    expect(all.text).toContain('D1 DECIDE: Postgres or SQLite?')
    // Numbering carries on from the stored counters.
    await create($, 'ACTION: rotate the deploy key [no link]')
    await create($, 'DECIDE: retire kapp? (recommend yes) [no link]')
    expect(tasks.get('3')?.subject).toBe('A3 ACTION: rotate the deploy key [no link]')
    expect(tasks.get('4')?.subject).toBe('D2 DECIDE: retire kapp? (recommend yes) [no link]')
    const closed = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'c1', label: 'A2', ruling: 'approved' })
    expect(closed.result).toBe('A2 closed')
  })

  test("a session's own record moves under its task list on the first read", async ($, on) => {
    const { store, tasks } = engine(
      on,
      { CLAUDE_CODE_TASK_LIST_ID: 'shared' },
      {
        lists: { shared: { '1': task('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]') } },
        store: {
          [`session:${SID}`]: { counters: { D: 1, A: 1 }, created: { D1: 1, A1: 2 }, ledger: [ledgerItem('A1', 'approve the deploy [no link]')], closed: {}, touched: 2 },
          'session:someone-else': { counters: { D: 9, A: 9 }, created: {}, ledger: [], closed: {}, touched: 1 },
        },
      },
    )
    await start($)
    expect(store.has(`session:${SID}`)).toBe(false)
    expect((store.get('list:shared') as any).ledger.map((item: any) => item.label)).toEqual(['A1'])
    // Another session's record is left where it is.
    expect(store.has('session:someone-else')).toBe(true)
    expect((await listAll($)).text).toContain('A1 ACTION: approve the deploy [no link]  (ledger')
    await create($, 'ACTION: rotate the deploy key [no link]')
    expect(tasks.get('2')?.subject).toBe('A2 ACTION: rotate the deploy key [no link]')
  })

  test('a forked session starts from the record of the session it was forked from', async ($, on) => {
    const D1 = 'D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]'
    const A1 = 'A1 ACTION: run gh auth login [no link]'
    const A2 = 'A2 ACTION: merge the release PR [#16]'
    const { session, store, lists } = engine(on, {}, {
      lists: {
        // The parent revised A2 after the fork; its label still matches.
        parent: { '1': task(D1), '2': task(A1), '3': task('A2 ACTION: merge the release PR, then tag it [#16]') },
        // Same task ids and labels, none of the same words: not this session's parent.
        stranger: { '1': task('D1 DECIDE: tabs or spaces? (recommend tabs) [no link]'), '2': task('A1 ACTION: renew the cert [no link]'), '3': task('A2 ACTION: rotate the key [no link]') },
        // Backgrounding copied the parent's task files under the new session id.
        forked: { '1': task(D1), '2': task(A1), '3': task(A2) },
      },
      store: {
        'session:parent': {
          counters: { D: 1, A: 3 },
          created: { D1: 1, A1: 2, A2: 3, A3: 4 },
          ledger: [ledgerItem('A3', 'approve the deploy [no link]')],
          closed: { '2': { id: '2', source: 'tasks', label: 'A1', kind: 'ACTION', body: 'run gh auth login [no link]', status: 'completed', description: 'context\nRuling (2026-10-03): done' } },
          touched: 5,
        },
        'session:stranger': { counters: { D: 4, A: 4 }, created: {}, ledger: [ledgerItem('A4', 'not ours [no link]')], closed: {}, touched: 9 },
      },
    })
    session.id = 'forked'
    await start($)
    const all = await listAll($)
    expect(all.text).toContain('A3 ACTION: approve the deploy [no link]  (ledger')
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
    expect(all.text).not.toContain('not ours')
    // The fork keeps its own copy; the parent's record stays for the parent.
    expect((store.get('session:forked') as any).ledger.map((item: any) => item.label)).toEqual(['A3'])
    expect(store.has('session:parent')).toBe(true)
    await create($, 'ACTION: rotate the deploy key [no link]')
    expect(lists.get(`${ROOT}/forked`)?.get('4')?.subject).toBe('A4 ACTION: rotate the deploy key [no link]')
  })

  test('a fresh session adopts nothing', async ($, on) => {
    const { store } = engine(on, {}, {
      lists: { other: { '1': task('D1 DECIDE: tabs or spaces? (recommend tabs) [no link]') } },
      store: { 'session:other': { counters: { D: 1, A: 1 }, created: {}, ledger: [ledgerItem('A1', 'not ours [no link]')], closed: {}, touched: 1 } },
    })
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    expect((await listAll($)).text).not.toContain('not ours')
    expect((store.get(`session:${SID}`) as any).ledger).toEqual([])
  })

  test('the resume hook reads the session the input names', async ($, on) => {
    // Under --continue, $.session.id() still answers a placeholder when SessionStart runs.
    const { session } = engine(on, {}, {
      lists: { [SID]: { '1': task('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]') } },
      store: { [`session:${SID}`]: { counters: { D: 1, A: 1 }, created: {}, ledger: [ledgerItem('A1', 'approve the deploy [no link]')], closed: {}, touched: 1 } },
    })
    on('classic.SessionStart', () => ({}))
    session.id = 'placeholder'
    const after = await $.classic.SessionStart({ source: 'resume', session_id: SID })
    const text = after.additionalContext?.join('\n') ?? ''
    expect(text).toContain('- D1 DECIDE: Postgres or SQLite?')
    expect(text).toContain('- A1 ACTION: approve the deploy [no link] (fallback ledger)')
  })

  test('prune keeps the most recently used records of both kinds', async ($, on) => {
    const records: Record<string, unknown> = { unrelated: 'kept' }
    for (let i = 0; i < 110; i++) {
      records[i % 2 ? `list:l${i}` : `session:s${i}`] = { counters: { D: 0, A: 0 }, created: {}, ledger: [], closed: {}, touched: i + 1 }
    }
    const { store } = engine(on, {}, { store: records })
    await start($)
    const kept = [...store.keys()].filter(key => /^(session|list):/.test(key))
    expect(kept).toHaveLength(100)
    expect(store.has('session:s0')).toBe(false)
    expect(store.has('list:l9')).toBe(false)
    expect(store.has('list:l11')).toBe(true)
    expect(store.has('session:s108')).toBe(true)
    expect(store.get('unrelated')).toBe('kept')
  })
})

describe('drawing', () => {
  test('the pane draws on every surface and Accept fills the prompt', async ($, on) => {
    const { filled } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: PLUGIN, props: {} as never })
      expect(await ui.find({ type: 'Text', text: '1 open' })).toBeDefined()
      await ui.press({ key: 'accept-D1' })
      await ui.unmount()
    }
    expect(filled).toEqual(SURFACES.map(() => 'D1: go with SQLite'))
  })

  test('presses build one message, and Accept all answers every recommendation', async ($, on) => {
    const { filled } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'DECIDE: retire kapp? (recommend yes) [no link]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    await ui.press({ key: 'accept-D1' })
    await ui.press({ key: 'done-A1' })
    await ui.press({ key: 'answer-D1' })
    expect(filled.at(-1)).toBe('D1: \nA1: done')
    await ui.press({ key: 'accept-all' })
    expect(filled.at(-1)).toBe('D1: go with SQLite\nA1: done\nD2: go with yes')
  })

  test('settled cards show their ruling, and NO_COLOR draws ASCII', async ($, on) => {
    engine(on, { NO_COLOR: '1' })
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, 'ACTION: run gh auth login [no link]')
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '2', status: 'completed', description: 'Ruling (2026-10-03): moot, logged in already' })
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Text', text: '[1 open]' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '[recommends SQLite]' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '2026-10-03 · moot' })).toBeUndefined()
    await ui.press({ key: 'settled' })
    expect(await ui.find({ type: 'Text', text: '2026-10-03 · moot, logged in already' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'x' })).toBeDefined()
  })

  test('the footer counts open items and opens the queue when pressed', async ($, on) => {
    const { opened } = engine(on)
    await start($)
    const props = { modes: [] } as never
    for (const surface of ['terminal', 'desktop'] as const) {
      const empty = await $.ui.mount({ plugin: PLUGIN, surface, component: 'SessionMode', props })
      expect(await empty.find({ key: 'decisions-footer' })).toBeUndefined()
      await empty.unmount()
    }
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'SessionMode', props })
      expect((await ui.find({ key: 'decisions-count' }))?.text).toBe('2 decisions')
      await ui.press({ key: 'decisions-count' })
      await ui.unmount()
    }
    expect(opened.at(-1)).toEqual({ id: PLUGIN, focus: true })
  })

  test('a TaskCreate row in the transcript draws as its card, on every surface', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const props = { tool_use_id: 't1', tool: 'TaskCreate', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: { task: { id: '1', subject: 'D1 DECIDE: x' } } } as never
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'ToolUse', requestId: 't1', props })
      expect(await ui.find({ type: 'Text', text: 'recommends SQLite' })).toBeDefined()
      expect(await ui.find({ key: 'accept-D1' })).toBeDefined()
      await ui.unmount()
    }
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '1', status: 'completed', description: 'Ruling (2026-10-04): SQLite' })
    const settled = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props })
    expect(await settled.find({ type: 'Text', text: '2026-10-04 · SQLite' })).toBeDefined()
    expect(await settled.find({ key: 'accept-D1' })).toBeUndefined()
  })

  test('the sidebar opens itself in fullscreen and docks borderless', async ($, on) => {
    const { opened } = engine(on)
    await start($)
    const footer = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'SessionMode', props: { modes: [] } as never, viewport: { columns: 200, rows: 50, isFullscreen: true } })
    await footer.unmount()
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    expect(opened).toEqual([{ id: PLUGIN, focus: undefined }])
    const pane = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: { placement: 'dock', bodyColumns: 50 } as never })
    expect((await pane.find({ key: 'question-D1' }))?.text).toBe('Postgres or SQLite?')
    expect(JSON.stringify(await pane.drawn())).not.toContain('borderStyle')
  })
})

describe('markdown', () => {
  const SUBJECT = 'DECIDE: run **`mise run core`** or read [the guide](https://example.com/guide) first? (recommend **core**, it is _faster_) [https://github.com/lokkju/skills/issues/20]'
  const DESCRIPTION = 'Needs `sudo` on ml-01.\n\n```\nmise run flux-up core\n```'
  const RAW = /\*\*|`|\]\(|(^|\s)_\w/

  test('cards draw markdown, and no Text shows raw markers, on every surface', async ($, on) => {
    engine(on)
    await start($)
    await create($, SUBJECT, DESCRIPTION)
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: PLUGIN, props: {} as never })
      expect((await ui.find({ key: 'question-D1' }))?.type).toBe('Markdown')
      expect(await ui.find({ type: 'Text', text: 'recommends core' })).toBeDefined()
      for (const text of await ui.findAll({ type: 'Text' })) expect(text.text).not.toMatch(RAW)
      await ui.unmount()
    }
  })

  test('More shows the description, the link and copy buttons for its commands', async ($, on) => {
    const { copied } = engine(on)
    await start($)
    await create($, SUBJECT, DESCRIPTION)
    await create($, 'ACTION: log in [no link]', '')
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: PLUGIN, props: {} as never })
      expect(await ui.find({ key: 'description-D1' })).toBeUndefined()
      expect(await ui.find({ key: 'more-A1' })).toBeUndefined()
      await ui.press({ key: 'more-D1' })
      expect((await ui.find({ key: 'description-D1' }))?.text).toBe(DESCRIPTION)
      expect(await ui.find({ type: 'Link' })).toBeDefined()
      expect((await ui.find({ key: 'copy-D1-0' }))?.text).toBe('Copy mise run flux-up core')
      await ui.press({ key: 'copy-D1-1' })
      expect((await ui.find({ key: 'more-D1' }))?.text).toBe('Less')
      await ui.press({ key: 'more-D1' })
      expect(await ui.find({ key: 'description-D1' })).toBeUndefined()
      await ui.unmount()
    }
    expect(copied).toEqual(SURFACES.map(() => 'mise run core'))
  })

  test('a settled row and the toast use plain text', async ($, on) => {
    const { toasts } = engine(on)
    await start($)
    await create($, SUBJECT, DESCRIPTION)
    expect(toasts).toContain('D1 added: run mise run core or read the guide first?')
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '1', status: 'completed', description: 'Ruling (2026-10-04): **core**' })
    const props = { tool_use_id: 't1', tool: 'TaskCreate', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: { task: { id: '1', subject: 'D1 DECIDE: x' } } } as never
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props })
    expect(await ui.find({ type: 'Text', text: '2026-10-04 · core' })).toBeDefined()
  })
})

describe('link and label forms', () => {
  test('self-numbered subjects get the hook label, and a bracket that is not a link is refused', async ($, on) => {
    const { tasks } = engine(on)
    await start($)
    await create($, 'D7: DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, '[A4] ACTION: run gh auth login [no link]')
    await create($, 'D9 - DECIDE: retire kapp? (recommend yes) [https://example.com/kapp]')
    expect([...tasks.values()].map(t => t.subject)).toEqual([
      'D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]',
      'A1 ACTION: run gh auth login [no link]',
      'D2 DECIDE: retire kapp? (recommend yes) [https://example.com/kapp]',
    ])
    const refused = await create($, 'ACTION: press [Enter] in the installer')
    expect(refused.deny).toContain('end the subject with one link token')
    expect(tasks.size).toBe(3)
    // A retitle written with a typed prefix keeps the item's own label.
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u-prefix', taskId: '1', subject: '[D7] DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]' })
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: Postgres or DuckDB? (recommend DuckDB) [#3]')
  })
})
