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

function world(on: On, hasCli = true): World {
  const w: World = {
    calls: [],
    commands: [],
    hasCli,
    rows: [
      { id: 'aaa', ts: '2026-10-18T10:41:00', type: 'insight', title: 'jsonl keeps rows', kind: 'new', status: 'active' },
      { id: 'bbb', ts: '2026-10-18T10:52:00', type: 'decision', title: 'old idea', kind: 'updated', status: 'rejected' },
    ],
  }
  bottoms(on)
  mock.env(on, { HOME: '/home/me' })
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
    if (args[0] === 'status') out = { next_batch_min: 6, pending_sweep: 2 }
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

test('session start sweeps and registers /captures', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  await $.session.start(START)
  await clock.settle()
  expect(w.calls).toContainEqual(['sweep'])
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

test('pane lists captures and Reject rejects', async ($, on) => {
  mock.clock(on)
  const w = world(on)
  await $.session.start(START)
  await $.command.run({ command: 'captures', args: '' } as never)
  for (const surface of SURFACES) {
    w.rows = w.rows.map(r => (r.id === 'aaa' ? { ...r, status: 'active' } : r))
    const ui = await $.ui.mount({
      plugin: PLUGIN,
      surface,
      component: 'Pane',
      requestId: 'captures',
      props: {
        title: 'Captures',
        isFocused: true,
        bodyColumns: 120,
        placement: 'dock',
        scroll: { offset: 0, bodyRows: 30 },
        view: {},
      },
      viewport: { columns: 140, rows: 40 },
    } as never)
    await $.command.run({ command: 'captures', args: '' } as never)
    expect(await ui.find({ text: 'jsonl keeps rows' })).toBeDefined()
    expect(await ui.find({ key: 'reject-aaa' })).toBeDefined()
    expect(await ui.find({ key: 'reject-bbb' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'next batch in 6 min · 2 waiting for sweep' })).toBeDefined()
    await ui.press({ key: 'reject-aaa' })
    expect(w.calls).toContainEqual(['reject', 'aaa'])
    expect(await ui.find({ key: 'reject-aaa' })).toBeUndefined()
    await ui.unmount()
  }
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

test('a failing CLI never breaks the turn', async ($, on) => {
  bottoms(on)
  mock.env(on, { HOME: '/home/me' })
  on('fs.read', () => ({ value: CLI }))
  on('process.run', () => ({ value: { ...RUN, exitCode: 1, stdout: '', stderr: 'boom' } }))
  const done = await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as never)
  expect(done.text).toBe('ok')
})
