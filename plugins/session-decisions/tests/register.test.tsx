import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PLUGIN = 'session-decisions'
const SURFACES = ['terminal', 'desktop', 'vscode', 'mobile'] as const
const SID = 'test-session'
const DIR = `/home/me/.claude/tasks/${SID}`
const RUN = { origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } } as const

/**
 * The engine's own behaviour beneath the plugin: a clock, a store, a task list in memory that
 * the plugin's one read at session start sees as files, a prompt box, and the UI it reports to.
 */
function engine(on: On, env: Record<string, string> = {}) {
  const tasks = new Map<string, { subject: string; description: string; status: string }>()
  const filled: string[] = []
  let draft = ''
  const submitted: string[] = []
  const status: (string | undefined)[] = []
  let next = 1
  const clock = mock.clock(on, { now: Date.UTC(2026, 9, 3, 12) })
  mock.store(on)
  mock.env(on, { HOME: '/home/me', ...env })
  on('session.id', () => ({ value: SID }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('tool.register', (_$, e) => ({ value: { tool: `mcp__${PLUGIN}__${e.name}` } }))
  on('ui.status', (_$, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
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
    if (e.path !== DIR) throw new Error(`ENOENT: ${e.path}`)
    return { value: [...tasks.keys()].map(id => ({ name: `${id}.json`, kind: 'file' as const, size: 1, mtimeMs: 0, isLink: false })) }
  })
  on('fs.exists', (_$, e) => ({ value: e.path === DIR }))
  on('fs.read', (_$, e) => {
    const id = e.path.slice(DIR.length + 1).replace(/\.json$/, '')
    const task = tasks.get(id)
    if (!task) throw new Error(`ENOENT: ${e.path}`)
    return { value: JSON.stringify({ id, ...task, blocks: [], blockedBy: [] }) }
  })
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box key="engine" />
  })
  on('tool.call', { tool: 'TaskCreate' }, (_$, e) => {
    const id = String(next++)
    tasks.set(id, { subject: String(e.subject), description: String(e.description ?? ''), status: 'pending' })
    return { result: { task: { id, subject: String(e.subject) } } }
  })
  on('tool.call', { tool: 'TaskUpdate' }, (_$, e) => {
    const task = tasks.get(String(e.taskId))
    if (!task) return { deny: `Task ${e.taskId} not found` }
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
  return { tasks, filled, submitted, status, clock, opened }
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
    expect(missing.deny).toContain("isn't in the fallback ledger")
  })

  test('compaction re-shows open items to the model', async ($, on) => {
    engine(on)
    on('classic.SessionStart', () => ({}))
    await start($)
    await create($, 'DECIDE: Postgres or SQLite? (recommend SQLite) [#3]')
    const after = await $.classic.SessionStart({ source: 'compact' })
    expect(after.additionalContext?.join('\n')).toContain('- D1 DECIDE: Postgres or SQLite?')
    const fresh = await $.classic.SessionStart({ source: 'startup' })
    expect(fresh.additionalContext).toBeUndefined()
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
    expect(await pane.find({ type: 'Text', text: 'Postgres or SQLite?' })).toBeDefined()
    expect(JSON.stringify(await pane.drawn())).not.toContain('borderStyle')
  })
})
