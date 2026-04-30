import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveAgent,
  listAgents,
  rejectAgent,
  type Agent,
  type AgentState,
} from '@/api/agents'
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

interface Toast {
  id: number
  msg: string
  kind: 'success' | 'error'
}

const stateBadge = (state: AgentState) => {
  switch (state) {
    case 'pending':
      return <Badge variant="warning">⏳ pending</Badge>
    case 'approved-no-policy':
      return <Badge variant="default">approved · no policy</Badge>
    case 'approved-with-policy':
      return <Badge variant="success">● approved</Badge>
  }
}

export default function Agents() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [toasts, setToasts] = useState<Toast[]>([])

  const pushToast = (msg: string, kind: 'success' | 'error') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, msg, kind }])
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id))
    }, 3500)
  }

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 5_000,
  })

  const approve = useMutation({
    mutationFn: approveAgent,
    onSuccess: (_res, agentId) => {
      qc.invalidateQueries({ queryKey: ['agents'] })
      pushToast(`${agentId} approved`, 'success')
    },
    onError: (err: unknown, agentId) => {
      const msg = err instanceof Error ? err.message : 'Approve failed'
      pushToast(`Approve ${agentId}: ${msg}`, 'error')
    },
  })

  const reject = useMutation({
    mutationFn: rejectAgent,
    onSuccess: (_res, agentId) => {
      qc.invalidateQueries({ queryKey: ['agents'] })
      pushToast(`${agentId} rejected`, 'success')
    },
    onError: (err: unknown, agentId) => {
      const msg = err instanceof Error ? err.message : 'Reject failed'
      pushToast(`Reject ${agentId}: ${msg}`, 'error')
    },
  })

  const agents: Agent[] = data ?? []
  const total = agents.length
  const pendingCount = agents.filter((a) => a.state === 'pending').length

  const isMutating = (id: string) =>
    (approve.isPending && approve.variables === id) ||
    (reject.isPending && reject.variables === id)

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>Agents</h1>
        <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
          {isLoading
            ? 'Loading…'
            : `${total} agent${total === 1 ? '' : 's'}${
                pendingCount > 0 ? ` · ${pendingCount} pending approval` : ''
              }`}
        </p>
      </div>

      {isError && (
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
          Failed to load agents: {error instanceof Error ? error.message : 'unknown error'}
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
              <TableHead style={{ width: '24%' }}>Agent ID</TableHead>
              <TableHead style={{ width: '22%' }}>State</TableHead>
              <TableHead style={{ width: '12%' }}>Mode</TableHead>
              <TableHead>Endpoint</TableHead>
              <TableHead style={{ width: 200, textAlign: 'right' }}>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!isLoading && agents.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} style={{ textAlign: 'center', color: 'var(--muted-raw)', padding: 32 }}>
                  No agents registered yet.
                </TableCell>
              </TableRow>
            )}
            {agents.map((a) => {
              const busy = isMutating(a.agent_id)
              const clickable = a.state !== 'pending'
              // approved-with-policy → dashboard; approved-no-policy → policy editor.
              // Pending rows render the approve/reject buttons inline and aren't
              // clickable as a whole row.
              const rowTarget =
                a.state === 'approved-with-policy'
                  ? `/agents/${encodeURIComponent(a.agent_id)}`
                  : `/agents/${encodeURIComponent(a.agent_id)}/policy`
              const rowTitle =
                !clickable
                  ? 'Approve before editing policy'
                  : a.state === 'approved-with-policy'
                  ? 'Open dashboard'
                  : 'Edit policy'
              return (
                <TableRow
                  key={a.agent_id}
                  onClick={() => {
                    if (clickable) navigate(rowTarget)
                  }}
                  style={{ cursor: clickable ? 'pointer' : 'default' }}
                  title={rowTitle}
                >
                  <TableCell>
                    <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 13, fontWeight: 600 }}>
                      {a.agent_id}
                    </span>
                  </TableCell>
                  <TableCell>{stateBadge(a.state)}</TableCell>
                  <TableCell>
                    {a.policy_summary ? (
                      <Badge variant="default">{a.policy_summary.mode}</Badge>
                    ) : (
                      <span style={{ color: 'var(--muted-raw)', fontSize: 12 }}>—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <span
                      style={{
                        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                        fontSize: 12,
                        color: 'var(--muted-raw)',
                        maxWidth: 280,
                        display: 'inline-block',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        verticalAlign: 'middle',
                      }}
                      title={a.endpoint ?? ''}
                    >
                      {a.endpoint ?? '—'}
                    </span>
                  </TableCell>
                  <TableCell style={{ textAlign: 'right' }} onClick={(e) => e.stopPropagation()}>
                    {a.state === 'pending' ? (
                      <div style={{ display: 'inline-flex', gap: 8 }}>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => reject.mutate(a.agent_id)}
                          style={{ borderColor: 'var(--red)', color: 'var(--red)' }}
                        >
                          Reject
                        </Button>
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() => approve.mutate(a.agent_id)}
                          style={{ background: 'var(--green)', color: '#0b1020' }}
                        >
                          {busy ? '…' : 'Approve'}
                        </Button>
                      </div>
                    ) : (
                      <span style={{ color: 'var(--muted-raw)', fontSize: 12 }}>—</span>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {/* Inline toast stack */}
      <div
        style={{
          position: 'fixed',
          bottom: 24,
          right: 24,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
          zIndex: 100,
        }}
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            style={{
              padding: '10px 14px',
              borderRadius: 10,
              fontSize: 13,
              fontWeight: 500,
              background: t.kind === 'success' ? 'rgba(31,191,117,.18)' : 'rgba(224,82,82,.18)',
              border: `1px solid ${t.kind === 'success' ? 'var(--green)' : 'var(--red)'}`,
              color: t.kind === 'success' ? '#7be3a8' : '#ffb3b3',
              minWidth: 220,
              boxShadow: '0 8px 24px rgba(0,0,0,.4)',
            }}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  )
}
