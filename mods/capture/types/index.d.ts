export type CaptureRow = {
  id: string
  ts?: string
  type?: string
  title?: string
  kind?: 'new' | 'updated'
  status?: 'active' | 'rejected'
  note_path?: string
}

export type CaptureStatus = { next_batch_min: number | null; pending_sweep: number }

declare module 'claude-code' {
  interface PluginState {
    'obsidian-brain': { rows: CaptureRow[]; status: CaptureStatus | null; problem: string }
  }
}
