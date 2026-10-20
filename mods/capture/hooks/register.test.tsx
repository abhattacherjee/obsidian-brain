import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PLUGIN = 'obsidian-brain'
const CLI = '/x/hooks/capture_cli.py'
const SURFACES = ['terminal', 'desktop'] as const

const SID = 'sid-1234'
const RUN = { exitCode: 0, stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

type World = { calls: string[][]; rows: Record<string, unknown>[]; hasCli: boolean; commands: string[] }

// The engine's own answers, which nothing beneath the plugins gives in a test.
function bottoms(on: On): void {
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('session.id', () => ({ value: SID }))
}

function world(on: On, hasCli = true, env = { HOME: '/home/me' } as Record<string, string>): World {
  const w: World = {
    calls: [],
    commands: [],
    hasCli,
    rows: [
      { id: 'aaa', ts: 1792348860.5, type: 'insight', title: 'jsonl keeps rows', kind: 'new', status: 'active' },
      { id: 'bbb', ts: '2026-10-18T10:52:00', type: 'decision', title: 'old idea', kind: 'updated', status: 'rejected' },
    ],
  }
  bottoms(on)
  mock.env(on, env)
  on('command.register', ($, e) => {
    w.commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('fs.read', ($, e) => {
    if (!w.hasCli) throw new Error('ENOENT')
    return { value: `${CLI}\n` }
  })
  on('process.run', ($, e) => {
    const args = e.argv.slice(2)
    w.calls.push(args)
    let out: unknown = { launched: false }
    if (args[0] === 'ledger-list') out = w.rows
    if (args[0] === 'status') out = { next_batch_min: 6, pending_sweep: 2, vault: 'my vault' }
    if (args[0] === 'sweep') out = { launched: [] }
    if (args[0] === 'reject') {
      w.rows = w.rows.map(r => (r.id === args[1] ? { ...r, status: 'rejected' } : r))
      out = w.rows.find(r => r.id === args[1])
    }
    return { value: { ...RUN, stdout: JSON.stringify(out) } }
  })
  return w
}

const START = { cwd: '/w', surface: 'terminal', isInteractive: true } as const

test('session start registers /captures and leaves the sweep to the SessionStart hook', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  await $.session.start(START)
  await clock.settle()
  expect(w.calls).toEqual([])
  expect(w.commands).toContain('captures')
})

test('a main-loop turn end launches if due; a subagent turn does not', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  const sid = SID
  const base = { answer: '', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as const
  await $.turn.complete({ ...base, agentId: 'agent-1' })
  await clock.settle()
  expect(w.calls).toEqual([])
  await $.turn.complete(base)
  await clock.settle()
  expect(w.calls).toEqual([['launch', sid, '--if-due']])
})

test('the timer launches if due', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  const sid = SID
  await $.session.start(START)
  w.calls = []
  await clock.advance(60_000)
  expect(w.calls).toContainEqual(['launch', sid, '--if-due'])
})

test('a second session start (/clear, resume) adds no second timer', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  await $.session.start(START)
  await $.session.start(START)
  w.calls = []
  await clock.advance(60_000)
  expect(w.calls.filter(c => c[0] === 'launch').length).toBe(1)
})

function paneProps(surface: 'terminal' | 'desktop', bodyColumns = 72, bodyRows = 30) {
  return {
    plugin: PLUGIN,
    surface,
    component: 'Pane',
    requestId: 'captures',
    props: {
      title: 'Captures',
      isFocused: true,
      bodyColumns,
      placement: 'dock',
      scroll: { offset: 0, bodyRows },
      view: {},
    },
    viewport: { columns: bodyColumns + 4, rows: bodyRows + 10 },
  } as never
}

async function openPane($: any, on: On) {
  mock.clock(on)
  const w = world(on)
  await $.session.start(START)
  await $.command.run({ command: 'captures', args: '' } as never)
  return w
}

test('pane lists captures and Reject rejects, on both surfaces', async ($, on) => {
  const w = await openPane($, on)
  for (const surface of SURFACES) {
    w.rows = w.rows.map(r => (r.id === 'aaa' ? { ...r, status: 'active' } : r))
    const ui = await $.ui.mount(paneProps(surface))
    await $.command.run({ command: 'captures', args: '' } as never)
    expect(await ui.find({ text: /jsonl keeps rows/ })).toBeDefined()
    const d = new Date(1792348860.5 * 1000) // row aaa's epoch-seconds ts, as the ledger writes it
    const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    expect(await ui.find({ type: 'Text', text: new RegExp(`^${hhmm}$`) })).toBeDefined()
    expect(await ui.find({ key: 'reject-aaa' })).toBeDefined()
    expect(await ui.find({ key: 'reject-bbb' })).toBeUndefined()
    expect(await ui.find({ text: /next batch in 6 min/ })).toBeDefined()
    expect(await ui.find({ text: /2 (sessions )?waiting for sweep/ })).toBeDefined()
    await ui.press({ key: 'reject-aaa' })
    expect(w.calls).toContainEqual(['reject', 'aaa'])
    expect(await ui.find({ key: 'reject-aaa' })).toBeUndefined()
    await ui.unmount()
  }
})

test('terminal rows keep fixed columns; a long title wraps under its column to 3 lines', async ($, on) => {
  const w = await openPane($, on)
  const words = Array.from({ length: 40 }, (_, i) => `word${i}`).join(' ')
  w.rows = [
    { id: 'ccc', ts: 1792348860, type: 'error-fix', title: words, kind: 'updated', status: 'active', summary: words },
  ]
  await $.command.run({ command: 'captures', args: '' } as never)
  const ui = await $.ui.mount(paneProps('terminal', 60))
  // 60 columns less time (5), type (7), kind (7) and three gaps.
  const first = await ui.find({ type: 'Text', text: /^word0 word1 / })
  expect(String(first!.text).length).toBe(60 - 5 - 7 - 7 - 3)
  expect(await ui.find({ type: 'Text', text: 'error  ' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'updated' })).toBeDefined()
  // Lines 2 and 3 sit under the title column; the third ends with "…" and there is no fourth.
  expect(await ui.find({ type: 'Text', text: /^ {14}word6 word7 .*word11$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^ {14}word12 .*word16…$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /word17 word18 word19 word20 word21 word22$/ })).toBeUndefined()
  // The summary wraps to 3 lines too, the last cut with "…".
  expect(await ui.find({ type: 'Text', text: /^ {2}word0 word1 .*word8$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^ {2}word17 .*word24…$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^ {2}word25/ })).toBeUndefined()
  expect(await ui.find({ key: 'reject-ccc' })).toBeDefined()
  expect(await ui.find({ text: /Reject update/ })).toBeDefined()
  await ui.unmount()
})

test('terminal detail strip shows the selected row; focus moves the selection and Reject follows it', async ($, on) => {
  const w = await openPane($, on)
  w.rows = [
    { id: 'n1', ts: 1792348860, type: 'insight', title: 'first', kind: 'new', status: 'active', note_path: 'claude-insights/a.md', summary: '## Error sum one' },
    { id: 'n2', ts: 1792348800, type: 'decision', title: 'second', kind: 'new', status: 'active', note_path: 'claude-insights/b.md', summary: 'sum two' },
  ]
  await $.command.run({ command: 'captures', args: '' } as never)
  const ui = await $.ui.mount(paneProps('terminal'))
  expect(await ui.find({ type: 'Text', text: /^ {2}Error sum one$/ })).toBeDefined() // heading marker dropped
  expect(await ui.find({ type: 'Link', href: 'obsidian://open?vault=my%20vault&file=claude-insights%2Fa.md' } as never)).toBeDefined()
  expect(await ui.find({ key: 'reject-n1' })).toBeDefined()
  await $.ui.focus({ component: 'Pane', requestId: 'captures', plugin: PLUGIN, element: 'row-n2', origin: { kind: 'person' } } as never)
  expect(await ui.find({ text: /sum two/ })).toBeDefined()
  expect(await ui.find({ key: 'reject-n1' })).toBeUndefined()
  await ui.press({ key: 'reject-n2' })
  expect(w.calls).toContainEqual(['reject', 'n2'])
  await ui.unmount()
})

test('terminal draws only the rows that fit, so the footer stays on screen', async ($, on) => {
  const w = await openPane($, on)
  w.rows = Array.from({ length: 40 }, (_, i) => ({ id: `r${i}`, ts: 1792348860 - i, type: 'insight', title: `t${i}`, kind: 'new', status: 'active' }))
  await $.command.run({ command: 'captures', args: '' } as never)
  const ui = await $.ui.mount(paneProps('terminal', 72, 16))
  // 16 rows less header, column names, footer and a two-line detail strip leave 11.
  expect(await ui.find({ key: 'row-r0' })).toBeDefined()
  expect(await ui.find({ key: 'row-r10' })).toBeDefined()
  expect(await ui.find({ key: 'row-r11' })).toBeUndefined()
  expect(await ui.find({ text: /r reject · s scope/ })).toBeDefined()
  await ui.unmount()
})

test('scope switches between this session, today and all projects', async ($, on) => {
  const w = await openPane($, on)
  const ui = await $.ui.mount(paneProps('desktop'))
  w.calls = []
  await ui.press({ key: 'scope-today' })
  const today = w.calls.find(c => c[0] === 'ledger-list')!
  expect(today[1]).toBe('--since')
  const midnight = new Date()
  midnight.setHours(0, 0, 0, 0)
  expect(Number(today[2])).toBe(Math.floor(midnight.getTime() / 1000))
  w.calls = []
  await ui.press({ key: 'scope-all' })
  expect(w.calls.find(c => c[0] === 'ledger-list')).toEqual(['ledger-list', '--limit', '100'])
  await ui.unmount()
  const term = await $.ui.mount(paneProps('terminal'))
  w.calls = []
  await term.press({ key: 'scope' }) // all → session
  expect(w.calls.find(c => c[0] === 'ledger-list')).toEqual(['ledger-list', '--session', SID, '--limit', '50'])
  await term.unmount()
})

test('without cli_path nothing runs and the status line says so', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, false)
  const statuses: unknown[] = []
  on('ui.status', ($, e) => {
    statuses.push(e)
    return { value: undefined }
  })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as never)
  await clock.settle()
  expect(w.calls).toEqual([])
  expect(JSON.stringify(statuses)).toContain('capture: not set up')
})

test('the not-set-up status clears once a later call finds the CLI', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, false)
  const statuses: (string | undefined)[] = []
  on('ui.status', ($, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  const turn = { answer: '', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as const
  await $.turn.complete(turn)
  await clock.settle()
  expect(statuses).toEqual(['capture: not set up'])
  w.hasCli = true
  await $.turn.complete({ ...turn, turnId: 't2' })
  await clock.settle()
  expect(w.calls).toEqual([['launch', SID, '--if-due']])
  expect(statuses).toEqual(['capture: not set up', undefined])
  await $.turn.complete({ ...turn, turnId: 't3' })
  await clock.settle()
  expect(statuses).toEqual(['capture: not set up', undefined])
})

test('a failed cli_path read shows its cause in the pane', async ($, on) => {
  const w = await openPane($, on)
  w.hasCli = false
  w.calls = []
  for (const surface of SURFACES) {
    const ui = await $.ui.mount(paneProps(surface))
    await ui.press({ key: surface === 'terminal' ? 'scope' : 'scope-today' })
    expect(await ui.find({ type: 'Text', text: /cannot read .*cli_path: \S/ })).toBeDefined()
    await ui.unmount()
  }
  expect(w.calls).toEqual([])
})

test('a missing HOME shows its cause in the pane', async ($, on) => {
  mock.clock(on)
  const w = world(on, true, {})
  await $.session.start(START)
  await $.command.run({ command: 'captures', args: '' } as never)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount(paneProps(surface))
    expect(await ui.find({ type: 'Text', text: /HOME is not set/ })).toBeDefined()
    await ui.unmount()
  }
  expect(w.calls.filter(c => c[0] === 'ledger-list')).toEqual([])
})

test('a failing CLI never breaks the turn', async ($, on) => {
  bottoms(on)
  mock.env(on, { HOME: '/home/me' })
  on('fs.read', () => ({ value: CLI }))
  on('process.run', () => ({ value: { ...RUN, exitCode: 1, stdout: '', stderr: 'boom' } }))
  const done = await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as never)
  expect(done.text).toBe('ok')
})
