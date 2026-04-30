import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download } from 'lucide-react'

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { listAgents } from '@/api/agents'
import { downloadExport } from '@/api/exports'

// ── Types ────────────────────────────────────────────────────────────────

export interface ExportModalProps {
  open: boolean
  onClose: () => void
  /** Pre-fill the agent filter; the user can still change it. */
  agentId?: string
}

type Preset = '1h' | '6h' | '24h' | '7d' | 'custom'

const PRESETS: Array<{ value: Preset; label: string }> = [
  { value: '1h', label: 'Last 1h' },
  { value: '6h', label: 'Last 6h' },
  { value: '24h', label: 'Last 24h' },
  { value: '7d', label: 'Last 7d' },
  { value: 'custom', label: 'Custom' },
]

const ALL_AGENTS = '__all__'
const ONE_YEAR_SEC = 365 * 24 * 60 * 60

function presetToSeconds(p: Exclude<Preset, 'custom'>): number {
  switch (p) {
    case '1h':
      return 3600
    case '6h':
      return 6 * 3600
    case '24h':
      return 24 * 3600
    case '7d':
      return 7 * 24 * 3600
  }
}

// `<input type="datetime-local">` returns a string like "2026-04-30T14:23"
// in *local* time. We convert through Date so the seconds value matches what
// the user actually sees in the picker.
function localDatetimeToUnix(s: string): number | null {
  if (!s) return null
  const d = new Date(s)
  const ms = d.getTime()
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}

function unixToLocalDatetime(unix: number): string {
  const d = new Date(unix * 1000)
  // Build YYYY-MM-DDTHH:MM in local time without TZ shift surprises.
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

// ── Component ────────────────────────────────────────────────────────────

export function ExportModal({ open, onClose, agentId }: ExportModalProps): React.ReactElement {
  const agentsQ = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    enabled: open,
  })

  const [preset, setPreset] = useState<Preset>('24h')
  const [customStart, setCustomStart] = useState<string>('')
  const [customEnd, setCustomEnd] = useState<string>('')
  const [agentSel, setAgentSel] = useState<string>(agentId ?? ALL_AGENTS)
  const [includeTraces, setIncludeTraces] = useState(true)
  const [includeAudit, setIncludeAudit] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

  // Reset transient state every time the modal opens. We keep the agent
  // pre-fill aligned with the prop so navigating Agent A → Agent B doesn't
  // leak A into B's modal.
  useEffect(() => {
    if (!open) return
    setPreset('24h')
    setCustomStart('')
    setCustomEnd('')
    setAgentSel(agentId ?? ALL_AGENTS)
    setIncludeTraces(true)
    setIncludeAudit(true)
    setError(null)
    setDownloading(false)
  }, [open, agentId])

  // Resolve start/end (unix seconds) from the current picker state. For
  // presets the window ends at "now"; for custom we parse the two inputs.
  const window = useMemo<{ start: number; end: number } | null>(() => {
    if (preset !== 'custom') {
      const now = Math.floor(Date.now() / 1000)
      return { start: now - presetToSeconds(preset), end: now }
    }
    const s = localDatetimeToUnix(customStart)
    const e = localDatetimeToUnix(customEnd)
    if (s === null || e === null) return null
    return { start: s, end: e }
  }, [preset, customStart, customEnd])

  // Validation state — the Download button stays disabled until clean.
  const validation = useMemo<{ ok: boolean; reason?: string }>(() => {
    if (!includeTraces && !includeAudit) {
      return { ok: false, reason: 'Pick at least one content type.' }
    }
    if (!window) {
      return { ok: false, reason: 'Set a start and end date.' }
    }
    if (window.end <= window.start) {
      return { ok: false, reason: 'End must be after start.' }
    }
    const now = Math.floor(Date.now() / 1000)
    // Allow a small future fudge for clock skew, then reject.
    if (window.end > now + 60 || window.start > now + 60) {
      return { ok: false, reason: 'Range cannot be in the future.' }
    }
    if (now - window.start > ONE_YEAR_SEC) {
      return { ok: false, reason: 'Range cannot exceed the last year.' }
    }
    return { ok: true }
  }, [window, includeTraces, includeAudit])

  // Estimated filename mirrors what the backend will likely send back. Pure
  // cosmetic — used for the muted hint card under the form.
  const estimatedFilename = useMemo(() => {
    if (!window) return 'amaze-export.zip'
    const fmt = (u: number) => {
      const d = new Date(u * 1000)
      const pad = (n: number) => String(n).padStart(2, '0')
      return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
    }
    return `amaze-export-${fmt(window.start)}-${fmt(window.end)}.zip`
  }, [window])

  const handleDownload = async () => {
    if (!validation.ok || !window || downloading) return
    const content: string[] = []
    if (includeTraces) content.push('traces')
    if (includeAudit) content.push('audit')

    setError(null)
    setDownloading(true)
    try {
      await downloadExport({
        agent: agentSel === ALL_AGENTS ? undefined : agentSel,
        start: window.start,
        end: window.end,
        content,
      })
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.')
    } finally {
      setDownloading(false)
    }
  }

  const agents = agentsQ.data ?? []

  // When the user switches *into* custom for the first time we seed the
  // inputs from the currently-selected preset so the values aren't blank.
  const selectPreset = (p: Preset) => {
    if (p === 'custom' && preset !== 'custom') {
      const now = Math.floor(Date.now() / 1000)
      const span = presetToSeconds(preset)
      setCustomStart(unixToLocalDatetime(now - span))
      setCustomEnd(unixToLocalDatetime(now))
    }
    setPreset(p)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent
        className="max-w-md"
        style={{
          background: 'var(--panel)',
          border: '1px solid var(--line)',
          color: 'var(--text)',
        }}
      >
        <DialogHeader>
          <DialogTitle>Export</DialogTitle>
          <DialogDescription style={{ color: 'var(--muted-raw)' }}>
            Download trace summaries and audit log for a window as a ZIP.
          </DialogDescription>
        </DialogHeader>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 4 }}>
          {/* Date range — segmented preset control */}
          <div>
            <Label>Date range</Label>
            <div
              role="tablist"
              style={{
                display: 'inline-flex',
                background: 'var(--panel2)',
                border: '1px solid var(--line)',
                borderRadius: 8,
                padding: 2,
                gap: 0,
                marginTop: 6,
                flexWrap: 'wrap',
              }}
            >
              {PRESETS.map((p) => {
                const active = p.value === preset
                return (
                  <button
                    key={p.value}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => selectPreset(p.value)}
                    style={{
                      padding: '4px 12px',
                      fontSize: 12,
                      fontWeight: 700,
                      border: 'none',
                      borderRadius: 6,
                      cursor: 'pointer',
                      background: active ? 'var(--blue)' : 'transparent',
                      color: active ? '#0b1020' : 'var(--muted-raw)',
                      letterSpacing: '.04em',
                    }}
                  >
                    {p.label}
                  </button>
                )
              })}
            </div>
            {preset === 'custom' && (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr 1fr',
                  gap: 10,
                  marginTop: 10,
                }}
              >
                <div>
                  <Label htmlFor="export-start" style={{ fontSize: 11 }}>
                    From
                  </Label>
                  <Input
                    id="export-start"
                    type="datetime-local"
                    value={customStart}
                    onChange={(e) => setCustomStart(e.target.value)}
                    style={{ marginTop: 4 }}
                  />
                </div>
                <div>
                  <Label htmlFor="export-end" style={{ fontSize: 11 }}>
                    To
                  </Label>
                  <Input
                    id="export-end"
                    type="datetime-local"
                    value={customEnd}
                    onChange={(e) => setCustomEnd(e.target.value)}
                    style={{ marginTop: 4 }}
                  />
                </div>
              </div>
            )}
          </div>

          {/* Agent filter */}
          <div>
            <Label htmlFor="export-agent">Agent filter</Label>
            <Select value={agentSel} onValueChange={setAgentSel}>
              <SelectTrigger id="export-agent" style={{ marginTop: 6 }}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_AGENTS}>All agents</SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id}>
                    {a.agent_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Contents */}
          <div>
            <Label>Contents</Label>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                marginTop: 6,
              }}
            >
              <label
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                <input
                  type="checkbox"
                  checked={includeTraces}
                  onChange={(e) => setIncludeTraces(e.target.checked)}
                  style={{ accentColor: 'var(--blue)' }}
                />
                Trace summaries (traces.json)
              </label>
              <label
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                <input
                  type="checkbox"
                  checked={includeAudit}
                  onChange={(e) => setIncludeAudit(e.target.checked)}
                  style={{ accentColor: 'var(--blue)' }}
                />
                Audit log (audit.csv)
              </label>
              <label
                title="Available in S5+"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  fontSize: 13,
                  cursor: 'not-allowed',
                  color: 'var(--muted-raw)',
                  opacity: 0.6,
                }}
              >
                <input type="checkbox" disabled style={{ accentColor: 'var(--blue)' }} />
                Sequence diagrams (HTML)
                <span style={{ fontSize: 11 }}>· S5+</span>
              </label>
            </div>
          </div>

          {/* Filename hint */}
          <div
            style={{
              background: 'var(--panel2)',
              border: '1px solid var(--line)',
              borderRadius: 8,
              padding: 10,
              fontSize: 12,
              color: 'var(--muted-raw)',
            }}
          >
            Estimated:{' '}
            <strong
              style={{
                color: 'var(--text)',
                fontFamily: 'JetBrains Mono, ui-monospace, monospace',
              }}
            >
              {estimatedFilename}
            </strong>
          </div>

          {/* Validation / network error */}
          {(error || (!validation.ok && validation.reason)) && (
            <div
              role="alert"
              style={{
                background: error
                  ? 'rgba(224, 82, 82, 0.12)'
                  : 'rgba(245, 197, 24, 0.10)',
                border: error
                  ? '1px solid rgba(224, 82, 82, 0.4)'
                  : '1px solid rgba(245, 197, 24, 0.4)',
                color: error ? 'var(--red)' : 'var(--yellow)',
                borderRadius: 6,
                padding: '8px 12px',
                fontSize: 12,
              }}
            >
              {error ?? validation.reason}
            </div>
          )}
        </div>

        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 8,
            marginTop: 18,
            paddingTop: 14,
            borderTop: '1px solid var(--line)',
          }}
        >
          <Button type="button" variant="ghost" onClick={onClose} disabled={downloading}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={handleDownload}
            disabled={!validation.ok || downloading}
            style={{ background: 'var(--blue)', color: '#0b1020' }}
          >
            <Download size={13} />
            {downloading ? 'Preparing…' : 'Download ZIP'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default ExportModal
