import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { listTraces, type TraceListItem } from '@/api/traces'
import { listAgents } from '@/api/agents'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ExportModal } from '@/components/ExportModal'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const ALL = '__all__'

function relativeTime(ts: number | string | undefined | null): string {
  if (ts === undefined || ts === null) return '—'
  const ms = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(String(ts))
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

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function statusBadge(status: TraceListItem['status']) {
  if (status === 'failed') return <Badge variant="destructive">failed</Badge>
  return <Badge variant="success">passed</Badge>
}

export default function Traces() {
  const navigate = useNavigate()
  const [agentFilter, setAgentFilter] = useState<string>(ALL)
  const [exportOpen, setExportOpen] = useState(false)

  const agentsQuery = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 30_000,
  })

  const tracesQuery = useQuery({
    queryKey: ['traces', { agent: agentFilter }],
    queryFn: () =>
      listTraces({
        agent: agentFilter === ALL ? undefined : agentFilter,
        limit: 50,
      }),
    refetchInterval: 5_000,
  })

  const traces = tracesQuery.data?.traces ?? []
  const agents = agentsQuery.data ?? []

  const headerSubtitle = useMemo(() => {
    if (tracesQuery.isLoading) return 'Loading…'
    const total = traces.length
    const scope = agentFilter === ALL ? 'all agents' : agentFilter
    return `${total} trace${total === 1 ? '' : 's'} · ${scope}`
  }, [tracesQuery.isLoading, traces.length, agentFilter])

  return (
    <div>
      {/* Top bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 16,
          marginBottom: 24,
        }}
      >
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>Traces</h1>
          <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>{headerSubtitle}</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ minWidth: 200 }}>
            <Select value={agentFilter} onValueChange={setAgentFilter}>
              <SelectTrigger>
                <SelectValue placeholder="All agents" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All agents</SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id}>
                    {a.agent_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExportOpen(true)}
          >
            Export
          </Button>
        </div>
      </div>

      <ExportModal
        open={exportOpen}
        onClose={() => setExportOpen(false)}
        agentId={agentFilter === ALL ? undefined : agentFilter}
      />

      {tracesQuery.isError && (
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
          Failed to load traces:{' '}
          {tracesQuery.error instanceof Error ? tracesQuery.error.message : 'unknown error'}
        </div>
      )}

      <div
        style={{
          background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          overflow: 'hidden',
        }}
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead style={{ width: '18%' }}>Trace ID</TableHead>
              <TableHead style={{ width: '16%' }}>Agent</TableHead>
              <TableHead style={{ width: '12%' }}>Started</TableHead>
              <TableHead style={{ width: '10%' }}>Duration</TableHead>
              <TableHead style={{ width: '6%' }}>LLM</TableHead>
              <TableHead style={{ width: '7%' }}>Tools</TableHead>
              <TableHead style={{ width: '6%' }}>A2A</TableHead>
              <TableHead style={{ width: '8%' }}>Tokens</TableHead>
              <TableHead style={{ width: '8%' }}>Violations</TableHead>
              <TableHead style={{ width: '9%' }}>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!tracesQuery.isLoading && traces.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={10}
                  style={{ textAlign: 'center', color: 'var(--muted-raw)', padding: 32 }}
                >
                  No traces yet. Use the agent's Send Message action to generate one.
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
                  <TableCell style={{ fontSize: 13 }}>{t.agent_id}</TableCell>
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
                      <span style={{ color: 'var(--red)', fontWeight: 700 }}>{t.violations}</span>
                    ) : (
                      <span style={{ color: 'var(--muted-raw)' }}>0</span>
                    )}
                  </TableCell>
                  <TableCell>{statusBadge(t.status)}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
