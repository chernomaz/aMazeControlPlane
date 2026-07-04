import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2, UserPlus, ShieldCheck, User as UserIcon } from 'lucide-react'
import {
  listUsers,
  createUser,
  deleteUser,
  assignAgent,
  type UserRecord,
} from '@/api/users'
import { listAgents, type Agent } from '@/api/agents'
import { getMe } from '@/api/auth'
import { ApiError } from '@/api/client'

const card: React.CSSProperties = {
  background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
  border: '1px solid var(--line)',
  borderRadius: 14,
  padding: 18,
  marginBottom: 16,
}

const label: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  color: 'var(--muted-raw)',
  marginBottom: 10,
}

const input: React.CSSProperties = {
  padding: '8px 11px',
  fontSize: 13,
  background: 'var(--panel2)',
  border: '1px solid var(--line)',
  borderRadius: 8,
  color: 'var(--text)',
  outline: 'none',
  boxSizing: 'border-box',
}

const errMsg = (e: unknown) =>
  e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'error'

export default function Users() {
  const qc = useQueryClient()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: getMe, retry: false })

  const usersQuery = useQuery({ queryKey: ['users'], queryFn: listUsers })
  const agentsQuery = useQuery({ queryKey: ['agents'], queryFn: listAgents })

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('user')
  const [formErr, setFormErr] = useState<string | null>(null)

  const createMut = useMutation({
    mutationFn: () => createUser(username.trim(), password, role),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      setRole('user')
      setFormErr(null)
      qc.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (e) => setFormErr(errMsg(e)),
  })

  const deleteMut = useMutation({
    mutationFn: (userId: string) => deleteUser(userId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })

  const assignMut = useMutation({
    mutationFn: ({ agentId, userId }: { agentId: string; userId: string }) =>
      assignAgent(agentId, userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agents'] }),
  })

  if (me && me.role !== 'admin') {
    return (
      <div style={{ ...card, color: 'var(--muted-raw)' }}>
        Admin access required to manage users.
      </div>
    )
  }

  const users: UserRecord[] = usersQuery.data ?? []
  const agents: Agent[] = agentsQuery.data ?? []

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setFormErr('Username and password are required.')
      return
    }
    createMut.mutate()
  }

  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>Users</h1>
        <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
          Create accounts and assign agents to a single owner
        </p>
      </div>

      {/* ───────── Create user ───────── */}
      <div style={card}>
        <div style={label}>Create user</div>
        <form
          onSubmit={onSubmit}
          style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}
        >
          <input
            style={{ ...input, flex: '1 1 180px' }}
            placeholder="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
          />
          <input
            style={{ ...input, flex: '1 1 180px' }}
            placeholder="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
          <select
            style={{ ...input, flex: '0 0 140px', cursor: 'pointer' }}
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="user">regular user</option>
            <option value="admin">admin</option>
          </select>
          <button
            type="submit"
            disabled={createMut.isPending}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 7,
              padding: '8px 16px',
              fontSize: 13,
              fontWeight: 700,
              borderRadius: 8,
              border: 'none',
              background: 'var(--blue)',
              color: '#0b1020',
              cursor: createMut.isPending ? 'default' : 'pointer',
              opacity: createMut.isPending ? 0.6 : 1,
            }}
          >
            <UserPlus size={15} strokeWidth={2.2} />
            {createMut.isPending ? 'Creating…' : 'Create'}
          </button>
        </form>
        {formErr && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: 'var(--red, #ef4444)' }}>
            {formErr}
          </div>
        )}
      </div>

      {/* ───────── Users list ───────── */}
      <div style={card}>
        <div style={label}>
          Accounts {usersQuery.isLoading ? '· loading…' : `· ${users.length}`}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {users.length === 0 && !usersQuery.isLoading && (
            <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>No users yet.</div>
          )}
          {users.map((u) => {
            const isAdmin = u.role === 'admin'
            const isSelf = me?.user_id === u.user_id
            return (
              <div
                key={u.user_id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '10px 12px',
                  borderRadius: 10,
                  background: 'var(--panel2)',
                  border: '1px solid var(--line)',
                }}
              >
                <span
                  style={{
                    width: 30,
                    height: 30,
                    flexShrink: 0,
                    borderRadius: '50%',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: isAdmin ? 'rgba(242,185,75,.15)' : 'rgba(93,169,255,.15)',
                    color: isAdmin ? 'var(--yellow)' : 'var(--blue)',
                  }}
                >
                  {isAdmin ? <ShieldCheck size={16} /> : <UserIcon size={16} />}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 700, color: 'var(--text)' }}>
                    {u.username}
                    {isSelf && (
                      <span style={{ color: 'var(--muted-raw)', fontWeight: 500 }}> · you</span>
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: 10,
                      textTransform: 'uppercase',
                      letterSpacing: '.06em',
                      color: isAdmin ? 'var(--yellow)' : 'var(--muted-raw)',
                    }}
                  >
                    {u.role}
                  </div>
                </div>
                <button
                  onClick={() => {
                    if (confirm(`Delete user "${u.username}"? Their agents become unassigned.`))
                      deleteMut.mutate(u.user_id)
                  }}
                  disabled={isSelf || deleteMut.isPending}
                  title={isSelf ? 'You cannot delete your own account' : 'Delete user'}
                  style={{
                    flexShrink: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 32,
                    height: 32,
                    borderRadius: 8,
                    background: 'transparent',
                    border: '1px solid var(--line)',
                    color: isSelf ? 'var(--muted-raw)' : 'var(--red, #ef4444)',
                    cursor: isSelf ? 'not-allowed' : 'pointer',
                    opacity: isSelf ? 0.4 : 1,
                  }}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            )
          })}
        </div>
      </div>

      {/* ───────── Agent ownership ───────── */}
      <div style={card}>
        <div style={label}>Agent ownership — one owner per agent</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {agents.length === 0 && !agentsQuery.isLoading && (
            <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>No agents registered.</div>
          )}
          {agents.map((a) => (
            <div
              key={a.agent_id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '10px 12px',
                borderRadius: 10,
                background: 'var(--panel2)',
                border: '1px solid var(--line)',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 13.5,
                    fontWeight: 700,
                    color: 'var(--text)',
                    fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                  }}
                >
                  {a.agent_id}
                </div>
                <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 2 }}>
                  owner:{' '}
                  <span style={{ color: a.owner ? 'var(--text)' : 'var(--yellow)' }}>
                    {a.owner ?? 'unassigned'}
                  </span>
                </div>
              </div>
              <select
                value={users.find((u) => u.username === a.owner)?.user_id ?? ''}
                onChange={(e) => {
                  if (e.target.value)
                    assignMut.mutate({ agentId: a.agent_id, userId: e.target.value })
                }}
                style={{ ...input, flex: '0 0 200px', cursor: 'pointer' }}
              >
                <option value="" disabled>
                  Assign to…
                </option>
                {users.map((u) => (
                  <option key={u.user_id} value={u.user_id}>
                    {u.username} ({u.role})
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
        {assignMut.isError && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: 'var(--red, #ef4444)' }}>
            {errMsg(assignMut.error)}
          </div>
        )}
      </div>
    </div>
  )
}
