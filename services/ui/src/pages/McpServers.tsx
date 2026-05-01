import { useState } from 'react'
import { Plus } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveMcpServer,
  listMcpServers,
  rejectMcpServer,
  type McpServer,
} from '@/api/mcp'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import AddMcpModal from '@/components/AddMcpModal'

interface Toast {
  id: number
  msg: string
  kind: 'success' | 'error'
}

export default function McpServers() {
  const qc = useQueryClient()
  const [modalOpen, setModalOpen] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])

  const pushToast = (msg: string, kind: 'success' | 'error') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, msg, kind }])
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id))
    }, 3500)
  }

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['mcp_servers'],
    queryFn: listMcpServers,
    refetchInterval: 5_000,
  })

  const approve = useMutation({
    mutationFn: approveMcpServer,
    onSuccess: (_res, name) => {
      qc.invalidateQueries({ queryKey: ['mcp_servers'] })
      pushToast(`${name} approved`, 'success')
    },
    onError: (err: unknown, name) => {
      const msg = err instanceof Error ? err.message : 'Approve failed'
      pushToast(`Approve ${name}: ${msg}`, 'error')
    },
  })

  const reject = useMutation({
    mutationFn: rejectMcpServer,
    onSuccess: (_res, name) => {
      qc.invalidateQueries({ queryKey: ['mcp_servers'] })
      pushToast(`${name} rejected`, 'success')
    },
    onError: (err: unknown, name) => {
      const msg = err instanceof Error ? err.message : 'Reject failed'
      pushToast(`Reject ${name}: ${msg}`, 'error')
    },
  })

  const servers: McpServer[] = data ?? []
  const total = servers.length
  const pending = servers.filter((s) => !s.approved).length

  const isBusy = (name: string) =>
    (approve.isPending && approve.variables === name) ||
    (reject.isPending && reject.variables === name)

  return (
    <div>
      <div
        style={{
          marginBottom: 24,
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 16,
        }}
      >
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>MCP Servers</h1>
          <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
            {isLoading
              ? 'Loading…'
              : `${total} server${total === 1 ? '' : 's'}${
                  pending > 0 ? ` · ${pending} pending approval` : ''
                }`}
          </p>
        </div>
        <Button onClick={() => setModalOpen(true)} size="sm">
          <Plus size={14} strokeWidth={2.5} />
          Add Server
        </Button>
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
          Failed to load MCP servers: {error instanceof Error ? error.message : 'unknown error'}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {!isLoading && servers.length === 0 && (
          <div
            style={{
              background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
              border: '1px solid var(--line)',
              borderRadius: 14,
              padding: 32,
              textAlign: 'center',
              color: 'var(--muted-raw)',
              fontSize: 13,
            }}
          >
            No MCP servers registered yet. Click <strong>Add Server</strong> to register one
            manually, or wait for one to self-register.
          </div>
        )}

        {servers.map((s) => {
          const busy = isBusy(s.name)
          const borderColor = s.approved ? 'var(--line)' : 'rgba(242,185,75,.3)'
          const bgGradient = s.approved
            ? 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)'
            : 'linear-gradient(180deg, rgba(242,185,75,.04) 0%, var(--panel) 100%)'

          return (
            <div
              key={s.name}
              style={{
                background: bgGradient,
                border: `1px solid ${borderColor}`,
                borderRadius: 14,
                padding: 18,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  marginBottom: 14,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 14, minWidth: 0 }}>
                  <div
                    style={{
                      width: 38,
                      height: 38,
                      borderRadius: 10,
                      background: s.approved ? 'rgba(67,209,198,.12)' : 'rgba(242,185,75,.12)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                      color: s.approved ? 'var(--cyan)' : 'var(--yellow)',
                      fontSize: 16,
                      fontWeight: 700,
                    }}
                  >
                    {s.name.slice(0, 1).toUpperCase()}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 15, fontWeight: 700 }}>{s.name}</div>
                    <div
                      style={{
                        fontSize: 12,
                        color: 'var(--muted-raw)',
                        marginTop: 2,
                        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={s.url}
                    >
                      {s.url}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
                  {s.approved ? (
                    <Badge variant="success">● approved</Badge>
                  ) : (
                    <Badge variant="warning">⏳ pending</Badge>
                  )}
                  {!s.approved && (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => reject.mutate(s.name)}
                        style={{ borderColor: 'var(--red)', color: 'var(--red)' }}
                      >
                        Reject
                      </Button>
                      <Button
                        size="sm"
                        disabled={busy}
                        onClick={() => approve.mutate(s.name)}
                        style={{ background: 'var(--green)', color: '#0b1020' }}
                      >
                        {busy ? '…' : 'Approve'}
                      </Button>
                    </>
                  )}
                </div>
              </div>

              <div style={{
                fontSize: 11, fontWeight: 600, textTransform: 'uppercase',
                letterSpacing: '0.5px', color: 'var(--muted-raw)', marginBottom: 8,
              }}>
                Available Tools ({s.tools.length}){!s.approved && ' — review before approving'}
              </div>

              {s.tools.length === 0 ? (
                <span style={{ color: 'var(--muted-raw)', fontSize: 12 }}>No tools listed.</span>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr>
                      <th style={{ textAlign: 'left', color: 'var(--muted-raw)', fontWeight: 600,
                                   paddingBottom: 6, paddingRight: 16, width: '30%',
                                   borderBottom: '1px solid var(--line)' }}>Tool</th>
                      <th style={{ textAlign: 'left', color: 'var(--muted-raw)', fontWeight: 600,
                                   paddingBottom: 6, borderBottom: '1px solid var(--line)' }}>Description</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.tools.map((tool) => (
                      <tr key={tool.name}>
                        <td style={{ paddingTop: 7, paddingBottom: 7, paddingRight: 16,
                                     fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                                     color: 'var(--cyan)', fontWeight: 600,
                                     verticalAlign: 'top' }}>
                          {tool.name}
                        </td>
                        <td style={{ paddingTop: 7, paddingBottom: 7,
                                     color: tool.description ? 'var(--text)' : 'var(--muted-raw)',
                                     verticalAlign: 'top', lineHeight: 1.45 }}>
                          {tool.description ?? '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )
        })}
      </div>

      <AddMcpModal open={modalOpen} onOpenChange={setModalOpen} onToast={pushToast} />

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
