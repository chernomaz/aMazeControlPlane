import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveAgent,
  listAgents,
  rejectAgent,
  removeAgent,
  sendAgentMessage,
  type Agent,
} from '@/api/agents'
import { Button } from '@/components/ui/button'
import { useQuery as useAuditQuery } from '@tanstack/react-query'
import { apiFetch } from '@/api/client'

// ── Types ────────────────────────────────────────────────────────────────────

interface Toast { id: number; msg: string; kind: 'success' | 'error' }
type Subtab = 'dashboard' | 'policy'
type TimeRange = '1h' | '6h' | '24h' | '7d'

// ── Audit log types ──────────────────────────────────────────────────────────

interface AuditEntry {
  trace_id: string | null
  ts: string
  kind: string
  target: string
  tool: string | null
  denied: boolean
  denial_reason: string | null
  input: unknown
  output: unknown
}

function listAudit(agentId: string) {
  return apiFetch<AuditEntry[]>(`/agents/${encodeURIComponent(agentId)}/audit?limit=200`)
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function stateBadge(state: Agent['state']) {
  if (state === 'pending')
    return <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: 'rgba(242,185,75,.15)', color: 'var(--yellow)', border: '1px solid rgba(242,185,75,.3)' }}>● pending</span>
  if (state === 'approved-with-policy')
    return <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: 'rgba(31,191,117,.12)', color: 'var(--green)', border: '1px solid rgba(31,191,117,.3)' }}>● running</span>
  return <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: 'rgba(93,169,255,.1)', color: 'var(--blue)', border: '1px solid rgba(93,169,255,.3)' }}>● approved</span>
}

function modeBadge(mode?: string) {
  if (!mode) return null
  return <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: 'rgba(93,169,255,.1)', color: 'var(--blue)', border: '1px solid rgba(93,169,255,.3)' }}>{mode}</span>
}

// ── Time picker ───────────────────────────────────────────────────────────────

function TimePicker({ value, onChange }: { value: TimeRange; onChange: (t: TimeRange) => void }) {
  const opts: TimeRange[] = ['1h', '6h', '24h', '7d']
  return (
    <div style={{ display: 'inline-flex', padding: 3, borderRadius: 8, background: 'var(--panel2)', border: '1px solid var(--line)' }}>
      {opts.map(t => (
        <button
          key={t}
          onClick={() => onChange(t)}
          style={{
            padding: '4px 10px',
            fontSize: 11,
            fontWeight: 600,
            border: 'none',
            borderRadius: 6,
            cursor: 'pointer',
            background: value === t ? 'var(--blue)' : 'transparent',
            color: value === t ? '#0b1020' : 'var(--muted-raw)',
            transition: 'all .12s',
          }}
        >
          {t}
        </button>
      ))}
    </div>
  )
}

// ── KPI card ──────────────────────────────────────────────────────────────────

function KpiCard({ label, value, foot, color, onClick }: { label: string; value: string; foot: string; color: string; onClick?: () => void }) {
  return (
    <div
      onClick={onClick}
      style={{
        background: 'var(--panel)',
        border: '1px solid var(--line)',
        borderRadius: 12,
        padding: '12px 14px',
        cursor: onClick ? 'pointer' : undefined,
      }}
    >
      <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800, color, lineHeight: 1.1, marginBottom: 4 }}>{value}</div>
      <div style={{ fontSize: 11, color: 'var(--muted-raw)' }}>{foot}</div>
    </div>
  )
}

// ── Line chart with axes (SVG) ────────────────────────────────────────────────

function LineChart({ color, points, xLabels, yUnit = '' }: {
  color: string; points: number[]; xLabels: string[]; yUnit?: string
}) {
  const PAD = { top: 8, right: 8, bottom: 28, left: 36 }
  const VW = 800, VH = 130
  const plotW = VW - PAD.left - PAD.right
  const plotH = VH - PAD.top - PAD.bottom
  const max = Math.max(...points, 1)
  const n = points.length
  const cx = (i: number) => PAD.left + (i / Math.max(n - 1, 1)) * plotW
  const cy = (v: number) => PAD.top + plotH - (v / max) * plotH
  const linePath = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${cx(i).toFixed(1)},${cy(p).toFixed(1)}`).join(' ')
  const areaPath = linePath + ` L${cx(n - 1).toFixed(1)},${(PAD.top + plotH).toFixed(1)} L${PAD.left},${(PAD.top + plotH).toFixed(1)} Z`

  // 4 Y ticks: 0, 33%, 66%, 100%
  const yTicks = [0, Math.round(max / 3), Math.round((max * 2) / 3), max]
  // X ticks: show ~6 evenly spaced labels
  const xStep = Math.max(1, Math.floor(n / 6))

  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} style={{ width: '100%', height: 130, display: 'block' }}>
      <defs>
        <linearGradient id={`lgrad-${color}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* Y grid + labels */}
      {yTicks.map((v, i) => {
        const y = cy(v)
        return (
          <g key={i}>
            <line x1={PAD.left} y1={y} x2={VW - PAD.right} y2={y} stroke="var(--line)" strokeOpacity={0.5} strokeDasharray="3 3" />
            <text x={PAD.left - 4} y={y + 4} textAnchor="end" fontSize={9} fill="var(--muted-raw)" fontFamily="JetBrains Mono, monospace">
              {v}{yUnit}
            </text>
          </g>
        )
      })}
      {/* Area + line */}
      <path d={areaPath} fill={`url(#lgrad-${color})`} />
      <path d={linePath} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />
      {/* X labels */}
      {xLabels.map((label, i) => {
        if (!label || i % xStep !== 0) return null
        return (
          <text key={i} x={cx(i)} y={VH - 6} textAnchor="middle" fontSize={9} fill="var(--muted-raw)" fontFamily="JetBrains Mono, monospace">
            {label}
          </text>
        )
      })}
      {/* X axis line */}
      <line x1={PAD.left} y1={PAD.top + plotH} x2={VW - PAD.right} y2={PAD.top + plotH} stroke="var(--line)" />
    </svg>
  )
}

// ── Horizontal bar chart (per-category counts) ────────────────────────────────

function HBarChart({ bars, emptyMsg = 'No data' }: {
  bars: { label: string; value: number; color: string }[]
  emptyMsg?: string
}) {
  const max = Math.max(...bars.map(b => b.value), 1)
  if (bars.length === 0) return <div style={{ color: 'var(--muted-raw)', fontSize: 11, padding: '8px 0' }}>{emptyMsg}</div>
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {bars.map((b, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <div style={{ width: 82, fontSize: 10, color: 'var(--muted-raw)', textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 0 }}
            title={b.label}>{b.label}</div>
          <div style={{ flex: 1, background: 'var(--panel2)', borderRadius: 4, overflow: 'hidden', height: 14, border: '1px solid var(--line)' }}>
            <div style={{ height: '100%', width: `${(b.value / max) * 100}%`, background: b.color, borderRadius: 3, minWidth: b.value > 0 ? 4 : 0, transition: 'width .3s' }} />
          </div>
          <div style={{ width: 24, fontSize: 10, fontWeight: 700, color: 'var(--text)', textAlign: 'right', flexShrink: 0 }}>{b.value}</div>
        </div>
      ))}
    </div>
  )
}

// ── Histogram (vertical bars with bucket ranges) ──────────────────────────────

function HistoChart({ buckets, color, total }: {
  buckets: { label: string; count: number }[]
  color: string
  total: number
}) {
  const max = Math.max(...buckets.map(b => b.count), 1)
  const plotH = 80
  if (total === 0) return <div style={{ color: 'var(--muted-raw)', fontSize: 11, padding: '8px 0' }}>No data</div>
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Bars */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: plotH, position: 'relative' }}>
        {/* Y-axis ticks */}
        {[0, 50, 100].map(pct => {
          const y = plotH - (pct / 100) * plotH
          return (
            <div key={pct} style={{ position: 'absolute', left: 0, right: 0, top: y, borderTop: '1px dashed var(--line)', display: 'flex', alignItems: 'center' }}>
              <span style={{ fontSize: 8, color: 'var(--muted-raw)', marginLeft: -2, lineHeight: 1, background: 'var(--panel)', paddingRight: 2 }}>{pct}%</span>
            </div>
          )
        })}
        {buckets.map((b, i) => {
          const pct = total > 0 ? (b.count / total) * 100 : 0
          const barH = (b.count / max) * (plotH - 4)
          return (
            <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', height: '100%', justifyContent: 'flex-end', zIndex: 1 }}>
              {b.count > 0 && (
                <span style={{ fontSize: 8, color: 'var(--muted-raw)', marginBottom: 2, lineHeight: 1 }}>{Math.round(pct)}%</span>
              )}
              <div style={{ width: '80%', background: color, borderRadius: '3px 3px 0 0', height: barH, minHeight: b.count > 0 ? 2 : 0, opacity: 0.85 }} />
            </div>
          )
        })}
      </div>
      {/* X labels */}
      <div style={{ display: 'flex', gap: 4 }}>
        {buckets.map((b, i) => (
          <div key={i} style={{ flex: 1, fontSize: 8, color: 'var(--muted-raw)', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {b.label}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Pending detail ────────────────────────────────────────────────────────────

function PendingDetail({ agent, onApprove, onReject, busy }: {
  agent: Agent; onApprove: () => void; onReject: () => void; busy: boolean
}) {
  return (
    <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 14, padding: 24 }}>
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 20, fontWeight: 800 }}>{agent.agent_id}</div>
        <div style={{ marginTop: 8 }}>{stateBadge(agent.state)}</div>
      </div>
      <div style={{ color: 'var(--muted-raw)', fontSize: 13, marginBottom: 8 }}>This agent is awaiting approval</div>
      <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', rowGap: 8, columnGap: 12, fontSize: 13, marginBottom: 24 }}>
        <span style={{ color: 'var(--muted-raw)' }}>Endpoint</span>
        <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 12 }}>{agent.endpoint ?? '—'}</span>
        <span style={{ color: 'var(--muted-raw)' }}>State</span>
        <span>pending</span>
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <Button variant="outline" size="sm" disabled={busy} onClick={onReject} style={{ borderColor: 'var(--red)', color: 'var(--red)' }}>
          Reject
        </Button>
        <Button size="sm" disabled={busy} onClick={onApprove} style={{ background: 'var(--green)', color: '#0b1020' }}>
          {busy ? '…' : 'Approve'}
        </Button>
      </div>
    </div>
  )
}

// ── Approved detail ───────────────────────────────────────────────────────────

function ApprovedDetail({ agent, onRemove, removeDisabled, onToast }: {
  agent: Agent
  onRemove: () => void
  removeDisabled: boolean
  onToast: (msg: string, kind: 'success' | 'error') => void
}) {
  const navigate = useNavigate()
  const [subtab, setSubtab] = useState<Subtab>('dashboard')
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
  const [msgInput, setMsgInput] = useState('')
  const [sending, setSending] = useState(false)
  const [lastReply, setLastReply] = useState<string | null>(null)
  const [chartTab, setChartTab] = useState<'calls' | 'latency'>('calls')

  const { data: auditEntries = [] } = useAuditQuery({
    queryKey: ['audit', agent.agent_id],
    queryFn: () => listAudit(agent.agent_id),
    refetchInterval: 10_000,
  })

  const handleSend = async () => {
    if (!msgInput.trim() || sending) return
    setSending(true)
    try {
      const res = await sendAgentMessage(agent.agent_id, msgInput.trim())
      const reply =
        typeof res.response === 'string'
          ? res.response
          : (res.response as Record<string, unknown>)?.reply
          ? String((res.response as Record<string, unknown>).reply)
          : JSON.stringify(res.response)
      setLastReply(reply)
      setMsgInput('')
    } catch (err) {
      onToast(err instanceof Error ? err.message : 'Send failed', 'error')
    } finally {
      setSending(false)
    }
  }

  const tabStyle = (t: Subtab): React.CSSProperties => ({
    padding: '10px 18px',
    fontSize: 13,
    fontWeight: 600,
    cursor: 'pointer',
    color: subtab === t ? 'var(--blue)' : 'var(--muted-raw)',
    marginBottom: -1,
    background: 'transparent',
    border: 'none',
    borderBottom: subtab === t ? '2px solid var(--blue)' : '2px solid transparent',
    outline: 'none',
    display: 'flex',
    alignItems: 'center',
    gap: 6,
  })

  const chartTabStyle = (t: 'calls' | 'latency'): React.CSSProperties => ({
    padding: '8px 16px',
    fontSize: 12,
    fontWeight: 700,
    cursor: 'pointer',
    color: chartTab === t ? 'var(--blue)' : 'var(--muted-raw)',
    marginBottom: -1,
    background: 'transparent',
    border: 'none',
    borderBottom: chartTab === t ? '2px solid var(--blue)' : '2px solid transparent',
    outline: 'none',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.06em',
  })

  // ── Derive stats from audit log ───────────────────────────────────────────
  const toolCalls = auditEntries.filter(e => e.kind === 'mcp').length
  const a2aCalls = auditEntries.filter(e => e.kind === 'a2a').length
  const denials = auditEntries.filter(e => e.denied).length

  // Time-bucket audit entries for the line chart
  const RANGE_SEC: Record<TimeRange, number> = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800 }
  const NUM_BUCKETS: Record<TimeRange, number> = { '1h': 12, '6h': 12, '24h': 24, '7d': 14 }
  const rangeSeconds = RANGE_SEC[timeRange]
  const numBuckets = NUM_BUCKETS[timeRange]
  const bucketSec = rangeSeconds / numBuckets
  const nowSec = Date.now() / 1000
  const startSec = nowSec - rangeSeconds

  const callBuckets = Array(numBuckets).fill(0)
  auditEntries.forEach(e => {
    const ts = Number(e.ts)
    if (ts < startSec || ts > nowSec) return
    const idx = Math.min(Math.floor((ts - startSec) / bucketSec), numBuckets - 1)
    callBuckets[idx]++
  })

  const xLabels = Array.from({ length: numBuckets }, (_, i) => {
    const t = startSec + i * bucketSec
    const d = new Date(t * 1000)
    if (timeRange === '7d') return d.toLocaleDateString('en', { weekday: 'short' })
    return d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false })
  })

  // Token histogram — parse from LLM output JSON
  const TOKEN_BUCKETS = [
    { label: '<100', min: 0, max: 100 },
    { label: '100–300', min: 100, max: 300 },
    { label: '300–1k', min: 300, max: 1000 },
    { label: '1k–3k', min: 1000, max: 3000 },
    { label: '3k+', min: 3000, max: Infinity },
  ]
  const llmEntries = auditEntries.filter(e => e.kind === 'llm')
  const tokenCounts = llmEntries.map(e => {
    try {
      const out = typeof e.output === 'string' ? JSON.parse(e.output) : (e.output as Record<string, unknown>)
      return (out as Record<string, Record<string, number>>)?.usage?.total_tokens ?? 0
    } catch { return 0 }
  }).filter(t => t > 0)
  const tokenHistoBuckets = TOKEN_BUCKETS.map(b => ({
    label: b.label,
    count: tokenCounts.filter(t => t >= b.min && t < b.max).length,
  }))

  // Tool calls per tool name
  const toolCountMap: Record<string, number> = {}
  auditEntries.filter(e => e.kind === 'mcp' && e.tool).forEach(e => {
    toolCountMap[e.tool!] = (toolCountMap[e.tool!] || 0) + 1
  })
  const toolBars = Object.entries(toolCountMap)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, color: '#43d1c6' }))

  // A2A calls per target
  const a2aMap: Record<string, number> = {}
  auditEntries.filter(e => e.kind === 'a2a').forEach(e => {
    a2aMap[e.target] = (a2aMap[e.target] || 0) + 1
  })
  const a2aBars = Object.entries(a2aMap)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, color: '#a97cff' }))

  // Alerts per denial reason
  const denialMap: Record<string, number> = {}
  auditEntries.filter(e => e.denied && e.denial_reason).forEach(e => {
    denialMap[e.denial_reason!] = (denialMap[e.denial_reason!] || 0) + 1
  })
  const denialBars = Object.entries(denialMap)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, color: '#e05252' }))

  return (
    <div>
      {/* ── Header ── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, gap: 16, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 20, fontWeight: 800 }}>{agent.agent_id}</span>
          {stateBadge(agent.state)}
          {modeBadge(agent.policy_summary?.mode)}
          {agent.endpoint && (
            <span style={{ fontSize: 12, color: 'var(--muted-raw)', fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}>
              {agent.endpoint}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <TimePicker value={timeRange} onChange={setTimeRange} />
          <Button variant="outline" size="sm" disabled={removeDisabled} onClick={onRemove}
            style={{ borderColor: 'var(--red)', color: 'var(--red)' }}>
            Remove
          </Button>
        </div>
      </div>

      {/* ── Sub-tabs ── */}
      <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--line)', marginBottom: 16 }}>
        <button style={tabStyle('dashboard')} onClick={() => setSubtab('dashboard')}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/>
            <rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>
          </svg>
          Dashboard
        </button>
        <button style={tabStyle('policy')} onClick={() => setSubtab('policy')}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
          Policy
          {agent.state === 'approved-no-policy' && (
            <span style={{ fontSize: 9, padding: '1px 5px', borderRadius: 4, background: 'rgba(255,188,0,.15)', color: 'var(--yellow)', border: '1px solid rgba(255,188,0,.3)' }}>
              ⚠ none
            </span>
          )}
        </button>
      </div>

      {/* ── Dashboard ── */}
      {subtab === 'dashboard' && (
        <div>
          {/* 1. Chat input */}
          <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: '10px 12px', display: 'flex', gap: 8, alignItems: 'center', marginBottom: 14 }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9fb0d1" strokeWidth="2" style={{ flexShrink: 0 }}>
              <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
            </svg>
            <input
              type="text"
              value={msgInput}
              onChange={e => setMsgInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSend()}
              placeholder="Send a message to this agent…"
              style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', fontSize: 13, color: 'var(--text)', padding: '6px 0' }}
            />
            <Button size="sm" onClick={handleSend} disabled={sending || !msgInput.trim()}>
              {sending ? '…' : 'Send'}
              {!sending && (
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ marginLeft: 4 }}>
                  <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
                </svg>
              )}
            </Button>
          </div>

          {lastReply && (
            <div style={{ background: 'var(--panel2)', border: '1px solid var(--line)', borderRadius: 10, padding: '10px 12px', fontSize: 13, marginBottom: 14, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Agent reply</div>
              {lastReply}
            </div>
          )}

          {/* 2. KPI cards */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
            <KpiCard label="Avg Latency" value="—" foot="no data yet" color="var(--cyan)" />
            <KpiCard label="Tool Calls" value={String(toolCalls)} foot={`in last ${timeRange}`} color="var(--green)" />
            <KpiCard label="Tokens Used" value="—" foot="budget: —" color="var(--yellow)" />
            <KpiCard
              label="Alerts"
              value={String(denials)}
              foot="↑ click to drill →"
              color="var(--red)"
              onClick={() => navigate(`/alerts?agent=${encodeURIComponent(agent.agent_id)}`)}
            />
          </div>

          {/* 3. Line chart — Calls Over Time */}
          <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: '14px 16px', marginBottom: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
              <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--line)', marginBottom: -1 }}>
                <button style={chartTabStyle('calls')} onClick={() => setChartTab('calls')}>Calls Over Time</button>
                <button style={chartTabStyle('latency')} onClick={() => setChartTab('latency')}>Calls by Kind</button>
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted-raw)' }}>last {timeRange}</div>
            </div>
            {chartTab === 'calls' ? (
              <LineChart color="#5da9ff" points={callBuckets} xLabels={xLabels} yUnit="" />
            ) : (
              /* Calls by Kind — stacked indicator as horizontal bars */
              <div style={{ padding: '12px 0' }}>
                <HBarChart bars={[
                  { label: 'llm', value: auditEntries.filter(e => e.kind === 'llm').length, color: '#5da9ff' },
                  { label: 'mcp', value: toolCalls, color: '#43d1c6' },
                  { label: 'a2a', value: a2aCalls, color: '#a97cff' },
                  { label: 'unknown', value: auditEntries.filter(e => e.kind === 'unknown').length, color: '#9fb0d1' },
                ].filter(b => b.value > 0)} emptyMsg="No calls in range" />
              </div>
            )}
          </div>

          {/* 4. Bucket charts */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
            {/* Tokens / Call — histogram */}
            <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 12 }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>Tokens / Call</div>
              <HistoChart buckets={tokenHistoBuckets} color="#f2b94b" total={tokenCounts.length} />
            </div>
            {/* Tool Calls — per tool */}
            <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 12 }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>Tool Calls</div>
              <HBarChart bars={toolBars.slice(0, 5)} emptyMsg="No tool calls" />
            </div>
            {/* Agent → Agent — per target */}
            <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 12 }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>Agent → Agent</div>
              <HBarChart bars={a2aBars.slice(0, 5)} emptyMsg="No A2A calls" />
            </div>
            {/* Alerts — per denial reason */}
            <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 12, cursor: 'pointer' }}
              onClick={() => navigate(`/alerts?agent=${encodeURIComponent(agent.agent_id)}`)}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>
                Alerts <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0, color: 'var(--muted-raw)' }}>— click →</span>
              </div>
              <HBarChart bars={denialBars.slice(0, 5)} emptyMsg="No violations" />
            </div>
          </div>

          {/* 5. Recent traces */}
          <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--muted-raw)', textTransform: 'uppercase', letterSpacing: '0.07em' }}>Recent Traces</div>
              <Button variant="ghost" size="sm" onClick={() => navigate(`/traces?agent=${encodeURIComponent(agent.agent_id)}`)}>
                View all →
              </Button>
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ color: 'var(--muted-raw)', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  <th style={{ textAlign: 'left', padding: '4px 8px 8px 0' }}>Trace ID</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px 8px' }}>Time</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px 8px' }}>Kind</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px 8px' }}>Target</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px 8px' }}>Status</th>
                </tr>
              </thead>
              <tbody>
                {auditEntries.length === 0 ? (
                  <tr><td colSpan={5} style={{ color: 'var(--muted-raw)', padding: '12px 0', textAlign: 'center' }}>No traces yet</td></tr>
                ) : (
                  auditEntries.slice(0, 5).map((e, i) => (
                    <tr
                      key={i}
                      onClick={() => e.trace_id && navigate(`/traces/${encodeURIComponent(e.trace_id)}`)}
                      style={{ cursor: e.trace_id ? 'pointer' : undefined, borderTop: '1px solid var(--line)' }}
                    >
                      <td style={{ padding: '8px 8px 8px 0', fontFamily: 'JetBrains Mono, ui-monospace, monospace', color: e.denied ? 'var(--red)' : 'var(--blue)' }}>
                        {e.trace_id ? e.trace_id.slice(0, 8) : '—'}
                      </td>
                      <td style={{ padding: '8px', color: 'var(--muted-raw)' }}>{new Date(Number(e.ts) * 1000).toLocaleTimeString()}</td>
                      <td style={{ padding: '8px', fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}>{e.kind}</td>
                      <td style={{ padding: '8px', fontFamily: 'JetBrains Mono, ui-monospace, monospace', maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {e.tool ?? e.target}
                      </td>
                      <td style={{ padding: '8px' }}>
                        {e.denied
                          ? <span style={{ fontSize: 10, padding: '2px 7px', borderRadius: 999, background: 'rgba(224,82,82,.15)', color: 'var(--red)', border: '1px solid rgba(224,82,82,.3)' }}>violation</span>
                          : <span style={{ fontSize: 10, padding: '2px 7px', borderRadius: 999, background: 'rgba(31,191,117,.12)', color: 'var(--green)', border: '1px solid rgba(31,191,117,.3)' }}>passed</span>
                        }
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Policy ── */}
      {subtab === 'policy' && (
        <div>
          {agent.policy_summary ? (
            <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: 16, marginBottom: 12 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', rowGap: 10, columnGap: 12, fontSize: 13 }}>
                <span style={{ color: 'var(--muted-raw)' }}>Mode</span>
                <span>{modeBadge(agent.policy_summary.mode)}</span>
                {agent.policy_summary.allowed_tools && agent.policy_summary.allowed_tools.length > 0 && (<>
                  <span style={{ color: 'var(--muted-raw)' }}>Tools</span>
                  <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 12 }}>
                    {agent.policy_summary.allowed_tools.join(', ')}
                  </span>
                </>)}
                {agent.policy_summary.allowed_agents && agent.policy_summary.allowed_agents.length > 0 && (<>
                  <span style={{ color: 'var(--muted-raw)' }}>Agents</span>
                  <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 12 }}>
                    {agent.policy_summary.allowed_agents.join(', ')}
                  </span>
                </>)}
              </div>
            </div>
          ) : (
            <div style={{ background: 'rgba(255,188,0,.06)', border: '1px solid rgba(255,188,0,.3)', borderRadius: 10, padding: '10px 14px', color: 'var(--yellow)', fontSize: 13, marginBottom: 12 }}>
              No policy defined — this agent's requests will be denied by the proxy.
            </div>
          )}
          <Button size="sm" onClick={() => navigate(`/agents/${encodeURIComponent(agent.agent_id)}/policy`)}>
            {agent.policy_summary ? 'Edit policy' : 'Create policy'}
          </Button>
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Agents() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [toasts, setToasts] = useState<Toast[]>([])

  const selectedId = searchParams.get('id')

  const pushToast = (msg: string, kind: 'success' | 'error') => {
    const id = Date.now() + Math.random()
    setToasts(t => [...t, { id, msg, kind }])
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3500)
  }

  const { data, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 5_000,
  })

  const agents: Agent[] = data ?? []
  const selected = agents.find(a => a.agent_id === selectedId) ?? null

  const approve = useMutation({
    mutationFn: approveAgent,
    onSuccess: (_r, id) => { qc.invalidateQueries({ queryKey: ['agents'] }); pushToast(`${id} approved`, 'success') },
    onError: (err: unknown, id) => pushToast(`Approve ${id}: ${err instanceof Error ? err.message : 'failed'}`, 'error'),
  })

  const reject = useMutation({
    mutationFn: rejectAgent,
    onSuccess: (_r, id) => { qc.invalidateQueries({ queryKey: ['agents'] }); pushToast(`${id} rejected`, 'success') },
    onError: (err: unknown, id) => pushToast(`Reject ${id}: ${err instanceof Error ? err.message : 'failed'}`, 'error'),
  })

  const remove = useMutation({
    mutationFn: removeAgent,
    onSuccess: (_r, id) => {
      qc.invalidateQueries({ queryKey: ['agents'] })
      pushToast(`${id} removed`, 'success')
      setSearchParams({})
    },
    onError: (err: unknown, id) => pushToast(`Remove ${id}: ${err instanceof Error ? err.message : 'failed'}`, 'error'),
  })

  const isBusy = (id: string) =>
    (approve.isPending && approve.variables === id) ||
    (reject.isPending && reject.variables === id) ||
    (remove.isPending && remove.variables === id)

  return (
    <div>
      {/* Page header (only when no agent selected) */}
      {!selected && !isLoading && (
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 22, fontWeight: 800, marginBottom: 4 }}>Agents</div>
          <div style={{ fontSize: 13, color: 'var(--muted-raw)' }}>Select an agent from the sidebar to view its dashboard.</div>
        </div>
      )}

      {isLoading && !selected && (
        <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>Loading…</div>
      )}

      {selected && selected.state === 'pending' && (
        <PendingDetail
          agent={selected}
          onApprove={() => approve.mutate(selected.agent_id)}
          onReject={() => reject.mutate(selected.agent_id)}
          busy={isBusy(selected.agent_id)}
        />
      )}

      {selected && selected.state !== 'pending' && (
        <ApprovedDetail
          agent={selected}
          onRemove={() => remove.mutate(selected.agent_id)}
          removeDisabled={isBusy(selected.agent_id)}
          onToast={pushToast}
        />
      )}

      {/* Toast stack */}
      <div style={{ position: 'fixed', bottom: 24, right: 24, display: 'flex', flexDirection: 'column', gap: 8, zIndex: 100 }}>
        {toasts.map(t => (
          <div key={t.id} style={{
            padding: '10px 14px', borderRadius: 10, fontSize: 13, fontWeight: 500,
            background: t.kind === 'success' ? 'rgba(31,191,117,.18)' : 'rgba(224,82,82,.18)',
            border: `1px solid ${t.kind === 'success' ? 'var(--green)' : 'var(--red)'}`,
            color: t.kind === 'success' ? '#7be3a8' : '#ffb3b3',
            minWidth: 220, boxShadow: '0 8px 24px rgba(0,0,0,.4)',
          }}>
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  )
}
