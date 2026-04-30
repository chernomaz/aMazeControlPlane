import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Send } from 'lucide-react'

import {
  listAgents,
  sendAgentMessage,
  type Agent,
  type SendMessageDenialAlert,
} from '@/api/agents'
import { listTraces, type TraceListItem } from '@/api/traces'
import {
  getAgentStats,
  type CategoryBucket,
  type PolicyHealth,
  type StatsRange,
} from '@/api/stats'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { LineChart } from '@/components/LineChart'
import { DonutChart } from '@/components/DonutChart'

// ── Helpers ────────────────────────────────────────────────────────────

const RANGES: Array<{ value: StatsRange; label: string }> = [
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
]

function formatLatency(ms: number | null): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function relativeTime(ts: number | string | undefined | null): string {
  if (ts === undefined || ts === null) return '—'
  const ms =
    typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(String(ts))
  if (!Number.isFinite(ms)) return '—'
  const diff = Date.now() - ms
  if (diff < 0) return 'just now'
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

function policyHealthBadge(h: PolicyHealth | undefined) {
  switch (h) {
    case 'ok':
      return <Badge variant="success">healthy</Badge>
    case 'warn':
      return <Badge variant="warning">warn</Badge>
    case 'fail':
      return <Badge variant="destructive">fail</Badge>
    default:
      return <Badge variant="default">—</Badge>
  }
}

function stateBadge(state: Agent['state'] | undefined) {
  if (state === 'pending') return <Badge variant="warning">⏳ pending</Badge>
  if (state === 'approved-no-policy')
    return <Badge variant="default">approved · no policy</Badge>
  if (state === 'approved-with-policy')
    return <Badge variant="success">● approved</Badge>
  return null
}

// Coerce `count` to a positive number so a backend that omits or zeroes a
// bucket doesn't crash the donut renderer.
function safeBuckets(items: CategoryBucket[] | undefined): CategoryBucket[] {
  return (items ?? []).filter((b) => Number.isFinite(b.count) && b.count > 0)
}

// ── Sub-components ─────────────────────────────────────────────────────

function KpiCard({
  label,
  value,
  unit,
  emphasis,
  foot,
}: {
  label: string
  value: React.ReactNode
  unit?: string
  emphasis?: 'green' | 'cyan' | 'yellow' | 'red' | 'blue'
  foot?: React.ReactNode
}) {
  const color =
    emphasis === 'red'
      ? 'var(--red)'
      : emphasis === 'green'
      ? 'var(--green)'
      : emphasis === 'yellow'
      ? 'var(--yellow)'
      : emphasis === 'cyan'
      ? 'var(--cyan)'
      : 'var(--blue)'
  return (
    <div
      style={{
        background: 'var(--panel)',
        border: '1px solid var(--line)',
        borderRadius: 12,
        padding: '12px 14px',
        minWidth: 0,
      }}
    >
      <div
        style={{
          color: 'var(--muted-raw)',
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: '.08em',
          textTransform: 'uppercase',
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 800,
          color,
          marginTop: 4,
          lineHeight: 1.1,
          letterSpacing: '-0.5px',
        }}
      >
        {value}
        {unit ? (
          <span
            style={{
              fontSize: 12,
              fontWeight: 500,
              color: 'var(--muted-raw)',
              marginLeft: 4,
            }}
          >
            {unit}
          </span>
        ) : null}
      </div>
      {foot ? (
        <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 4 }}>
          {foot}
        </div>
      ) : null}
    </div>
  )
}

function RangePicker({
  value,
  onChange,
}: {
  value: StatsRange
  onChange: (v: StatsRange) => void
}) {
  return (
    <div
      role="tablist"
      style={{
        display: 'inline-flex',
        background: 'var(--panel2)',
        border: '1px solid var(--line)',
        borderRadius: 8,
        padding: 2,
        gap: 0,
      }}
    >
      {RANGES.map((r) => {
        const active = r.value === value
        return (
          <button
            key={r.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(r.value)}
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
            {r.label}
          </button>
        )
      })}
    </div>
  )
}

interface ChatLogEntry {
  id: number
  prompt: string
  reply: string
  denial: SendMessageDenialAlert | null
  trace_id: string | null
  ts: number
}

// Best-effort plain-text extraction from the agent's /chat reply, which the
// orchestrator forwards verbatim. The SDK puts the answer under `reply`,
// but we tolerate `text` / `error` / unknown shapes so the UI never blanks.
function extractReply(response: unknown): string {
  if (typeof response === 'string') return response
  if (response && typeof response === 'object') {
    const r = response as Record<string, unknown>
    if (typeof r.reply === 'string') return r.reply
    if (typeof r.text === 'string') return r.text
    if (typeof r.error === 'string') return r.error
    try {
      return JSON.stringify(response)
    } catch {
      return '[unreadable response]'
    }
  }
  return String(response ?? '')
}

// ── Page ───────────────────────────────────────────────────────────────

export default function AgentDashboard() {
  const { agentId = '' } = useParams<{ agentId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [range, setRange] = useState<StatsRange>('24h')
  const [chartTab, setChartTab] = useState<'calls' | 'latency'>('calls')
  const [composerOpen, setComposerOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [chatLog, setChatLog] = useState<ChatLogEntry[]>([])
  const [chatError, setChatError] = useState<string | null>(null)

  // Agent metadata — small list, refreshed lazily; powers the header badges.
  const agentsQ = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 30_000,
  })
  const agent = (agentsQ.data ?? []).find((a) => a.agent_id === agentId)

  // Dashboard payload — refetched every 5s so the page reflects the latest
  // proxy activity without operator interaction.
  const statsQ = useQuery({
    queryKey: ['stats', agentId, range],
    queryFn: () => getAgentStats(agentId, range),
    enabled: !!agentId,
    refetchInterval: 5_000,
  })

  // Recent traces — same key shape as the Traces tab so a send-message
  // invalidation here updates that tab too.
  const tracesQ = useQuery({
    queryKey: ['traces', { agent: agentId }],
    queryFn: () => listTraces({ agent: agentId, limit: 10 }),
    enabled: !!agentId,
    refetchInterval: 5_000,
  })

  const sendMut = useMutation({
    mutationFn: (prompt: string) => sendAgentMessage(agentId, prompt),
    onSuccess: (res, prompt) => {
      setChatLog((log) => [
        ...log,
        {
          id: Date.now() + Math.random(),
          prompt,
          reply: extractReply(res.response),
          denial: res.denial,
          trace_id: res.trace_id,
          ts: Date.now(),
        },
      ])
      setDraft('')
      setChatError(null)
      qc.invalidateQueries({ queryKey: ['stats', agentId, range] })
      qc.invalidateQueries({ queryKey: ['traces', { agent: agentId }] })
    },
    onError: (err: unknown) => {
      setChatError(err instanceof Error ? err.message : 'Send failed')
    },
  })

  const onSend = () => {
    const text = draft.trim()
    if (!text || sendMut.isPending) return
    sendMut.mutate(text)
  }

  const stats = statsQ.data
  const kpi = stats?.kpi
  const lineData =
    chartTab === 'calls'
      ? stats?.calls_over_time ?? []
      : stats?.latency_per_call ?? []

  const tokenSegments = safeBuckets(stats?.tokens)
  const toolSegments = safeBuckets(stats?.tools)
  const a2aSegments = safeBuckets(stats?.a2a)
  const alertSegments = safeBuckets(stats?.alerts)

  // Drill-through to filtered tabs. The Traces tab's filter param is
  // `agent`; `tool` is reserved for a future enhancement and is forwarded
  // verbatim in the URL so a no-op consumer simply ignores it.
  const drillTraces = (label?: string) => {
    const qs = new URLSearchParams({ agent: agentId })
    if (label) qs.set('tool', label)
    navigate(`/traces?${qs.toString()}`)
  }
  const drillAlerts = (label?: string) => {
    const qs = new URLSearchParams({ agent: agentId })
    if (label) qs.set('reason', label)
    navigate(`/alerts?${qs.toString()}`)
  }

  const traces: TraceListItem[] = tracesQ.data?.traces ?? []

  return (
    <div>
      {/* ── Header bar ─────────────────────────────────────────────── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
          marginBottom: 20,
          flexWrap: 'wrap',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate('/agents')}
            style={{ paddingLeft: 6 }}
          >
            <ArrowLeft size={14} />
            Agents
          </Button>
          <div style={{ minWidth: 0 }}>
            <h1
              style={{
                fontSize: 22,
                fontWeight: 800,
                letterSpacing: '-0.3px',
                fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                margin: 0,
              }}
            >
              {agentId}
            </h1>
            <div
              style={{
                marginTop: 4,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
              }}
            >
              {stateBadge(agent?.state)}
              {agent?.policy_summary?.mode ? (
                <Badge variant="default">{agent.policy_summary.mode}</Badge>
              ) : null}
            </div>
          </div>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <RangePicker value={range} onChange={setRange} />
          <Button
            size="sm"
            onClick={() => setComposerOpen((v) => !v)}
            style={{ background: 'var(--blue)', color: '#0b1020' }}
          >
            <Send size={12} />
            {composerOpen ? 'Hide composer' : 'Send Message'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              navigate(`/agents/${encodeURIComponent(agentId)}/policy`)
            }
          >
            Edit Policy
          </Button>
        </div>
      </div>

      {statsQ.isError && (
        <div
          style={{
            background: 'rgba(224,82,82,.08)',
            border: '1px solid rgba(224,82,82,.3)',
            borderRadius: 10,
            padding: 14,
            color: 'var(--red)',
            marginBottom: 16,
            fontSize: 13,
          }}
        >
          Failed to load stats:{' '}
          {statsQ.error instanceof Error ? statsQ.error.message : 'unknown error'}
        </div>
      )}

      {/* ── KPI row ────────────────────────────────────────────────── */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
          gap: 12,
          marginBottom: 16,
        }}
      >
        <KpiCard
          label="Calls"
          value={(kpi?.calls ?? 0).toLocaleString()}
          emphasis="blue"
        />
        <KpiCard
          label="Unique Tools"
          value={(kpi?.unique_tools ?? 0).toLocaleString()}
          emphasis="green"
        />
        <KpiCard
          label="Avg Latency"
          value={formatLatency(kpi?.avg_latency_ms ?? null)}
          emphasis="cyan"
        />
        <KpiCard
          label="Critical Alerts"
          value={(kpi?.critical_alerts ?? 0).toLocaleString()}
          emphasis={(kpi?.critical_alerts ?? 0) > 0 ? 'red' : 'blue'}
        />
        <div
          style={{
            background: 'var(--panel)',
            border: '1px solid var(--line)',
            borderRadius: 12,
            padding: '12px 14px',
            minWidth: 0,
          }}
        >
          <div
            style={{
              color: 'var(--muted-raw)',
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: '.08em',
              textTransform: 'uppercase',
            }}
          >
            Policy Health
          </div>
          <div style={{ marginTop: 8 }}>{policyHealthBadge(kpi?.policy_health)}</div>
        </div>
      </div>

      {/* ── Charts row: line chart (2fr) + 2x2 donuts (1fr) ───────── */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)',
          gap: 12,
          marginBottom: 16,
        }}
        className="dash-charts-row"
      >
        {/* Line chart card with internal tab toggle */}
        <div
          style={{
            background: 'var(--panel)',
            border: '1px solid var(--line)',
            borderRadius: 12,
            padding: 14,
            minWidth: 0,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 10,
              gap: 12,
              flexWrap: 'wrap',
            }}
          >
            <div
              style={{
                display: 'flex',
                gap: 4,
                borderBottom: '1px solid var(--line)',
                marginBottom: -1,
              }}
            >
              {(['calls', 'latency'] as const).map((tab) => {
                const active = chartTab === tab
                const label = tab === 'calls' ? 'Calls Over Time' : 'Latency per Call'
                return (
                  <button
                    key={tab}
                    onClick={() => setChartTab(tab)}
                    style={{
                      padding: '8px 14px',
                      fontSize: 12,
                      fontWeight: 700,
                      cursor: 'pointer',
                      background: 'transparent',
                      border: 'none',
                      color: active ? 'var(--blue)' : 'var(--muted-raw)',
                      borderBottom: `2px solid ${active ? 'var(--blue)' : 'transparent'}`,
                      textTransform: 'uppercase',
                      letterSpacing: '.06em',
                    }}
                  >
                    {label}
                  </button>
                )
              })}
            </div>
            <div style={{ fontSize: 11, color: 'var(--muted-raw)' }}>
              last {range}
            </div>
          </div>
          <LineChart
            data={lineData}
            unit={chartTab === 'latency' ? 'ms' : 'calls'}
            height={220}
            color={chartTab === 'latency' ? '#43d1c6' : '#5da9ff'}
          />
        </div>

        {/* 2x2 grid of donuts */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
            gap: 12,
          }}
        >
          <DonutCard
            title="Tokens / Call"
            segments={tokenSegments}
            centerLabel={tokenSegments.reduce((s, b) => s + b.count, 0).toLocaleString()}
            centerSubLabel="tokens"
          />
          <DonutCard
            title="Tool Calls"
            segments={toolSegments}
            centerLabel={toolSegments.reduce((s, b) => s + b.count, 0).toLocaleString()}
            centerSubLabel="calls"
            onSegmentClick={(s) => drillTraces(s.label)}
          />
          <DonutCard
            title="Agent → Agent"
            segments={a2aSegments}
            centerLabel={a2aSegments.reduce((s, b) => s + b.count, 0).toLocaleString()}
            centerSubLabel="A2A"
            onSegmentClick={(s) => drillTraces(s.label)}
          />
          <DonutCard
            title="Alert Reasons"
            segments={alertSegments}
            centerLabel={alertSegments.reduce((s, b) => s + b.count, 0).toLocaleString()}
            centerSubLabel="alerts"
            onSegmentClick={(s) => drillAlerts(s.label)}
          />
        </div>
      </div>

      {/* ── Send-message composer (collapsible) ───────────────────── */}
      {composerOpen && (
        <div
          style={{
            background: 'var(--panel)',
            border: '1px solid var(--line)',
            borderRadius: 12,
            padding: 14,
            marginBottom: 16,
          }}
        >
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Input
              value={draft}
              placeholder="Send a message to this agent…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  onSend()
                }
              }}
              disabled={sendMut.isPending}
            />
            <Button
              size="sm"
              onClick={onSend}
              disabled={sendMut.isPending || !draft.trim()}
              style={{ background: 'var(--blue)', color: '#0b1020' }}
            >
              {sendMut.isPending ? 'Sending…' : 'Send'}
            </Button>
          </div>
          {chatError && (
            <div
              style={{
                marginTop: 10,
                color: 'var(--red)',
                fontSize: 12,
              }}
            >
              {chatError}
            </div>
          )}
          {chatLog.length > 0 && (
            <div
              style={{
                marginTop: 12,
                display: 'flex',
                flexDirection: 'column',
                gap: 10,
                maxHeight: 320,
                overflowY: 'auto',
              }}
            >
              {chatLog.map((m) => (
                <div
                  key={m.id}
                  style={{
                    border: `1px solid ${m.denial ? 'rgba(224,82,82,.4)' : 'var(--line)'}`,
                    borderRadius: 10,
                    padding: 10,
                    background: 'var(--panel2)',
                  }}
                >
                  <div
                    style={{
                      fontSize: 11,
                      color: 'var(--muted-raw)',
                      letterSpacing: '.06em',
                      textTransform: 'uppercase',
                      marginBottom: 4,
                    }}
                  >
                    Prompt
                  </div>
                  <div style={{ fontSize: 13, marginBottom: 8 }}>{m.prompt}</div>
                  {m.denial ? (
                    <div
                      style={{
                        background: 'rgba(224,82,82,.1)',
                        border: '1px solid rgba(224,82,82,.4)',
                        color: '#ffb3b3',
                        padding: '8px 10px',
                        borderRadius: 8,
                        fontSize: 12,
                        marginBottom: 8,
                      }}
                    >
                      Denied · {m.denial.human}
                    </div>
                  ) : null}
                  <div
                    style={{
                      fontSize: 11,
                      color: 'var(--muted-raw)',
                      letterSpacing: '.06em',
                      textTransform: 'uppercase',
                      marginBottom: 4,
                    }}
                  >
                    Reply
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {m.reply || <span style={{ color: 'var(--muted-raw)' }}>—</span>}
                  </div>
                  {m.trace_id ? (
                    <div style={{ marginTop: 8 }}>
                      <button
                        onClick={() =>
                          navigate(`/traces/${encodeURIComponent(m.trace_id!)}`)
                        }
                        style={{
                          fontSize: 11,
                          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                          color: 'var(--blue)',
                          background: 'transparent',
                          border: 'none',
                          padding: 0,
                          cursor: 'pointer',
                          textDecoration: 'underline',
                        }}
                      >
                        trace · {m.trace_id.slice(0, 12)}
                      </button>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Recent Traces table ───────────────────────────────────── */}
      <div
        style={{
          background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            padding: '12px 16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid var(--line)',
          }}
        >
          <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--muted-raw)' }}>
            Recent Traces
          </div>
          <Button variant="ghost" size="sm" onClick={() => drillTraces()}>
            View all →
          </Button>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead style={{ width: '20%' }}>Trace ID</TableHead>
              <TableHead style={{ width: '14%' }}>Started</TableHead>
              <TableHead style={{ width: '12%' }}>Duration</TableHead>
              <TableHead style={{ width: '8%' }}>LLM</TableHead>
              <TableHead style={{ width: '8%' }}>Tools</TableHead>
              <TableHead style={{ width: '8%' }}>A2A</TableHead>
              <TableHead style={{ width: '12%' }}>Tokens</TableHead>
              <TableHead style={{ width: '10%' }}>Violations</TableHead>
              <TableHead style={{ width: '10%' }}>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!tracesQ.isLoading && traces.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={9}
                  style={{ textAlign: 'center', color: 'var(--muted-raw)', padding: 24 }}
                >
                  No traces yet for this agent.
                </TableCell>
              </TableRow>
            )}
            {traces.map((t) => {
              const idColor =
                t.status === 'failed'
                  ? 'var(--red)'
                  : t.violations > 0
                  ? 'var(--yellow)'
                  : 'var(--blue)'
              return (
                <TableRow
                  key={t.trace_id}
                  onClick={() => navigate(`/traces/${encodeURIComponent(t.trace_id)}`)}
                  style={{ cursor: 'pointer' }}
                >
                  <TableCell>
                    <span
                      style={{
                        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                        fontSize: 12,
                        color: idColor,
                        fontWeight: 600,
                      }}
                    >
                      {t.trace_id.length > 12 ? `${t.trace_id.slice(0, 8)}…` : t.trace_id}
                    </span>
                  </TableCell>
                  <TableCell style={{ fontSize: 12, color: 'var(--muted-raw)' }}>
                    {relativeTime(t.started_at)}
                  </TableCell>
                  <TableCell
                    style={{
                      fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                      fontSize: 12,
                    }}
                  >
                    {formatDuration(t.duration_ms)}
                  </TableCell>
                  <TableCell>{t.llm_calls}</TableCell>
                  <TableCell>{t.tool_calls}</TableCell>
                  <TableCell>{t.a2a_calls}</TableCell>
                  <TableCell>{t.total_tokens.toLocaleString()}</TableCell>
                  <TableCell>
                    {t.violations > 0 ? (
                      <span style={{ color: 'var(--red)', fontWeight: 700 }}>
                        {t.violations}
                      </span>
                    ) : (
                      <span style={{ color: 'var(--muted-raw)' }}>0</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {t.status === 'failed' ? (
                      <Badge variant="destructive">failed</Badge>
                    ) : (
                      <Badge variant="success">passed</Badge>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {/* Narrow-viewport stack: collapse the 2-col charts row on <960px. */}
      <style>{`
        @media (max-width: 960px) {
          .dash-charts-row { grid-template-columns: minmax(0, 1fr) !important; }
        }
      `}</style>
    </div>
  )
}

// ── Donut card wrapper ────────────────────────────────────────────────

function DonutCard({
  title,
  segments,
  centerLabel,
  centerSubLabel,
  onSegmentClick,
}: {
  title: string
  segments: CategoryBucket[]
  centerLabel?: string
  centerSubLabel?: string
  onSegmentClick?: (s: CategoryBucket) => void
}) {
  return (
    <div
      style={{
        background: 'var(--panel)',
        border: '1px solid var(--line)',
        borderRadius: 12,
        padding: 12,
        minWidth: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: '.08em',
          textTransform: 'uppercase',
          color: 'var(--muted-raw)',
        }}
      >
        {title}
      </div>
      <DonutChart
        segments={segments}
        size={140}
        thickness={20}
        legend="right"
        centerLabel={centerLabel}
        centerSubLabel={centerSubLabel}
        onSegmentClick={onSegmentClick}
      />
    </div>
  )
}
