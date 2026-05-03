import { useMemo, useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { ChevronLeft, ChevronDown, ChevronRight } from 'lucide-react'
import {
  getTrace,
  type TraceDetail as TraceDetailData,
  type TraceEdge,
  type TraceViolation,
} from '@/api/traces'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import SequenceDiagram from '@/components/SequenceDiagram'

function safeStr(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try { return JSON.stringify(v) } catch { return String(v) }
}

const MONO: React.CSSProperties = {
  fontFamily: 'JetBrains Mono, ui-monospace, monospace',
}

const codeBlockStyle: React.CSSProperties = {
  background: 'var(--panel2)',
  border: '1px solid var(--line)',
  borderRadius: 8,
  padding: '10px 12px',
  fontFamily: 'JetBrains Mono, ui-monospace, monospace',
  fontSize: 12,
  color: 'var(--text)',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  overflow: 'auto',
}

const cardStyle: React.CSSProperties = {
  background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
  border: '1px solid var(--line)',
  borderRadius: 14,
  padding: 18,
}

const sectionHeading: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: 'var(--muted-raw)',
  textTransform: 'uppercase',
  letterSpacing: '0.07em',
  marginBottom: 10,
}

const labelStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: 'var(--muted-raw)',
  textTransform: 'uppercase',
  letterSpacing: '0.07em',
}

function formatTs(ts: number | string | undefined | null): string {
  if (ts === undefined || ts === null || ts === '') return '—'
  const ms = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(String(ts))
  if (!Number.isFinite(ms)) return String(ts)
  const d = new Date(ms)
  return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')
}

function formatDuration(value: number | string | undefined): string {
  if (value === undefined || value === null) return '—'
  const ms = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(ms)) return String(value)
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function edgeStatusBadge(status: string) {
  const s = (status || '').toLowerCase()
  if (s === 'denied' || s === 'failed' || s === 'error')
    return <Badge variant="destructive">{status}</Badge>
  if (s === 'allowed' || s === 'ok' || s === 'passed')
    return <Badge variant="success">{status}</Badge>
  if (s === 'mocked') return <Badge variant="secondary">{status}</Badge>
  return <Badge variant="outline">{status || '—'}</Badge>
}

function MiniMetric({ label, value }: { label: string; value: number | string }) {
  return (
    <div
      style={{
        background: 'var(--panel2)',
        border: '1px solid var(--line)',
        borderRadius: 10,
        padding: '10px 12px',
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          color: 'var(--muted-raw)',
          textTransform: 'uppercase',
          letterSpacing: '0.07em',
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 18, fontWeight: 700, ...MONO }}>{value}</div>
    </div>
  )
}

function EdgesTable({
  edges,
  selectedIndex,
  onSelect,
}: {
  edges: TraceEdge[]
  selectedIndex: number | null
  onSelect: (i: number | null) => void
}) {
  // Track which rows have their input/output panel open.
  // Clicking a row (or selecting via diagram) auto-opens it.
  const [manualCollapsed, setManualCollapsed] = useState<Set<number>>(new Set())

  // When selection changes from outside (diagram click), clear manual-collapse so it auto-opens.
  useEffect(() => {
    if (selectedIndex !== null) {
      setManualCollapsed(s => { const next = new Set(s); next.delete(selectedIndex); return next })
    }
  }, [selectedIndex])

  const isOpen = (i: number) => {
    // Open if selected (via row click or diagram) AND not manually collapsed.
    return selectedIndex === i && !manualCollapsed.has(i)
  }

  const handleRowClick = (i: number) => {
    if (selectedIndex === i) {
      // Toggle collapse/expand when clicking the already-selected row.
      setManualCollapsed(s => {
        const next = new Set(s)
        if (next.has(i)) next.delete(i); else next.add(i)
        return next
      })
    } else {
      // Select a new row — clear manual collapse for it so it auto-opens.
      setManualCollapsed(s => { const next = new Set(s); next.delete(i); return next })
      onSelect(i)
    }
  }

  if (edges.length === 0) {
    return (
      <div style={{ color: 'var(--muted-raw)', fontSize: 13, padding: '8px 0' }}>
        No edges recorded.
      </div>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead style={{ width: 28 }}></TableHead>
            <TableHead>#</TableHead>
            <TableHead>Time</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Indirect</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Model</TableHead>
            <TableHead>In</TableHead>
            <TableHead>Out</TableHead>
            <TableHead>Total</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {edges.map((e, i) => {
            const isSelected = selectedIndex === i
            const open = isOpen(i)
            const rowStyle: React.CSSProperties = isSelected
              ? { background: 'rgba(93,169,255,.08)', boxShadow: 'inset 0 0 0 1px var(--blue)' }
              : {}
            return (
              <>
                <TableRow
                  key={`edge-${i}`}
                  style={{ ...rowStyle, cursor: 'pointer' }}
                  onClick={() => handleRowClick(i)}
                >
                  <TableCell>
                    <span style={{ color: 'var(--muted-raw)', display: 'inline-flex' }}>
                      {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </span>
                  </TableCell>
                  <TableCell style={MONO}>{i}</TableCell>
                  <TableCell style={{ ...MONO, fontSize: 11, color: 'var(--muted-raw)' }}>
                    {formatTs(e.ts)}
                  </TableCell>
                  <TableCell style={{ fontSize: 12 }}>{e.type}</TableCell>
                  <TableCell style={{ fontSize: 12, fontWeight: 600 }}>{e.name}</TableCell>
                  <TableCell style={{ fontSize: 12 }}>{e.indirect ? 'yes' : 'no'}</TableCell>
                  <TableCell style={{ fontSize: 12, color: 'var(--muted-raw)' }}>{e.source || '—'}</TableCell>
                  <TableCell style={{ ...MONO, fontSize: 11 }}>{e.model || '—'}</TableCell>
                  <TableCell style={MONO}>{e.input_tokens || '—'}</TableCell>
                  <TableCell style={MONO}>{e.output_tokens || '—'}</TableCell>
                  <TableCell style={MONO}>{e.total_tokens || '—'}</TableCell>
                  <TableCell>{edgeStatusBadge(e.status)}</TableCell>
                </TableRow>
                {open && (
                  <TableRow key={`edge-${i}-detail`} style={{ background: 'var(--panel2)' }}>
                    <TableCell colSpan={13} style={{ padding: '10px 12px' }}>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                        <div>
                          <div style={{ ...labelStyle, marginBottom: 6 }}>Input</div>
                          <div style={codeBlockStyle}>{safeStr(e.input) || '(empty)'}</div>
                        </div>
                        <div>
                          <div style={{ ...labelStyle, marginBottom: 6 }}>Output</div>
                          <div style={codeBlockStyle}>
                            {safeStr(e.output) || (
                              e.type === 'mcp'
                                ? '(streaming SSE response — not captured in audit log)'
                                : '(empty)'
                            )}
                          </div>
                        </div>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function ViolationsCard({ violations }: { violations: TraceViolation[] }) {
  if (violations.length === 0) {
    return (
      <div style={{ color: 'var(--muted-raw)', fontSize: 13, padding: '8px 0' }}>
        No violations detected.
      </div>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          <TableHead>Name</TableHead>
          <TableHead>Turn</TableHead>
          <TableHead>#</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Details</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {violations.map((v, i) => (
          <TableRow key={`v-${i}`}>
            <TableCell>
              <Badge variant="destructive">{v.kind}</Badge>
            </TableCell>
            <TableCell style={{ fontWeight: 600 }}>{v.name}</TableCell>
            <TableCell style={MONO}>{v.turn ?? '—'}</TableCell>
            <TableCell style={MONO}>{v.index ?? '—'}</TableCell>
            <TableCell>{edgeStatusBadge(v.status)}</TableCell>
            <TableCell style={{ fontSize: 12, color: 'var(--muted-raw)' }}>{safeStr(v.details)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function KeyValueRow({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <>
      <div style={{ color: 'var(--muted-raw)', fontSize: 12 }}>{k}</div>
      <div style={{ fontSize: 13 }}>{v}</div>
    </>
  )
}

export default function TraceDetail() {
  const { traceId = '' } = useParams<{ traceId: string }>()
  const navigate = useNavigate()
  const [selectedEdge, setSelectedEdge] = useState<number | null>(null)

  const { data, isLoading, isError, error } = useQuery<TraceDetailData>({
    queryKey: ['trace', traceId],
    queryFn: () => getTrace(traceId),
    enabled: !!traceId,
    refetchInterval: 5_000,
  })

  const policySnapshot = useMemo(() => {
    if (!data?.summary?.policy_snapshot) return '(empty)'
    try {
      return JSON.stringify(data.summary.policy_snapshot, null, 2)
    } catch {
      return String(data.summary.policy_snapshot)
    }
  }, [data])

  const goBack = () => {
    if (window.history.length > 1) {
      navigate(-1)
    } else {
      navigate('/traces')
    }
  }

  return (
    <div>
      {/* Top bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 16,
          marginBottom: 20,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
          <Button variant="ghost" size="sm" onClick={goBack}>
            <ChevronLeft size={14} /> Back
          </Button>
          <div>
            <div style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>
              {safeStr(data?.title) || 'Trace'}
            </div>
            <div style={{ color: 'var(--muted-raw)', fontSize: 12, marginTop: 4, ...MONO }}>
              trace_id: {traceId}
            </div>
          </div>
        </div>
        <div>
          {data &&
            (data.passed ? (
              <Badge variant="success">passed</Badge>
            ) : (
              <Badge variant="destructive">failed</Badge>
            ))}
        </div>
      </div>

      {isLoading && (
        <div style={{ ...cardStyle, color: 'var(--muted-raw)' }}>Loading trace…</div>
      )}

      {isError && (
        <div
          style={{
            background: 'rgba(224,82,82,.08)',
            border: '1px solid rgba(224,82,82,.3)',
            borderRadius: 10,
            padding: 14,
            color: 'var(--red)',
            fontSize: 13,
          }}
        >
          Failed to load trace: {error instanceof Error ? error.message : 'unknown error'}
        </div>
      )}

      {data && (
        <>
          {/* Summary 3-col grid */}
          <div className="trace-summary-grid">
            {/* Run summary */}
            <div style={cardStyle}>
              <div style={sectionHeading}>Test Run Summary</div>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '120px 1fr',
                  rowGap: 8,
                  columnGap: 12,
                  marginBottom: 14,
                }}
              >
                <KeyValueRow
                  k="Trace ID"
                  v={<span style={{ ...MONO, fontSize: 12 }}>{data.trace_id}</span>}
                />
                <KeyValueRow k="Agent" v={<span style={{ ...MONO }}>{data.agent_id}</span>} />
                <KeyValueRow k="Started" v={formatTs(data.started_at)} />
                <KeyValueRow k="Ended" v={formatTs(data.ended_at)} />
                <KeyValueRow k="Duration" v={formatDuration(data.duration)} />
                <KeyValueRow
                  k="Status"
                  v={
                    data.passed ? (
                      <Badge variant="success">passed</Badge>
                    ) : (
                      <Badge variant="destructive">failed</Badge>
                    )
                  }
                />
              </div>

              <div style={{ ...labelStyle, marginBottom: 5 }}>Agent Prompt</div>
              <div style={codeBlockStyle}>{data.summary?.prompt || '(no prompt)'}</div>

              <div style={{ ...labelStyle, marginTop: 10, marginBottom: 5 }}>
                {data.passed ? 'Final Answer' : 'Failure Details'}
              </div>
              <div style={codeBlockStyle}>
                {data.passed
                  ? data.summary?.final_answer || '(no answer)'
                  : data.summary?.failure_details ||
                    data.summary?.final_answer ||
                    '(no failure details)'}
              </div>
            </div>

            {/* Metrics */}
            <div style={cardStyle}>
              <div style={sectionHeading}>Metrics</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <MiniMetric
                  label="Total Tokens"
                  value={(data.metrics?.total_tokens ?? 0).toLocaleString()}
                />
                <MiniMetric label="LLM Calls" value={data.metrics?.llm_calls ?? 0} />
                <MiniMetric label="Tool Calls" value={data.metrics?.tool_calls ?? 0} />
                <MiniMetric label="Violations" value={data.metrics?.violations ?? 0} />
              </div>

              <div style={{ marginTop: 14 }}>
                <div style={{ ...labelStyle, marginBottom: 6 }}>Tool Breakdown</div>
                {data.metrics?.tool_breakdown?.length ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Tool</TableHead>
                        <TableHead style={{ textAlign: 'right' }}>Calls</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.metrics.tool_breakdown.map((t) => (
                        <TableRow key={t.tool}>
                          <TableCell style={{ fontSize: 12, ...MONO }}>{t.tool}</TableCell>
                          <TableCell style={{ textAlign: 'right', ...MONO, fontSize: 12 }}>
                            {t.count}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <div style={{ color: 'var(--muted-raw)', fontSize: 12 }}>
                    No tool calls recorded.
                  </div>
                )}
              </div>
            </div>

            {/* Policy snapshot */}
            <div style={cardStyle}>
              <div style={sectionHeading}>Policy Snapshot</div>
              <div style={{ ...codeBlockStyle, maxHeight: 420 }}>{policySnapshot}</div>
            </div>
          </div>

          {/* Sequence diagram */}
          <div style={{ ...cardStyle, marginTop: 14 }}>
            <div style={sectionHeading}>
              Conversation Sequence Diagram{' '}
              <span
                style={{
                  fontWeight: 400,
                  textTransform: 'none',
                  letterSpacing: 0,
                  color: 'var(--muted-raw)',
                  marginLeft: 6,
                }}
              >
                (click an arrow to highlight the edge below)
              </span>
            </div>
            <div
              style={{
                background: 'var(--panel2)',
                border: '1px solid var(--line)',
                borderRadius: 10,
                padding: 10,
                overflowX: 'auto',
                minHeight: 140,
              }}
            >
              <SequenceDiagram
                steps={data.sequence_steps ?? []}
                selectedIndex={selectedEdge}
                onSelectStep={(i) => setSelectedEdge(i)}
              />
            </div>
            <div
              style={{
                display: 'flex',
                gap: 14,
                flexWrap: 'wrap',
                marginTop: 10,
                fontSize: 12,
                color: 'var(--muted-raw)',
              }}
            >
              {[
                ['succeeded', 'var(--blue)'],
                ['failed', 'var(--red)'],
              ].map(([label, color]) => (
                <span
                  key={label}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                >
                  <i
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: '50%',
                      background: color,
                      display: 'inline-block',
                    }}
                  />
                  {label}
                </span>
              ))}
            </div>
          </div>

          {/* Execution edges */}
          <div style={{ ...cardStyle, marginTop: 14 }}>
            <div style={sectionHeading}>Execution Edges</div>
            <EdgesTable
              edges={data.edges ?? []}
              selectedIndex={selectedEdge}
              onSelect={setSelectedEdge}
            />
          </div>

          {/* Violations */}
          <div style={{ ...cardStyle, marginTop: 14, marginBottom: 30 }}>
            <div style={sectionHeading}>Violations & Assertion Failures</div>
            <ViolationsCard violations={data.violations_list ?? []} />
          </div>
        </>
      )}

      <style>{`
        .trace-summary-grid {
          display: grid;
          grid-template-columns: 2fr 1fr 1.4fr;
          gap: 14px;
        }
        @media (max-width: 1100px) {
          .trace-summary-grid {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
    </div>
  )
}
