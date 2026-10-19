import { atom, read, update } from 'claude-code'
import type { EngineInterface as $, Register, Timer } from 'claude-code'

import type { CaptureRow, CaptureScope, CaptureStatus } from '../types'

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
const scope = atom({ plugin: 'obsidian-brain', key: 'scope' } as const, 'session')
const selected = atom({ plugin: 'obsidian-brain', key: 'selected' } as const, '')
const SCOPES: CaptureScope[] = ['session', 'today', 'all']
const SCOPE_LABEL: Record<CaptureScope, string> = { session: 'This session', today: 'Today', all: 'All projects' }
// Short labels keep the terminal's type column at 7 cells; the desktop has room for the long ones.
const TYPES: Record<string, { short: string; long: string; color: string }> = {
  insight: { short: 'insight', long: 'Insight', color: '#4ade80' },
  decision: { short: 'decide', long: 'Decision', color: '#60a5fa' },
  'error-fix': { short: 'error', long: 'Error fix', color: '#f472b6' },
}
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

// Local midnight in epoch seconds, as the ledger's ts counts.
function startOfToday(): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return Math.floor(d.getTime() / 1000)
}

function listArgs(which: CaptureScope, sid: string): string[] {
  if (which === 'session') return ['ledger-list', '--session', sid, '--limit', '50']
  if (which === 'today') return ['ledger-list', '--since', String(startOfToday()), '--limit', '100']
  return ['ledger-list', '--limit', '100']
}

async function refresh($: $): Promise<void> {
  const sid = await $.session.id()
  const which = await read($, scope)
  await quietly($, async () => {
    const list = (await cli($, listArgs(which, sid))) as CaptureRow[] | undefined
    const now = (await cli($, ['status', '--session', sid])) as CaptureStatus | undefined
    if (list) await update($, rows, () => list)
    if (now) await update($, status, () => now)
  })
}

async function reject($: $, id: string): Promise<void> {
  await quietly($, () => cli($, ['reject', id]))
  await refresh($)
}

async function setScope($: $, which: CaptureScope): Promise<void> {
  await update($, scope, () => which)
  await update($, selected, () => '')
  await refresh($)
}

function typeOf(row: CaptureRow): { short: string; long: string; color: string } {
  const t = row.type ?? ''
  return TYPES[t] ?? { short: t.slice(0, 7), long: t, color: '#a1a1aa' }
}

// Pad or cut to exactly `width` cells so the terminal's columns never wrap.
function fit(text: string, width: number): string {
  if (width <= 0) return ''
  return text.length > width ? `${text.slice(0, Math.max(0, width - 1))}…` : text.padEnd(width)
}

// Word-wrap to `width`, as control-tower's wrapText does; a word longer than the line is split.
// Past `max` lines the last one is cut with "…".
function wrap(text: string, width: number, max: number): string[] {
  const room = Math.max(1, width)
  const lines: string[] = []
  let line = ''
  for (const word of text.split(/\s+/)) {
    if (!word) continue
    if (line && line.length + 1 + word.length > room) {
      lines.push(line)
      line = ''
    }
    let rest = word
    while (!line && rest.length > room) {
      lines.push(rest.slice(0, room))
      rest = rest.slice(room)
    }
    line = line ? `${line} ${rest}` : rest
  }
  if (line) lines.push(line)
  if (lines.length <= max) return lines.length ? lines : ['']
  const kept = lines.slice(0, max)
  const last = kept[max - 1]!
  kept[max - 1] = last.length >= room ? `${last.slice(0, room - 1)}…` : `${last}…`
  return kept
}

function obsidianUrl(vault: string | undefined, notePath: string | undefined): string | undefined {
  if (!vault || !notePath) return undefined
  return `obsidian://open?vault=${encodeURIComponent(vault)}&file=${encodeURIComponent(notePath)}`
}

// Rows grouped by session, in list order, for the views that span sessions.
function groups(list: CaptureRow[]): { key: string; label: string; rows: CaptureRow[] }[] {
  const out: { key: string; label: string; rows: CaptureRow[] }[] = []
  for (const row of list) {
    const key = row.session_id ?? ''
    let g = out.find(x => x.key === key)
    if (!g) {
      g = { key, label: `${row.project ?? '?'} · ${key.slice(0, 8)}`, rows: [] }
      out.push(g)
    }
    g.rows.push(row)
  }
  return out
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

function nextBatch(now: CaptureStatus | null): string {
  if (!now) return ''
  return now.next_batch_min === null ? 'first batch after 10 min of work' : `next batch in ${now.next_batch_min} min`
}

function footer(now: CaptureStatus | null): string {
  if (!now) return ''
  return `${nextBatch(now)} · ${now.pending_sweep} waiting for sweep`
}

const TIME_W = 5
const TYPE_W = 7
const KIND_W = 7
// A long title or summary wraps to at most this many lines.
const MAX_LINES = 3

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
  }).catch(($, e, next) => (next.called ? undefined : next(e)))

  // ↑↓ in the terminal moves the focus ring across the rows; the focused row is the selected one.
  on('ui.focus', { requestId: PANE }, async ($, e, next) => {
    const el = e.element ?? ''
    if (el.startsWith('row-')) await update($, selected, () => el.slice(4))
    return next(e)
  }).catch(($, e, next) => (next.called ? {} : next(e)))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const list = await read($, rows)
    const now = await read($, status)
    const issue = await read($, problem)
    const which = await read($, scope)
    const chosen = await read($, selected)
    const saved = list.filter(r => r.status !== 'rejected').length
    const rejected = list.length - saved
    const problemLine =
      issue !== '' ? (
        <Text key="problem" color="red">
          {issue}
        </Text>
      ) : null

    if (e.surface !== 'terminal') {
      return (
        <Box flexDirection="column" gap={1}>
          <Box key="head" flexDirection="column">
            <Text weight="semibold">Captures</Text>
            <Text dimColor>Saved in the background · review is optional</Text>
          </Box>
          <Box key="tabs" flexDirection="row" gap={1}>
            {SCOPES.map(s => (
              <Button
                key={`scope-${s}`}
                variant={s === which ? 'primary' : 'secondary'}
                onPress={() => void setScope($, s)}
              >
                {SCOPE_LABEL[s]}
              </Button>
            ))}
          </Box>
          {list.length === 0 && <Text dimColor>No captures yet{which === 'session' ? ' this session' : ''}.</Text>}
          {groups(list).map(g => (
            <Box key={`group-${g.key}`} flexDirection="column" gap={1}>
              <Text dimColor>
                {g.label} · {g.rows.filter(r => r.status !== 'rejected').length} saved
              </Text>
              {g.rows.map(row => {
                const t = typeOf(row)
                const url = obsidianUrl(now?.vault, row.note_path)
                if (row.status === 'rejected') {
                  return (
                    <Box key={`card-${row.id}`} flexDirection="column" padding={1} borderStyle="round" borderDimColor>
                      <Text dimColor size="footnote">
                        Rejected · {clockTime(row.ts)} · moved to _rejected
                      </Text>
                      <Text dimColor strikethrough>
                        {row.title ?? ''}
                      </Text>
                    </Box>
                  )
                }
                return (
                  <Box key={`card-${row.id}`} flexDirection="column" gap={1} padding={1} borderStyle="round">
                    <Box flexDirection="row" gap={1}>
                      <Text color={t.color} weight="semibold" size="footnote">
                        {t.long}
                      </Text>
                      <Text dimColor size="footnote">
                        {row.kind === 'updated' ? 'Updated existing note' : 'New'}
                      </Text>
                      <Text dimColor size="footnote">
                        {clockTime(row.ts)}
                      </Text>
                    </Box>
                    <Text weight="semibold">{row.title ?? ''}</Text>
                    {row.summary ? <Text dimColor>{row.summary}</Text> : null}
                    <Box flexDirection="row" gap={2} alignItems="center">
                      {url ? <Link href={url}>Open in Obsidian</Link> : null}
                      <Button key={`reject-${row.id}`} onPress={() => void reject($, row.id)}>
                        {row.kind === 'updated' ? 'Reject update' : 'Reject'}
                      </Button>
                    </Box>
                  </Box>
                )
              })}
            </Box>
          ))}
          <Box key="footer" flexDirection="row" justifyContent="space-between">
            <Text dimColor>{nextBatch(now)}</Text>
            <Text dimColor>{now ? `${now.pending_sweep} sessions waiting for sweep` : ''}</Text>
          </Box>
          {problemLine}
        </Box>
      )
    }

    const cols = Math.max(40, e.props.bodyColumns)
    const titleW = cols - TIME_W - TYPE_W - KIND_W - 3
    const indent = ' '.repeat(TIME_W + TYPE_W + 2)
    const active = list.filter(r => r.status !== 'rejected')
    const focus = list.find(r => r.id === chosen && r.status !== 'rejected') ?? active[0]
    const multi = which !== 'session'
    const url = focus ? obsidianUrl(now?.vault, focus.note_path) : undefined
    const titleOf = (row: CaptureRow) => (multi ? `${row.project ?? '?'}: ${row.title ?? ''}` : (row.title ?? ''))
    const titleLines = list.map(row => (row.status === 'rejected' ? [fit(titleOf(row), titleW)] : wrap(titleOf(row), titleW, MAX_LINES)))
    const summaryLines = focus?.summary ? wrap(focus.summary, cols - 2, MAX_LINES) : []
    const pathLines = focus?.note_path ? Math.ceil(focus.note_path.length / (cols - 2)) : 0
    // Header, column names and footer; then the detail strip: a gap, its first line, the path and the summary.
    const chrome = 3 + (list.length === 0 ? 1 : 0) + (focus ? 2 + pathLines + summaryLines.length : 0)
    // Draw only the rows that fit, around the selected one, so the strip and footer stay on screen.
    const room = Math.max(1, e.props.scroll.bodyRows - chrome)
    const at = focus ? list.indexOf(focus) : 0
    let first = at
    let last = at
    let used = titleLines[at]?.length ?? 0
    for (let grew = true; grew; ) {
      grew = false
      const below = titleLines[last + 1]
      if (below && used + below.length <= room) {
        last += 1
        used += below.length
        grew = true
      }
      const above = titleLines[first - 1]
      if (above && used + above.length <= room) {
        first -= 1
        used += above.length
        grew = true
      }
    }
    const shown = list.slice(first, last + 1)

    return (
      <Box flexDirection="column">
        <Box key="head" flexDirection="row" justifyContent="space-between">
          <Text color="#fbbf24" bold>
            Captures
          </Text>
          <Box flexDirection="row" gap={1}>
            <Button key="scope" plain hotkey="s" dimColor onPress={() => void setScope($, SCOPES[(SCOPES.indexOf(which) + 1) % SCOPES.length]!)}>
              {SCOPE_LABEL[which].toLowerCase()}
            </Button>
            <Text dimColor>
              · {saved} saved · {rejected} rejected
            </Text>
          </Box>
        </Box>
        <Text key="cols" dimColor>
          {fit('time', TIME_W)} {fit('type', TYPE_W)} {fit('title', titleW)} {fit('kind', KIND_W)}
        </Text>
        {list.length === 0 && <Text dimColor>No captures yet{which === 'session' ? ' this session' : ''}.</Text>}
        {shown.map(row => {
          const t = typeOf(row)
          const lines = titleLines[list.indexOf(row)]!
          const kind = fit(row.kind === 'updated' ? 'updated' : 'new', KIND_W)
          if (row.status === 'rejected') {
            return (
              <Text key={`row-${row.id}`} dimColor>
                {fit(clockTime(row.ts), TIME_W)} {fit(t.short, TYPE_W)} <Text strikethrough>{lines[0]}</Text> {fit('rejected', KIND_W)}
              </Text>
            )
          }
          const bold = row.id === focus?.id
          return (
            <Box key={`item-${row.id}`} flexDirection="column">
              <Button key={`row-${row.id}`} plain onPress={() => void update($, selected, () => row.id)}>
                <Text dimColor>{fit(clockTime(row.ts), TIME_W)}</Text> <Text color={t.color}>{fit(t.short, TYPE_W)}</Text>{' '}
                <Text bold={bold}>{fit(lines[0]!, titleW)}</Text> <Text dimColor>{kind}</Text>
              </Button>
              {lines.slice(1).map((line, i) => (
                <Text key={`more-${row.id}-${i}`}>
                  {indent}
                  <Text bold={bold}>{line}</Text>
                </Text>
              ))}
            </Box>
          )
        })}
        {focus ? (
          <Box key="detail" flexDirection="column" marginTop={1}>
            <Box flexDirection="row" justifyContent="space-between">
              <Text>
                ▸ {clockTime(focus.ts)} {typeOf(focus).long.toLowerCase()} · {focus.kind === 'updated' ? 'updated a note' : 'new note'}
              </Text>
              <Button key={`reject-${focus.id}`} hotkey="r" variant="primary" onPress={() => void reject($, focus.id)}>
                {focus.kind === 'updated' ? 'Reject update' : 'Reject'}
              </Button>
            </Box>
            {focus.note_path ? (
              <Box flexDirection="row">
                <Text>{'  '}</Text>
                {url ? <Link href={url}>{focus.note_path}</Link> : <Text>{focus.note_path}</Text>}
              </Box>
            ) : null}
            {summaryLines.map((line, i) => (
              <Text key={`summary-${i}`} dimColor>
                {'  '}
                {line}
              </Text>
            ))}
          </Box>
        ) : null}
        <Box key="footer" flexDirection="row" justifyContent="space-between">
          <Text dimColor>↑↓ select · r reject · s scope</Text>
          <Text dimColor>{footer(now)}</Text>
        </Box>
        {problemLine}
      </Box>
    )
  })
}
