export type CaptureRow = {
  id: string
  ts?: number | string
  session_id?: string
  project?: string
  type?: string
  title?: string
  summary?: string
  kind?: 'new' | 'updated'
  status?: 'active' | 'rejected'
  note_path?: string
}

export type CaptureStatus = { next_batch_min: number | null; pending_sweep: number; vault?: string }

export type CaptureScope = 'session' | 'today' | 'all'

declare module 'claude-code' {
  interface PluginState {
    'obsidian-brain': {
      rows: CaptureRow[]
      status: CaptureStatus | null
      problem: string
      scope: CaptureScope
      selected: string
    }
  }
}
