import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  listAlerts,
  type AlertRange,
  type AlertRecord,
} from '@/api/alerts'
import { listAgents } from '@/api/agents'
import { Badge } from '@/components/ui/badge'
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
import { DonutChart } from '@/components/DonutChart'

const ALL_AGENTS = '__all__'
const RANGES: AlertRange[] = ['1h', '24h', '7d']

function relativeTime(ts: number | undefined | null): string {
  if (ts === undefined || ts === null) return '—'
  const ms = ts < 1e12 ? ts * 1000 : ts
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

export default function Alerts() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  // Read URL state — single source of truth for `agent`, `reason`, `range`.
  const agent = searchParams.get('agent') ?? ''
  const selectedReason = searchParams.get('reason')
  const rangeParam = (searchParams.get('range') ?? '24h') as AlertRange
  const range: AlertRange = RANGES.includes(rangeParam) ? rangeParam : '24h'

  // Helpers to update URL params while preserving other keys.
  function setParam(key: string, value: string | null) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value === null || value === '') next.delete(key)
        else next.set(key, value)
        return next
      },
      { replace: true },
    )
  }

  const setAgent = (v: string) =>
    setParam('agent', v === ALL_AGENTS ? null : v)
  const setRange = (v: AlertRange) => setParam('range', v)
  const setSelectedReason = (v: string | null) => setParam('reason', v)

  const agentsQuery = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 30_000,
  })

  const alertsQuery = useQuery({
    queryKey: ['alerts', { agent, range }],
    queryFn: () =>
      listAlerts({
        agent: agent || undefined,
        range,
        groupBy: 'reason',
        limit: 200,
      }),
    refetchInterval: 5_000,
  })

  const data = alertsQuery.data
  const total = data?.total ?? 0
  const byReason = data?.by_reason ?? []
  const records = data?.records ?? []
  const agents = agentsQuery.data ?? []

  const segments = useMemo(
    () => byReason.map((r) => ({ label: r.label, count: r.count })),
    [byReason],
  )

  const filteredRecords = useMemo<AlertRecord[]>(() => {
    if (!selectedReason) return records
    return records.filter((r) => r.denial_reason === selectedReason)
  }, [records, selectedReason])

  const togglePill = (label: string | null) => {
    if (label === null) {
      setSelectedReason(null)
      return
    }
    setSelectedReason(selectedReason === label ? null : label)
  }

  return (
    <div>
      {/* ───────── Top bar ───────── */}
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
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>
            Alerts
          </h1>
          <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
            Policy violations and budget breaches
            {alertsQuery.isLoading ? ' · loading…' : ''}
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* Time range segmented control */}
          <div
            style={{
              display: 'inline-flex',
              padding: 3,
              borderRadius: 8,
              background: 'var(--panel2, #0f1627)',
              border: '1px solid var(--line)',
            }}
          >
            {RANGES.map((r) => {
              const active = r === range
              return (
                <button
                  key={r}
                  onClick={() => setRange(r)}
                  style={{
                    padding: '4px 12px',
                    fontSize: 12,
                    fontWeight: 600,
                    borderRadius: 6,
                    border: 'none',
                    background: active ? 'var(--blue, #5da9ff)' : 'transparent',
                    color: active ? '#0b1020' : 'var(--muted-raw)',
                    cursor: 'pointer',
                    transition: 'background 150ms ease, color 150ms ease',
                  }}
                >
                  {r}
                </button>
              )
            })}
          </div>

          {/* Agent picker */}
          <div style={{ minWidth: 200 }}>
            <Select
              value={agent === '' ? ALL_AGENTS : agent}
              onValueChange={setAgent}
            >
              <SelectTrigger>
                <SelectValue placeholder="All agents" />
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

          {/* Total alerts pill */}
          <div
            style={{
              padding: '6px 12px',
              borderRadius: 999,
              background: total > 0 ? 'rgba(239,68,68,.12)' : 'rgba(148,163,184,.1)',
              border: `1px solid ${
                total > 0 ? 'rgba(239,68,68,.3)' : 'var(--line)'
              }`,
              color: total > 0 ? 'var(--red, #ef4444)' : 'var(--muted-raw)',
              fontSize: 12,
              fontWeight: 700,
              whiteSpace: 'nowrap',
            }}
          >
            {total} alert{total === 1 ? '' : 's'}
          </div>
        </div>
      </div>

      {alertsQuery.isError && (
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
          Failed to load alerts:{' '}
          {alertsQuery.error instanceof Error
            ? alertsQuery.error.message
            : 'unknown error'}
        </div>
      )}

      {/* ───────── Reason pills ───────── */}
      <div
        style={{
          background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          padding: '14px 18px',
          marginBottom: 14,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 10,
          }}
        >
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
              color: 'var(--muted-raw)',
            }}
          >
            Alert Reasons
            <span
              style={{
                fontWeight: 400,
                textTransform: 'none',
                letterSpacing: 0,
                marginLeft: 8,
              }}
            >
              — click to filter
            </span>
          </div>
          {selectedReason && (
            <button
              onClick={() => setSelectedReason(null)}
              style={{
                fontSize: 11,
                background: 'transparent',
                border: '1px solid var(--line)',
                color: 'var(--muted-raw)',
                padding: '3px 10px',
                borderRadius: 6,
                cursor: 'pointer',
              }}
            >
              ✕ Clear filter
            </button>
          )}
        </div>

        <div
          style={{
            display: 'flex',
            gap: 8,
            flexWrap: 'wrap',
            overflowX: 'auto',
          }}
        >
          {/* "All" pill */}
          <ReasonPill
            label="All"
            count={total}
            active={selectedReason === null}
            dim={false}
            onClick={() => togglePill(null)}
          />
          {byReason.map((r) => {
            const active = selectedReason === r.label
            return (
              <ReasonPill
                key={r.label}
                label={r.label}
                count={r.count}
                active={active}
                dim={selectedReason !== null && !active}
                onClick={() => togglePill(r.label)}
              />
            )
          })}
          {byReason.length === 0 && !alertsQuery.isLoading && (
            <span style={{ fontSize: 12, color: 'var(--muted-raw)' }}>
              No reasons in this range.
            </span>
          )}
        </div>
      </div>

      {/* ───────── Big donut ───────── */}
      <div
        style={{
          background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          padding: 24,
          marginBottom: 14,
          display: 'flex',
          justifyContent: 'center',
        }}
      >
        <DonutChart
          segments={segments}
          size={280}
          legend="bottom"
          selectedLabel={selectedReason ?? undefined}
          onSegmentClick={(s) => togglePill(s.label)}
          centerSubLabel="alerts"
          empty={
            <div
              style={{
                fontSize: 12,
                color: 'var(--muted-raw)',
                textAlign: 'center',
              }}
            >
              No alerts in this range.
            </div>
          }
        />
      </div>

      {/* ───────── Filtered traces table ───────── */}
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
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '14px 18px',
            borderBottom: '1px solid var(--line)',
          }}
        >
          <div>
            <div
              style={{
                fontSize: 10,
                fontWeight: 700,
                textTransform: 'uppercase',
                letterSpacing: '0.06em',
                color: 'var(--muted-raw)',
                marginBottom: 4,
              }}
            >
              Traces with alerts{' '}
              <span
                style={{
                  color: 'var(--blue, #5da9ff)',
                  fontWeight: 600,
                  textTransform: 'none',
                  letterSpacing: 0,
                }}
              >
                {selectedReason ? `(${selectedReason})` : '(all reasons)'}
              </span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--muted-raw)' }}>
              Showing {filteredRecords.length} trace
              {filteredRecords.length === 1 ? '' : 's'} · click any to inspect
            </div>
          </div>
        </div>

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead style={{ width: '16%' }}>Trace ID</TableHead>
              <TableHead style={{ width: '16%' }}>Agent</TableHead>
              <TableHead style={{ width: '12%' }}>Time</TableHead>
              <TableHead style={{ width: '20%' }}>Reason</TableHead>
              <TableHead style={{ width: '24%' }}>Tool / Target</TableHead>
              <TableHead style={{ width: '12%' }}>Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!alertsQuery.isLoading && filteredRecords.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={6}
                  style={{
                    textAlign: 'center',
                    color: 'var(--muted-raw)',
                    padding: 32,
                  }}
                >
                  {selectedReason
                    ? `No alerts matching reason: ${selectedReason}`
                    : 'No alerts in this range.'}
                </TableCell>
              </TableRow>
            )}
            {filteredRecords.map((r) => {
              const toolOrTarget = r.tool ?? r.target ?? '—'
              return (
                <TableRow
                  key={`${r.trace_id}:${r.ts}`}
                  onClick={() =>
                    navigate(`/traces/${encodeURIComponent(r.trace_id)}`)
                  }
                  style={{ cursor: 'pointer' }}
                >
                  <TableCell>
                    <span
                      style={{
                        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                        fontSize: 12,
                        color: 'var(--red, #ef4444)',
                        fontWeight: 600,
                      }}
                    >
                      {r.trace_id.length > 12
                        ? `${r.trace_id.slice(0, 8)}…`
                        : r.trace_id}
                    </span>
                  </TableCell>
                  <TableCell style={{ fontSize: 13 }}>{r.agent_id}</TableCell>
                  <TableCell
                    style={{ fontSize: 12, color: 'var(--muted-raw)' }}
                  >
                    {relativeTime(r.ts)}
                  </TableCell>
                  <TableCell>
                    <Badge variant="destructive">{r.denial_reason}</Badge>
                  </TableCell>
                  <TableCell
                    style={{
                      fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                      fontSize: 12,
                      color: 'var(--muted-raw)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {toolOrTarget}
                  </TableCell>
                  <TableCell>
                    <Badge variant="destructive">blocked</Badge>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

interface ReasonPillProps {
  label: string
  count: number
  active: boolean
  dim: boolean
  onClick: () => void
}

function ReasonPill({ label, count, active, dim, onClick }: ReasonPillProps) {
  return (
    <button
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 12px',
        borderRadius: 999,
        border: active
          ? '1px solid var(--blue, #5da9ff)'
          : '1px solid var(--line)',
        background: active
          ? 'rgba(93,169,255,.15)'
          : 'var(--panel2, #0f1627)',
        color: active ? 'var(--blue, #5da9ff)' : 'var(--text, #e8eefc)',
        fontSize: 12,
        fontWeight: 600,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
        opacity: dim ? 0.5 : 1,
        boxShadow: active ? '0 0 0 2px rgba(93,169,255,.25)' : 'none',
        transition:
          'opacity 150ms ease, background 150ms ease, border-color 150ms ease',
      }}
    >
      <span>{label}</span>
      <span
        style={{
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          fontSize: 11,
          fontWeight: 700,
          color: active ? 'var(--blue, #5da9ff)' : 'var(--muted-raw)',
        }}
      >
        {count}
      </span>
    </button>
  )
}
