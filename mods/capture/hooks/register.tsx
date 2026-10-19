import { atom, read, update } from 'claude-code'
import type { EngineInterface as $, Register, Timer } from 'claude-code'

import type { CaptureRow, CaptureStatus } from '../types'

// obsidian-brain auto-capture (#425): starts capture batches while a session
// runs and shows what they saved in a read-only Captures pane with Reject.
// All the work happens in hooks/capture_cli.py; this mod only triggers and draws.

const PLUGIN = 'obsidian-brain'
const PANE = 'captures'
const TICK_MS = 60_000
const REFRESH_MS = 30_000
const rows = atom({ plugin: 'obsidian-brain', key: 'rows' } as const, [])
const status = atom({ plugin: 'obsidian-brain', key: 'status' } as const, null)
const problem = atom({ plugin: 'obsidian-brain', key: 'problem' } as const, '')
// One of each per load, however often session.start (/clear, resume) or /captures runs.
let tick: Timer | undefined
let refresher: Timer | undefined

async function cliPath($: $): Promise<string | undefined> {
  const home = await $.env.get('HOME')
  if (!home) return undefined
  try {
    const text: string = await $.fs.read(`${home}/.claude/obsidian-brain/capture/cli_path`)
    return text.trim() || undefined
  } catch {
    return undefined
  }
}

async function cli($: $, args: string[]): Promise<unknown> {
  const path = await cliPath($)
  if (!path) {
    $.ui.status('capture: not set up')
    return undefined
  }
  const ran = await $.process.run(['python3', path, ...args], { timeoutMs: 20_000 })
  if (ran.exitCode !== 0) throw new Error(`capture ${args[0]} exited ${ran.exitCode}: ${ran.stderr.slice(0, 200)}`)
  return JSON.parse(ran.stdout || 'null')
}

// A capture failure is shown in the pane, never thrown into the session's turn.
async function quietly($: $, work: () => Promise<unknown>): Promise<void> {
  let message = ''
  try {
    await work()
  } catch (err) {
    message = String((err as Error)?.message ?? err).slice(0, 200)
  }
  try {
    await update($, problem, () => message)
  } catch {
    // the module is unloading; nothing is left to show it
  }
}

async function launchIfDue($: $): Promise<void> {
  const sid = await $.session.id()
  await quietly($, () => cli($, ['launch', sid, '--if-due']))
}

async function refresh($: $): Promise<void> {
  const sid = await $.session.id()
  await quietly($, async () => {
    const list = (await cli($, ['ledger-list', '--session', sid, '--limit', '50'])) as CaptureRow[] | undefined
    const now = (await cli($, ['status', '--session', sid])) as CaptureStatus | undefined
    if (list) await update($, rows, () => list)
    if (now) await update($, status, () => now)
  })
}

async function reject($: $, id: string): Promise<void> {
  await quietly($, () => cli($, ['reject', id]))
  await refresh($)
}

// The ledger writes epoch seconds; an ISO string is read too.
function clockTime(ts?: number | string): string {
  if (typeof ts === 'number') {
    const d = new Date(ts * 1000)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }
  const m = typeof ts === 'string' ? ts.match(/T(\d\d:\d\d)/) : null
  return m ? m[1]! : '     '
}

function footer(now: CaptureStatus | null): string {
  if (!now) return ''
  const next = now.next_batch_min === null ? 'first batch after 10 min of work' : `next batch in ${now.next_batch_min} min`
  return `${next} · ${now.pending_sweep} waiting for sweep`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({ name: 'captures', description: 'Show what auto-capture saved this session' })
    // The SessionStart hook already sweeps ended sessions; this only drives this one.
    tick ??= $.clock.every(TICK_MS, () => void launchIfDue($))
    return started
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) void launchIfDue($)
    return done
  })

  on('command.run', { command: 'captures' }, async $ => {
    await refresh($)
    await $.ui.open({ id: PANE, title: 'Captures' })
    refresher ??= $.clock.every(REFRESH_MS, () => void refresh($))
    return { text: 'Captures pane opened.' }
  })

  on('ui.close', { id: PANE }, async ($, e, next) => {
    refresher?.cancel()
    refresher = undefined
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const list = await read($, rows)
    const now = await read($, status)
    const issue = await read($, problem)
    const isDesktop = e.surface === 'desktop'

    return (
      <Box flexDirection="column">
        {list.length === 0 && <Text dimColor>No captures yet this session.</Text>}
        {list.map(row => {
          const isRejected = row.status === 'rejected'
          return (
            <Box key={`row-${row.id}`} flexDirection={isDesktop ? 'column' : 'row'} gap={1}>
              <Text dimColor>{clockTime(row.ts)}</Text>
              <Text bold={!isRejected}>{(row.type ?? '').padEnd(9)}</Text>
              <Text strikethrough={isRejected} dimColor={isRejected}>
                {row.title ?? ''}
              </Text>
              <Text dimColor>{row.kind === 'updated' ? 'updated' : 'new'}</Text>
              {isRejected ? (
                <Text dimColor>rejected</Text>
              ) : (
                <Button key={`reject-${row.id}`} onPress={() => void reject($, row.id)}>
                  {row.kind === 'updated' ? 'Reject update' : 'Reject'}
                </Button>
              )}
            </Box>
          )
        })}
        <Text key="footer" dimColor>
          {footer(now)}
        </Text>
        {issue !== '' && (
          <Text key="problem" color="red">
            {issue}
          </Text>
        )}
      </Box>
    )
  })
}
