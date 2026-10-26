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
 * `version` is the Claude Code version $.session.version() reports; null makes the call fail.
 */
function engine(on: On, env: Record<string, string> = {}, seed: Seed = {}, version: string | null = '2.1.310') {
  const lists = new Map<string, Map<string, Task>>()
  for (const [name, list] of Object.entries(seed.lists ?? {})) lists.set(`${ROOT}/${name}`, new Map(Object.entries(list)))
  const listOf = (name: string) => {
    const dir = `${ROOT}/${name}`
    if (!lists.has(dir)) lists.set(dir, new Map())
    return lists.get(dir)!
  }
  // `messages` is the transcript $.session.messages() answers: what the conversation already holds.
  const session = { id: SID, messages: [] as unknown[] }
  on('session.messages', () => ({ value: session.messages as any }))
  const own = () => listOf(env.CLAUDE_CODE_TASK_LIST_ID ?? session.id)
  const tasks = own()
  // The tools the model is offered; a test empties it to take the task tools away. `deferred`
  // names the ones behind ToolSearch that the model hasn't loaded: $.tool.list() still lists them
  // (Claude Code 2.1.286, lokkju/skills#25), but a call fails until they're loaded.
  const offered = { tools: ['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate'], deferred: [] as string[] }
  on('tool.list', () => ({ value: offered.tools.map(name => ({ name, description: '', mcp: false })) }))
  on('tool.describe', (_$, e) => ({ description: e.description, isDeferred: offered.deferred.includes(e.tool) }))
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
  // The commands and tools the plugin registered, by name.
  const registered: string[] = []
  on('command.register', (_$, e) => {
    registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('tool.register', (_$, e) => {
    registered.push(e.name)
    return { value: { tool: `mcp__${PLUGIN}__${e.name}` } }
  })
  on('ui.status', (_$, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  const logged: string[] = []
  on('ui.log', (_$, e) => {
    logged.push(e.text)
    return { value: undefined }
  })
  on('session.version', () => {
    if (version === null) throw new Error('no version')
    return { value: { version, base: version } }
  })
  const copied: string[] = []
  on('ui.copy', (_$, e) => {
    copied.push(e.text)
    return { value: { isCopied: true as const } }
  })
  const opened: { id: string; focus?: true }[] = []
  const panes = new Set<string>()
  on('ui.open', (_$, e) => {
    opened.push({ id: e.id, focus: e.focus })
    panes.add(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_$, e) => {
    panes.delete(e.id)
    return { value: undefined }
  })
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
    if (offered.deferred.includes('TaskUpdate')) return { deny: 'TaskUpdate is deferred; load it with ToolSearch' }
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
  return { tasks, lists, store, filled, submitted, status, clock, opened, panes, offered, session, copied, toasts, logged, registered }
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

  test("Accept answers with the item's own recommendation, not an option note's", async ($, on) => {
    const { filled } = engine(on)
    await start($)
    await create($, 'DECIDE: years? two-digit (agent) or four-digit (recommend four: most records print four); P4 too? (recommend all, P2 with four-digit years) [no link]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Text', text: 'recommends all' })).toBeDefined()
    await ui.press({ key: 'accept-D1' })
    expect(filled.at(-1)).toBe('D1: go with all, P2 with four-digit years')
    await create($, 'DECIDE: ship? (recommend yes) [no link]')
    await ui.press({ key: 'accept-all' })
    expect(filled.at(-1)).toBe('D1: go with all, P2 with four-digit years\nD2: go with yes')
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
    // Its transcript row is one line naming the revision, and its result row draws nothing.
    const props = { tool_use_id: 'r1', tool: 'mcp__session-decisions__decision_update', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: revised.result } as never
    const row = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 'r1', props })
    expect((await row.find({ key: 'mention-D1' }))?.text).toBe('revised D1')
    expect(await row.find({ key: 'question-D1' })).toBeUndefined()
    await row.unmount()
    const result = { tool_use_id: 'r1', tool: 'mcp__session-decisions__decision_update', isErrored: false, output: revised.result } as never
    const shown = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolResult', requestId: 'r1', props: result })
    expect(await shown.find({ key: 'decisions-result' })).toBeDefined()
    expect(await shown.find({ key: 'engine' })).toBeUndefined()
    await shown.unmount()
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

  test('a deferred TaskUpdate the model has not loaded still counts as available', async ($, on) => {
    const { tasks, offered } = engine(on)
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    offered.deferred = ['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate']
    // decision_close and decision_update point at TaskUpdate, and say how to load it.
    const refused = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'c1', label: 'A1', ruling: 'done' })
    expect(refused.deny).toContain('mark it completed with TaskUpdate')
    expect(refused.deny).toContain('load it with ToolSearch first')
    const revise = await $.tool.call({ tool: 'mcp__session-decisions__decision_update', tool_use_id: 'u1', label: 'A1', description: 'x' })
    expect(revise.deny).toContain('revise it with TaskUpdate')
    expect(revise.deny).toContain('load it with ToolSearch first')
    // Nothing was settled in the queue alone, so the task file and the queue still agree.
    expect(tasks.get('1')?.status).toBe('pending')
    expect((await $.command.run({ command: 'decisions', args: '', ...RUN })).text).toContain('A1 ACTION')
    // Once loaded, TaskUpdate closes it in both.
    offered.deferred = []
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u2', taskId: '1', status: 'completed', description: 'context\nRuling (2026-10-03): done' })
    expect(tasks.get('1')?.status).toBe('completed')
    const all = await $.command.run({ command: 'decisions', args: 'all', ...RUN })
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
  })

  test('a background subagent without TaskUpdate settles a task-list item with decision_close', async ($, on) => {
    // $.tool.list() keeps naming the main loop's TaskUpdate inside every subagent (lokkju/skills#37).
    const { tasks } = engine(on)
    let spawned = 0
    on('agent.spawn', () => ({ model: 'haiku', agentId: `agent-${++spawned}` }))
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    await create($, 'ACTION: approve the deploy [no link]')
    const background = (await $.agent.spawn({ prompt: 'settle A1', background: true } as never)).agentId
    const foreground = (await $.agent.spawn({ prompt: 'settle D1', background: false } as never)).agentId
    expect([background, foreground]).toEqual(['agent-1', 'agent-2'])
    const close = (label: string, agentId?: string) =>
      $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: `c-${Math.random()}`, label, ruling: 'done', ...(agentId ? { agentId } : {}) })
    // The background subagent has no TaskUpdate, so decision_close settles the item in the queue.
    const settled = await close('A1', background)
    expect(settled.deny).toBeUndefined()
    expect(settled.result).toContain('A1 closed in the decision queue')
    expect(tasks.get('1')?.status).toBe('pending')
    const revised = await $.tool.call({ tool: 'mcp__session-decisions__decision_update', tool_use_id: 'u1', label: 'D1', description: 'x', agentId: background })
    expect(revised.deny).toContain('close it with decision_close as superseded')
    // A foreground subagent has the main loop's task tools, and the main loop is still sent there.
    expect((await close('D1', foreground)).deny).toContain('mark it completed with TaskUpdate')
    expect((await close('D1')).deny).toContain('mark it completed with TaskUpdate')
    // A subagent whose spawn the plugin didn't see is taken to have no TaskUpdate...
    expect((await close('A2', 'agent-unknown')).result).toContain('A2 closed in the decision queue')
    // ...unless it has called a task tool, which shows it has them.
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u2', taskId: '2', description: 'context', agentId: 'agent-seen' } as never)
    expect((await close('D1', 'agent-seen')).deny).toContain('mark it completed with TaskUpdate')
    const all = await $.command.run({ command: 'decisions', args: 'all', ...RUN })
    expect(all.text).toContain('A1 ACTION: run gh auth login [no link]  (completed)')
    expect(all.text).toContain('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]  (waiting')
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

describe('minimum Claude Code', () => {
  for (const [name, version] of [
    ['an older build', '2.1.289'],
    ['a build whose version it cannot read', null],
  ] as const) {
    test(`on ${name} the plugin says so and stays off`, async ($, on) => {
      const { tasks, toasts, logged, opened, registered } = engine(on, {}, {}, version)
      on('classic.SessionStart', () => ({}))
      await start($)
      expect(registered).toEqual([])
      const said = version ? 'needs Claude Code 2.1.300 or newer; this is 2.1.289' : "couldn't read this one's version"
      expect(toasts.filter(text => text.includes(said))).toHaveLength(1)
      expect(logged.filter(text => text.includes(said))).toHaveLength(1)
      // The model hears it at session start, and keeps the queue itself.
      const begun = await $.classic.SessionStart({ source: 'startup', session_id: SID })
      expect(begun.additionalContext?.join('\n')).toContain(said)
      expect(begun.additionalContext?.join('\n')).toContain('number items yourself')
      // No label, no check, no card, no pane: the engine's own behaviour throughout.
      await create($, 'DECIDE: Postgres or SQLite? [#3]')
      expect([...tasks.values()].map(t => t.subject)).toEqual(['DECIDE: Postgres or SQLite? [#3]'])
      const footer = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'SessionMode', props: { modes: [] } as never })
      expect(await footer.find({ key: 'decisions-footer' })).toBeUndefined()
      expect(await footer.find({ key: 'engine' })).toBeDefined()
      expect(opened).toEqual([])
    })
  }

  test('on the minimum itself the plugin runs', async ($, on) => {
    const { tasks, toasts, registered } = engine(on, {}, {}, '2.1.300')
    await start($)
    expect(registered).toContain('decisions')
    expect(registered).toContain('decision_add')
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    expect(tasks.get('1')?.subject).toBe('D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    expect(toasts.filter(text => text.includes('needs Claude Code'))).toEqual([])
  })
})

describe('hand-off check', () => {
  const HANDOFF = 'Both PRs are green.\n\nLeft for you:\n- Merge #41 and #42\n- Run the backfill on each host'

  function classic(on: On) {
    on('classic.Stop', () => ({}))
    on('classic.UserPromptSubmit', () => ({}))
    on('turn.complete', (_$, e) => ({ text: e.answer }))
  }
  const prompt = ($: any, text = 'next') => $.classic.UserPromptSubmit({ prompt: text })
  const stop = ($: any, message: string, active = false) =>
    $.classic.Stop({ stop_hook_active: active, last_assistant_message: message })

  test('an unqueued hand-off warns the person, never blocks, and reminds the model on the next prompt', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await prompt($, 'ship both fixes')
    const stopped = await stop($, HANDOFF)
    expect(stopped.block).toBeUndefined()
    expect(toasts.at(-1)).toBe('2 hand-offs not queued: Merge #41 and #42')
    const next = await prompt($)
    const text = next.additionalContext?.join('\n') ?? ''
    expect(text).toContain('- Merge #41 and #42')
    expect(text).toContain('- Run the backfill on each host')
    // Once only.
    expect((await prompt($)).additionalContext).toBeUndefined()
  })

  test('a reply whose hand-offs are queued, labelled or absent passes quietly', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await prompt($)
    await create($, 'ACTION: merge #41 and #42 [#41]')
    await create($, 'ACTION: run the backfill on each host [no link]')
    await stop($, HANDOFF)
    await stop($, 'Left for you:\n- A2: rotate the key')
    await stop($, 'Done: merged #41 and ran the backfill. If you need to roll back, run `git revert HEAD`.')
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual([])
    expect((await prompt($)).additionalContext).toBeUndefined()
  })

  test('an item queued and settled in the same turn still covers its line', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await prompt($)
    await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'ACTION', text: 'run the backfill on each host [no link]' })
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l2', label: 'A1', ruling: 'done' })
    await stop($, 'Left for you:\n- Run the backfill on each host')
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual([])
  })

  test('a label the reply names but nobody created is flagged; a settled one is not', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await prompt($)
    await create($, 'ACTION: put the client secret in Vault [no link]')
    await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'DECIDE', text: 'ship it? (recommend yes) [no link]' })
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'l2', label: 'D1', ruling: 'yes' })
    await stop($, 'A1 is the secret; D1 is settled.')
    expect(toasts.filter(toast => toast.includes('not'))).toEqual([])
    await stop($, "I've queued it as D33 with that scope proposal.")
    expect(toasts.at(-1)).toBe('D33 named but not in the queue')
    const text = (await prompt($)).additionalContext?.join('\n') ?? ''
    expect(text).toContain("- I've queued it as D33 with that scope proposal.")
    expect(text).toContain('Your reply named D33')
  })

  test('a Stop hook continuation is left alone', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await stop($, HANDOFF, true)
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual([])
  })

  test('the handoff_check option turns it off', { options: { handoff_check: false } }, async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await stop($, HANDOFF)
    await done($, HANDOFF)
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual([])
    expect((await prompt($)).additionalContext).toBeUndefined()
  })

  // The turn's end, as the engine reports it after any Stop hooks.
  const done = ($: any, answer: string, more: Record<string, unknown> = {}) =>
    $.turn.complete({ answer, durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer', ...more })

  test('when classic.Stop is passed over, the turn end still warns the person and leaves the model alone', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await done($, HANDOFF)
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual(['2 hand-offs not queued: Merge #41 and #42'])
    // No reminder rides on the next prompt: that is classic.Stop's to give.
    expect((await prompt($)).additionalContext).toBeUndefined()
  })

  test('a turn whose classic.Stop ran is not checked again at its end', async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await prompt($)
    await stop($, HANDOFF)
    await done($, HANDOFF)
    expect(toasts.filter(toast => toast.includes('not queued'))).toHaveLength(1)
    // The next turn's Stop is passed over: its end checks again.
    await done($, 'Left for you:\n- Rotate the deploy key')
    expect(toasts.filter(toast => toast.includes('not queued'))).toHaveLength(2)
  })

  test("a subagent's end, an interrupted turn and a queued hand-off pass quietly", async ($, on) => {
    const { toasts } = engine(on)
    classic(on)
    await start($)
    await done($, HANDOFF, { agentId: 'a1' })
    await done($, HANDOFF, { reason: 'aborted', isAborted: true })
    await create($, 'ACTION: merge #41 and #42 [#41]')
    await create($, 'ACTION: run the backfill on each host [no link]')
    await done($, HANDOFF)
    expect(toasts.filter(toast => toast.includes('not queued'))).toEqual([])
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
/** A transcript row: one of the plugin's tools called, and what it answered. */
const said = (tool: string, result: string) => ({
  role: 'assistant',
  text: '',
  toolUses: [{ tool_use_id: `${tool}-${result}`, tool: `mcp__session-decisions__${tool}`, input: {}, result, text: result }],
})

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

  test('a ledger-only session resumed under a new id carries its ledger and numbering on', async ($, on) => {
    const { session, store, lists } = engine(on, {}, {
      store: {
        'session:old': {
          counters: { D: 1, A: 0 },
          created: { D1: 1 },
          ledger: [ledgerItem('D1', 'which cluster? (recommend k3s) [no link]')],
          closed: {},
          touched: 2,
        },
      },
    })
    session.id = 'resumed'
    session.messages = [said('decision_add', 'D1 DECIDE: which cluster? (recommend k3s) [no link]')]
    await start($)
    expect((await $.command.run({ command: 'decisions', args: '', ...RUN })).text).toContain(
      'D1 DECIDE: which cluster? (recommend k3s) [no link]  (ledger',
    )
    // The task tools are back: the next item numbers on from the carried counters.
    await create($, 'DECIDE: retire kapp? (recommend yes) [no link]')
    expect(lists.get(`${ROOT}/resumed`)?.get('1')?.subject).toBe('D2 DECIDE: retire kapp? (recommend yes) [no link]')
    const closed = await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'c1', label: 'D1', ruling: 'k3s' })
    expect(closed.result).toBe('D1 closed')
    // The new id keeps its own copy; the old record stays as it was.
    expect((store.get('session:resumed') as any).ledger.map((item: any) => [item.label, item.status])).toEqual([['D1', 'completed']])
    expect((store.get('session:old') as any).ledger[0].status).toBe('pending')
  })

  test('a transcript the engine loads after session start is read by the resume hook or the next prompt', async ($, on) => {
    const seed = {
      store: {
        'session:old': { counters: { D: 0, A: 1 }, created: {}, ledger: [ledgerItem('A1', 'approve the deploy [no link]')], closed: {}, touched: 2 },
      },
    }
    const { session } = engine(on, {}, seed)
    on('classic.SessionStart', () => ({}))
    on('classic.UserPromptSubmit', () => ({}))
    session.id = 'resumed'
    await start($)
    expect((await listAll($)).text).not.toContain('A1')
    session.messages = [said('decision_add', 'A1 ACTION: approve the deploy [no link]')]
    const after = await $.classic.SessionStart({ source: 'resume', session_id: session.id })
    expect(after.additionalContext?.join('\n')).toContain('- A1 ACTION: approve the deploy [no link] (fallback ledger)')
  })

  test('with no resume hook, the first prompt finds the record once the transcript is there', async ($, on) => {
    const { session } = engine(on, {}, {
      store: {
        'session:old': { counters: { D: 0, A: 1 }, created: {}, ledger: [ledgerItem('A1', 'approve the deploy [no link]')], closed: {}, touched: 2 },
      },
    })
    on('classic.UserPromptSubmit', () => ({}))
    session.id = 'resumed'
    await start($)
    session.messages = [said('decision_add', 'A1 ACTION: approve the deploy [no link]')]
    await $.classic.UserPromptSubmit({ prompt: 'next', session_id: session.id })
    expect((await listAll($)).text).toContain('A1 ACTION: approve the deploy [no link]  (ledger')
  })

  test('a fork with task-list and ledger items needs both to match its parent', async ($, on) => {
    const D1 = 'D1 DECIDE: Postgres or SQLite? (recommend SQLite) [#3]'
    const { session, lists } = engine(on, {}, {
      lists: {
        parent: { '1': task(D1) },
        // Another fork of the same conversation's start, which went another way: its task files
        // match, but its ledger doesn't have the item this conversation added.
        sibling: { '1': task(D1) },
        forked: { '1': task(D1) },
      },
      store: {
        'session:parent': { counters: { D: 1, A: 2 }, created: {}, ledger: [ledgerItem('A2', 'approve the deploy [no link]')], closed: {}, touched: 5 },
        'session:sibling': { counters: { D: 1, A: 2 }, created: {}, ledger: [ledgerItem('A2', 'renew the cert [no link]')], closed: {}, touched: 9 },
      },
    })
    session.id = 'forked'
    session.messages = [
      { role: 'assistant', text: '', toolUses: [{ tool_use_id: 't1', tool: 'TaskCreate', input: {}, result: { task: { id: '1', subject: D1 } } }] },
      said('decision_add', 'A2 ACTION: approve the deploy [no link]'),
    ]
    await start($)
    const all = (await listAll($)).text
    expect(all).toContain('A2 ACTION: approve the deploy [no link]  (ledger')
    expect(all).not.toContain('renew the cert')
    await create($, 'ACTION: rotate the deploy key [no link]')
    expect(lists.get(`${ROOT}/forked`)?.get('2')?.subject).toBe('A3 ACTION: rotate the deploy key [no link]')
  })

  test('an unrelated session in the same project is never carried over', async ($, on) => {
    const { session, store } = engine(on, {}, {
      store: {
        // Both start at D1; only the body tells them apart.
        'session:ours': { counters: { D: 1, A: 0 }, created: {}, ledger: [ledgerItem('D1', 'which cluster? (recommend k3s) [no link]')], closed: {}, touched: 2 },
        'session:theirs': { counters: { D: 1, A: 0 }, created: {}, ledger: [ledgerItem('D1', 'tabs or spaces? (recommend tabs) [no link]')], closed: {}, touched: 9 },
      },
    })
    session.id = 'resumed'
    session.messages = [said('decision_add', 'D1 DECIDE: which cluster? (recommend k3s) [no link]')]
    await start($)
    const all = (await listAll($)).text
    expect(all).toContain('which cluster?')
    expect(all).not.toContain('tabs or spaces?')
    expect((store.get('session:resumed') as any).ledger[0].body).toBe('which cluster? (recommend k3s) [no link]')
  })

  test('a session with an empty transcript takes no ledger-only record', async ($, on) => {
    const { store } = engine(on, {}, {
      store: { 'session:other': { counters: { D: 1, A: 0 }, created: {}, ledger: [ledgerItem('D1', 'not ours? (recommend no) [no link]')], closed: {}, touched: 9 } },
    })
    await start($)
    expect((await listAll($)).text).not.toContain('not ours')
    expect(store.has(`session:${SID}`)).toBe(false)
  })

  test('an item the conversation closed is never revived from an older record', async ($, on) => {
    const { session, lists } = engine(on, {}, {
      store: {
        // Still shows D1 open: the conversation closed it after this was written.
        'session:stale': { counters: { D: 1, A: 0 }, created: {}, ledger: [ledgerItem('D1', 'which cluster? (recommend k3s) [no link]')], closed: {}, touched: 9 },
      },
    })
    session.id = 'resumed'
    session.messages = [
      said('decision_add', 'D1 DECIDE: which cluster? (recommend k3s) [no link]'),
      said('decision_close', 'D1 closed'),
    ]
    await start($)
    expect((await $.command.run({ command: 'decisions', args: '', ...RUN })).text).toBe('No open decisions or actions.')
    // With no record to carry, the labels the conversation used still aren't handed out again.
    await create($, 'DECIDE: retire kapp? (recommend yes) [no link]')
    expect(lists.get(`${ROOT}/resumed`)?.get('1')?.subject).toBe('D2 DECIDE: retire kapp? (recommend yes) [no link]')
  })

  test('labels the transcript shows keep numbering going when the record is gone', async ($, on) => {
    const { session, offered } = engine(on)
    session.messages = [
      { role: 'assistant', text: '', toolUses: [{ tool_use_id: 't1', tool: 'TaskCreate', input: {}, result: { task: { id: '1', subject: 'A4 ACTION: run gh auth login [no link]' } } }] },
      said('decision_add', 'D3 DECIDE: which cluster? (recommend k3s) [no link]'),
    ]
    await start($)
    offered.tools = []
    const d = await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'DECIDE', text: 'retire kapp? (recommend yes) [no link]' })
    const a = await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l2', kind: 'ACTION', text: 'approve the deploy [no link]' })
    expect(d.result).toBe('D4 DECIDE: retire kapp? (recommend yes) [no link]')
    expect(a.result).toBe('A5 ACTION: approve the deploy [no link]')
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

  test('a TaskCreate row in the transcript is one line naming the item, and its result draws nothing, on every surface', async ($, on) => {
    const { opened } = engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const output = { task: { id: '1', subject: 'D1 DECIDE: x' } }
    const props = { tool_use_id: 't1', tool: 'TaskCreate', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output } as never
    const result = { tool_use_id: 't1', tool: 'TaskCreate', isErrored: false, output } as never
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'ToolUse', requestId: 't1', props })
      expect((await ui.find({ key: 'mention-D1' }))?.text).toBe('queued D1')
      // The card itself isn't drawn in the transcript: no question, chip or buttons.
      expect(await ui.find({ type: 'Text', text: 'recommends SQLite' })).toBeUndefined()
      expect(await ui.find({ key: 'accept-D1' })).toBeUndefined()
      await ui.unmount()
      const shown = await $.ui.mount({ plugin: PLUGIN, surface, component: 'ToolResult', requestId: 't1', props: result })
      expect(await shown.find({ key: 'decisions-result' })).toBeDefined()
      expect(await shown.find({ key: 'engine' })).toBeUndefined()
      await shown.unmount()
    }
    // The line opens the item in the queue pane.
    const row = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props })
    await row.press({ key: 'mention-D1' })
    expect(opened.at(-1)).toEqual({ id: PLUGIN, focus: true })
    await row.unmount()
    // ctrl+o draws the call and its result as the engine does.
    const full = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props: { ...(props as object), isExpanded: true } as never })
    expect(await full.find({ key: 'engine' })).toBeDefined()
    await full.unmount()
    const fullResult = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolResult', requestId: 't1', props: { ...(result as object), isExpanded: true } as never })
    expect(await fullResult.find({ key: 'engine' })).toBeDefined()
    await fullResult.unmount()
    // Another tool's result is left alone.
    const other = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolResult', requestId: 'b1', props: { tool_use_id: 'b1', tool: 'Bash', isErrored: false, output: {} } as never })
    expect(await other.find({ key: 'engine' })).toBeDefined()
    await other.unmount()
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '1', status: 'completed', description: 'Ruling (2026-10-04): SQLite' })
    const settled = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props })
    expect(await settled.find({ type: 'Text', text: '2026-10-04 · SQLite' })).toBeDefined()
    expect(await settled.find({ key: 'accept-D1' })).toBeUndefined()
  })

  test('a folded tool group with a TaskCreate in it draws the item line under the group', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const call = (tool: string, output: unknown) => ({ tool_use_id: tool, tool, input: {}, isRunning: false, isErrored: false, isInterrupted: false, output })
    const calls = [call('ToolSearch', {}), call('TaskCreate', { task: { id: '1', subject: 'D1 DECIDE: x' } })]
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'ToolGroup', props: { calls, isActive: false, isExpanded: false } as never })
      expect((await ui.find({ key: 'mention-D1' }))?.text).toBe('queued D1')
      await ui.unmount()
    }
    // Unfolded, the group is ToolUse rows, each drawing its own card.
    const open = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolGroup', props: { calls, isActive: false, isExpanded: true } as never })
    expect(await open.find({ key: 'mention-D1' })).toBeUndefined()
    const running = [call('TaskCreate', undefined)].map(one => ({ ...one, isRunning: true }))
    const live = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolGroup', props: { calls: running, isActive: true, isExpanded: false } as never })
    expect(await live.find({ key: 'mention-D1' })).toBeUndefined()
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

  test('the transcript line opens the pane on the item in full, with its actions, and Accept closes it', async ($, on) => {
    const { filled, copied, opened, panes } = engine(on)
    await start($)
    await create($, SUBJECT, DESCRIPTION)
    const props = { tool_use_id: 't1', tool: 'TaskCreate', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: { task: { id: '1', subject: 'D1 DECIDE: x' } } } as never
    const row = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'ToolUse', requestId: 't1', props })
    await row.press({ key: 'mention-D1' })
    expect(opened.at(-1)).toEqual({ id: PLUGIN, focus: true })
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    // The list gives way to the item: its question, link, whole recommendation and description, as markdown.
    expect(await ui.find({ key: 'question-D1' })).toBeUndefined()
    expect((await ui.find({ key: 'details-question' }))?.type).toBe('Markdown')
    expect((await ui.find({ key: 'details-question' }))?.text).toBe('run **`mise run core`** or read [the guide](https://example.com/guide) first?')
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://github.com/lokkju/skills/issues/20', label: 'https://github.com/lokkju/skills/issues/20' })
    expect((await ui.find({ key: 'details-recommendation-text' }))?.text).toBe('Recommended: **core**, it is _faster_')
    expect((await ui.find({ key: 'details-description-text' }))?.text).toBe(DESCRIPTION)
    expect((await ui.find({ key: 'details-end' }))?.text).toBe('─ end of D1 ─')
    const buttons = (await ui.findAll({ type: 'Button' })).map(one => one.text)
    expect(buttons).toEqual(['Accept', 'Answer', 'Copy link', 'Copy mise run flux-up core', 'Close'])
    await ui.press({ key: 'details-copy-link' })
    await ui.press({ key: 'details-copy-code' })
    expect(copied).toEqual(['https://github.com/lokkju/skills/issues/20', 'mise run flux-up core'])
    expect(await ui.find({ key: 'details-question' })).toBeDefined()
    // The pane opened for the item, so Accept fills the prompt and closes it.
    await ui.press({ key: 'details-accept' })
    expect(filled).toEqual(['D1: go with **core**, it is _faster_'])
    expect(panes.has(PLUGIN)).toBe(false)
    expect(await ui.find({ key: 'details-question' })).toBeUndefined()
    expect(await ui.find({ key: 'question-D1' })).toBeDefined()
  })

  test('in the open queue, Back returns to the list, and Done and Answer fill the prompt', async ($, on) => {
    const { filled, panes } = engine(on)
    await start($)
    await create($, 'ACTION: log in [no link]', '')
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]', '')
    await $.command.run({ command: 'decisions', args: 'pane', ...RUN })
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    // Neither has more to show than its card, so only the label opens the details.
    expect(await ui.find({ key: 'more-A1' })).toBeUndefined()
    await ui.press({ key: 'label-A1' })
    expect((await ui.findAll({ type: 'Button' })).map(one => one.text)).toEqual(['Done', 'Answer', 'Back'])
    expect(await ui.find({ key: 'details-description-text' })).toBeUndefined()
    await ui.press({ key: 'details-back' })
    expect(await ui.find({ key: 'question-A1' })).toBeDefined()
    expect(panes.has(PLUGIN)).toBe(true)
    await ui.press({ key: 'label-A1' })
    await ui.press({ key: 'details-done' })
    await ui.press({ key: 'label-D1' })
    await ui.press({ key: 'details-answer' })
    expect(filled.at(-1)).toBe('A1: done\nD1: ')
    expect(await ui.find({ key: 'question-D1' })).toBeDefined()
    expect(panes.has(PLUGIN)).toBe(true)
  })

  test('a long description is drawn whole, for the pane to scroll', async ($, on) => {
    engine(on)
    await start($)
    const long = ['Why it matters:', '', ...Array.from({ length: 60 }, (_, i) => `- Point ${i + 1}, with **some** detail.`)].join('\n')
    await create($, 'ACTION: log in [no link]', long)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never, viewport: { columns: 60, rows: 20 } })
    await ui.press({ key: 'more-A1' })
    const text = (await ui.find({ key: 'details-description-text' }))?.text
    expect(text).toBe(long)
    expect(text).toContain('- Point 60, with **some** detail.')
    expect((await ui.find({ key: 'details-end' }))?.text).toBe('─ end of A1 ─')
  })

  test('[more] ends the card text only when the details view has more, beside the last row when it fits', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: Keep SQLite for the tests? (recommend yes) [#3]', '')
    await create($, 'DECIDE: Keep SQLite for the tests? (recommend yes) [#3]', 'It runs in memory.')
    await create($, 'DECIDE: Run the suite on Postgres in CI as well as on SQLite locally? (recommend yes) [#4]', 'Slower.')
    await create($, 'DECIDE: Which driver? (recommend psycopg 3, for async) [#5]', 'Async.')
    await create($, 'DECIDE: Keep the café build? (recommend yes) [#6]', 'Menus.')
    await create($, 'ACTION: run `uv sync` [no link]', '')
    const ui = await $.ui.mount({
      plugin: PLUGIN,
      surface: 'terminal',
      component: 'Pane',
      requestId: PLUGIN,
      props: { bodyColumns: 52, placement: 'inline' } as never,
    })
    type Node = { type: string; props?: Record<string, unknown>; hover?: Record<string, unknown>; children?: (Node | string)[] }
    const find = (node: Node | string, key: string): Node | undefined => {
      if (typeof node === 'string') return undefined
      if (node.props?.key === key) return node
      for (const child of node.children ?? []) {
        const found = find(child, key)
        if (found) return found
      }
      return undefined
    }
    const drawn = (await ui.drawn()) as unknown as Node
    const keys = (node: Node | undefined) => (node?.children ?? []).map(one => (typeof one === 'string' ? one : String(one.props?.key ?? one.type)))
    // The bare item: no [more], and its action row has no More either.
    expect(find(drawn, 'more-D1')).toBeUndefined()
    expect(keys(find(find(drawn, 'card-tasks-1')!, 'actions'))).toEqual(['accept-D1', 'answer-D1', 'Box', 'Text'])
    // A one-row question: [more] beside it.
    expect(keys(find(drawn, 'question-D2-tail'))).toEqual(['question-D2-tail-text', 'more-D2'])
    // At 48 columns inside the frame, the question wraps and [more] follows its last row.
    expect((find(drawn, 'question-D3-head')?.props as { text: string }).text).toBe('Run the suite on Postgres in CI as well as on')
    expect(keys(find(drawn, 'question-D3-tail'))).toEqual(['question-D3-tail-text', 'more-D3'])
    // With a qualifier, [more] follows the qualifier rather than the question.
    expect(find(drawn, 'question-D4')?.type).toBe('Markdown')
    expect(keys(find(drawn, 'qualifier-D4-tail'))).toEqual(['qualifier-D4-tail-text', 'more-D4'])
    // A line whose width can't be told from its text: [more] on a row of its own.
    expect(keys(find(drawn, 'question-D5-end'))).toEqual(['question-D5', 'more-D5'])
    // A command in backticks is something the details view offers to copy.
    expect((await ui.find({ key: 'more-A1' }))?.text).toBe('[more]')
    // [more] is never dim: it's drawn in the accent color and underlined, so it reads as a control
    // after the question (normal text) and after the qualifier (dim text) alike, and on its own row.
    expect(find(drawn, 'qualifier-D4-tail-text')?.children?.[0]).toMatchObject({ props: { dimColor: true } })
    expect((find(drawn, 'question-D2-tail-text')?.children?.[0] as Node).props?.dimColor).toBeUndefined()
    for (const label of ['D2', 'D3', 'D4', 'D5', 'A1']) {
      const button = find(drawn, `more-${label}`)!
      expect(button.type).toBe('Button')
      expect(button).toMatchObject({ props: { label: '[more]', plain: true }, hover: { bold: true } })
      expect(button.props?.dimColor).toBeUndefined()
      expect(button.children).toEqual([{ type: 'Text', props: { color: 'claude', underline: true }, children: ['[more]'] }])
    }
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

  test('a short link stays on the button row; a URL gets its own line', async ($, on) => {
    engine(on)
    await start($)
    await create($, 'DECIDE: ship it? (recommend yes) [#3]')
    await create($, 'ACTION: review the PR [https://github.com/lokkju/skills/pull/20/files]')
    await create($, 'ACTION: read the repo [https://github.com/lokkju/skills]')
    const LONG = 'https://example.com/a/very/long/path/that/would/never/fit/beside/the/buttons/of/a/fifty-two/column/sidebar?with=query'
    await create($, `ACTION: read the runbook [${LONG}]`)
    await create($, 'ACTION: open the dashboard [http://grafana.lab/d/x]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    // Per card: the link text at the end of its actions row, and the link drawn on a line of its own.
    type Node = { type: string; props?: Record<string, unknown>; children?: (Node | string)[] }
    const textOf = (node: Node | string): string =>
      typeof node === 'string' ? node : node.type === 'Link' ? String(node.props?.label ?? node.props?.href) : (node.children ?? []).map(textOf).join('')
    const cards = new Map<string, { row: string; own: string[] }>()
    const walk = (node: Node | string) => {
      if (typeof node === 'string') return
      if (String(node.props?.key ?? '').startsWith('card-')) {
        const children = (node.children ?? []).filter((one): one is Node => typeof one !== 'string')
        const top = children.find(one => one.props?.key === 'top')!
        const label = String(top.children?.map(one => (typeof one === 'string' ? '' : one.props?.label ?? '')).join(''))
        const actions = children.find(one => one.props?.key === 'actions')!
        const last = actions.children?.at(-1)
        cards.set(label, {
          row: last && typeof last !== 'string' && last.type === 'Text' ? textOf(last) : '',
          own: children.filter(one => one.type === 'Text').map(textOf),
        })
      }
      for (const child of node.children ?? []) walk(child)
    }
    walk((await ui.drawn()) as unknown as Node)
    expect(cards.get('D1')).toEqual({ row: '#3', own: [] })
    expect(cards.get('A1')).toEqual({ row: '', own: ['lokkju/skills#20'] })
    expect(cards.get('A2')).toEqual({ row: '', own: ['lokkju/skills'] })
    expect(cards.get('A3')).toEqual({ row: '', own: [LONG] })
    expect(cards.get('A4')).toEqual({ row: '', own: ['http://grafana.lab/d/x'] })
    const links = await ui.findAll({ type: 'Link' })
    expect(links.map(link => link.text)).toEqual(['lokkju/skills#20', 'lokkju/skills', LONG])
    // Wrapped, never truncated, so the whole URL is there to read.
    const drawn = JSON.stringify(await ui.drawn())
    expect(drawn).toContain(`{"type":"Text","props":{"wrap":"wrap"},"children":[{"type":"Link","props":{"href":"${LONG}"`)
  })

  test('with a GitHub remote, references link to it in the card, the question and the details view', async ($, on) => {
    engine(on)
    const ran: string[][] = []
    on('process.run', (_$, e) => {
      ran.push([...e.argv])
      const origin = e.argv.join(' ') === 'git remote get-url origin'
      return { value: { exitCode: origin ? 0 : 2, stdout: origin ? 'git@github.com:lokkju/skills.git\n' : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    })
    await start($)
    expect(ran.slice(0, 2)).toEqual([
      ['git', 'config', '--get-regexp', '^tracker\\.(forge|web)$'],
      ['git', 'remote', 'get-url', 'origin'],
    ])
    await create($, 'DECIDE: merge !4 before #9? (recommend yes) [#3]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    const link = await ui.find({ type: 'Link' })
    expect(link?.props).toEqual({ href: 'https://github.com/lokkju/skills/issues/3', label: '#3' })
    expect((await ui.find({ key: 'question-D1' }))?.text).toBe(
      'merge [!4](https://github.com/lokkju/skills/pull/4) before [#9](https://github.com/lokkju/skills/issues/9)?',
    )
    await ui.press({ key: 'more-D1' })
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://github.com/lokkju/skills/issues/3', label: 'https://github.com/lokkju/skills/issues/3' })
    expect((await ui.find({ key: 'details-question' }))?.text).toBe(
      'merge [!4](https://github.com/lokkju/skills/pull/4) before [#9](https://github.com/lokkju/skills/issues/9)?',
    )
    expect(await ui.find({ key: 'details-copy-link' })).toBeDefined()
  })

  test('the forge_hosts option links a host the plugin cannot name, and notes the entries it skips', { options: { forge_hosts: 'git.lab.lokkju.com=forgejo, bad' } }, async ($, on) => {
    const { toasts } = engine(on)
    on('process.run', () => ({ value: { exitCode: 0, stdout: 'ssh://git@git.lab.lokkju.com/lokkju/demo.git\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
    await start($)
    expect(toasts).toContain('session-decisions: forge_hosts ignores "bad" is not host=kind')
    await create($, 'DECIDE: merge !4 first? (recommend yes) [#12]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://git.lab.lokkju.com/lokkju/demo/issues/12', label: '#12' })
    expect((await ui.find({ key: 'question-D1' }))?.text).toBe('merge [!4](https://git.lab.lokkju.com/lokkju/demo/pulls/4) first?')
  })

  test('the repo override in git config wins over the remote and forge_hosts, and notes values it skips', { options: { forge_hosts: 'homelab-git=github' } }, async ($, on) => {
    const { toasts } = engine(on)
    const ran: string[][] = []
    on('process.run', (_$, e) => {
      ran.push([...e.argv])
      const config = e.argv[1] === 'config'
      const stdout = config ? 'tracker.forge forgejo\ntracker.web https://git.lab.lokkju.com/lokkju/demo\n' : 'homelab-git:lokkju/demo.git\n'
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    })
    await start($)
    // With the web base set, the remote isn't read at all.
    expect(ran.filter(argv => argv[1] === 'remote')).toEqual([])
    await create($, 'DECIDE: merge !4 first? (recommend yes) [#12]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://git.lab.lokkju.com/lokkju/demo/issues/12', label: '#12' })
    expect((await ui.find({ key: 'question-D1' }))?.text).toBe('merge [!4](https://git.lab.lokkju.com/lokkju/demo/pulls/4) first?')
    expect(toasts.filter(toast => toast.includes('git config'))).toEqual([])
  })

  test('a git config override it cannot use is named in a toast, and the remote still decides', async ($, on) => {
    const { toasts } = engine(on)
    on('process.run', (_$, e) => {
      const stdout = e.argv[1] === 'config' ? 'tracker.forge bitbucket\ntracker.web git@x.example:a/b\n' : 'git@github.com:lokkju/skills.git\n'
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    })
    await start($)
    expect(toasts).toContain(
      'session-decisions: git config ignores tracker.forge "bitbucket": the kind is github, gitlab, forgejo or gitea; tracker.web "git@x.example:a/b" is not the https URL of a repository',
    )
    await create($, 'DECIDE: ship it? (recommend yes) [#3]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://github.com/lokkju/skills/issues/3', label: '#3' })
  })

  test('with no remote a forge can be named for, references stay text', async ($, on) => {
    engine(on)
    on('process.run', () => ({ value: { exitCode: 0, stdout: 'https://git.lab.example.org/me/repo.git\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
    await start($)
    await create($, 'DECIDE: merge before #9? (recommend yes) [#3]')
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect(await ui.find({ type: 'Link' })).toBeUndefined()
    expect((await ui.find({ key: 'question-D1' }))?.text).toBe('merge before #9?')
  })
})

describe('reconcile', () => {
  test('/decisions reconcile shows the brief, notes it for the model, and asks for the pass', async ($, on) => {
    const { submitted, session, toasts, clock } = engine(on)
    await start($)
    await create($, 'DECIDE: btrfs or zfs? (recommend zfs) [no link]')
    await create($, 'ACTION: add the disks to the pool [#12]')
    session.messages = [{ role: 'user', text: 'A1: done', toolUses: [] }]
    const ran = await $.command.run({ command: 'decisions', args: 'reconcile', ...RUN })
    expect(ran.text).toContain('| D1 | DECIDE | 0m | none | btrfs or zfs? (recommend zfs) |')
    expect(ran.text).toContain('| A1 | ACTION | 0m | #12 | add the disks to the pool |')
    expect(ran.text).toContain('- A1: "A1: done"')
    expect(ran.context).toEqual([ran.text])
    // The host refuses a submit from inside the command, so it goes out once the command has answered.
    expect(submitted).toEqual([])
    await clock.advance(1)
    expect(toasts.filter(text => text.includes("couldn't send"))).toEqual([])
    expect(submitted).toHaveLength(1)
    expect(submitted[0]?.startsWith("Reconcile this session's decision queue")).toBe(true)
  })

  test('/decisions wrap sends its prompt once the command has answered', async ($, on) => {
    const { submitted, clock, toasts } = engine(on)
    await start($)
    await create($, 'ACTION: add the disks to the pool [#12]')
    const ran = await $.command.run({ command: 'decisions', args: 'wrap', ...RUN })
    expect(ran.text).toContain('A1 ACTION: add the disks to the pool [#12]')
    await clock.advance(1)
    expect(toasts.filter(text => text.includes("couldn't send"))).toEqual([])
    expect(submitted.map(text => text.split('\n')[0]?.slice(0, 38))).toEqual(["Wrap up this session's decision queue,"])
  })

  test('/decisions reconcile with nothing open sends nothing', async ($, on) => {
    const { submitted } = engine(on)
    await start($)
    const ran = await $.command.run({ command: 'decisions', args: 'reconcile', ...RUN })
    expect(ran.text).toBe('No open decisions or actions; nothing to reconcile.')
    expect(submitted).toEqual([])
  })

  test('decision_list answers the open items from both sources', async ($, on) => {
    const { offered } = engine(on)
    await start($)
    await create($, 'ACTION: run gh auth login [no link]')
    offered.tools = []
    await $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: 'l1', kind: 'DECIDE', text: 'ship it? (recommend yes) [#4]' })
    const listed = await $.tool.call({ tool: 'mcp__session-decisions__decision_list', tool_use_id: 'l2' })
    expect(listed.result).toContain('| A1 | ACTION | 0m | none | run gh auth login |')
    expect(listed.result).toContain('| D1 (ledger) | DECIDE | 0m | #4 | ship it? (recommend yes) |')
  })
})

describe('superseding', () => {
  const SUPERSEDE = 'mcp__session-decisions__decision_supersede'
  const supersede = ($: any, label: string, by: string, why = 'the web JSON is blocked for every server-side request') =>
    $.tool.call({ tool: SUPERSEDE, tool_use_id: `s-${Math.random()}`, label, by, why })

  test('a settled task item whose task file is gone is marked superseded through the queue', async ($, on) => {
    const { tasks, registered } = engine(on)
    on('classic.SessionStart', () => ({}))
    await start($)
    expect(registered).toContain('decision_supersede')
    await create($, 'DECIDE: read Depop how? (recommend the web JSON) [#8]')
    await create($, 'Fix the flaky test')
    await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u1', taskId: '1', status: 'completed', description: 'context\nRuling (2026-10-03): try the web JSON for now' })
    // Claude Code deletes a list's task files once every task in it is completed; TaskUpdate can't reach it.
    tasks.delete('1')
    await create($, 'DECIDE: Depop, now the web JSON is blocked? (recommend apply for OAuth) [#8]')
    const lost = await $.tool.call({ tool: 'TaskUpdate', tool_use_id: 'u2', taskId: '1', description: 'Superseded' })
    expect(lost.deny).toBe('Task 1 not found')
    // An open item, an unknown one, and an unknown replacement are refused.
    expect((await supersede($, 'D2', 'D1')).deny).toContain('D2 is still open; settle it with the ruling "superseded by D1"')
    expect((await supersede($, 'D9', 'D2')).deny).toContain("D9 isn't in this session's decision queue")
    expect((await supersede($, 'D1', 'D7')).deny).toContain("D7 isn't another item in this session's decision queue; create the new item first")
    expect((await supersede($, 'D1', 'D1')).deny).toContain("D1 isn't another item")
    const marked = await supersede($, 'd1', 'd2')
    expect(marked.result).toBe('D1 superseded by D2')
    const check = async () => {
      const all = (await listAll($)).text
      expect(all).toContain('D1 DECIDE: read Depop how? (recommend the web JSON) [#8]  (superseded)')
      expect(all).toContain('    Ruling (2026-10-03): try the web JSON for now\n    Superseded (2026-10-03) by D2: the web JSON is blocked for every server-side request')
      expect(all).toContain('    replaces D1: try the web JSON for now')
    }
    await check()
    // The new card says what it replaces; the old one shows as superseded, not just completed.
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: PLUGIN, props: {} as never })
    expect((await ui.find({ key: 'replaces-D2' }))?.text).toBe('replaces D1: try the web JSON for now')
    await ui.press({ key: 'settled' })
    expect(await ui.find({ type: 'Text', text: '2026-10-03 · superseded by D2: the web JSON is blocked for every server-side request' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '→' })).toBeDefined()
    await ui.unmount()
    // A resume reads the task files again; the queue's own record keeps D1, its ruling and its mark.
    await $.classic.SessionStart({ source: 'resume', session_id: SID })
    await check()
  })

  test('a settled ledger item is marked superseded the same way', async ($, on) => {
    const { offered, store } = engine(on)
    offered.tools = []
    await start($)
    const add = (text: string) => $.tool.call({ tool: 'mcp__session-decisions__decision_add', tool_use_id: `a-${Math.random()}`, kind: 'DECIDE', text })
    await add('read Depop how? (recommend the web JSON) [#8]')
    await $.tool.call({ tool: 'mcp__session-decisions__decision_close', tool_use_id: 'c1', label: 'D1', ruling: 'try the web JSON for now' })
    await add('Depop now? (recommend apply for OAuth) [#8]')
    expect((await supersede($, 'D1', 'D2')).result).toBe('D1 superseded by D2')
    const record = store.get(`session:${SID}`) as any
    expect(record.ledger.find((item: any) => item.label === 'D1').description).toContain('Superseded (2026-10-03) by D2: the web JSON is blocked')
    const listed = await $.tool.call({ tool: 'mcp__session-decisions__decision_list', tool_use_id: 'l1' })
    expect(listed.result).toContain('| D2 (ledger) | DECIDE | 0m | #8 | Depop now? (recommend apply for OAuth) (supersedes D1: try the web JSON for now) |')
  })
})
