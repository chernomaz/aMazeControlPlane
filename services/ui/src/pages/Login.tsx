import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { login } from '@/api/auth'
import { ApiError } from '@/api/client'

export default function Login() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setError(null)
    setBusy(true)
    try {
      const me = await login(username, password)
      // Drop every cached query from any prior session so the incoming user
      // never sees the previous user's agents/users list (the stale-until-
      // refresh bug). Then prime ['me'] from the login response so RequireAuth
      // renders immediately instead of flashing its loader / bouncing to login.
      queryClient.clear()
      queryClient.setQueryData(['me'], me)
      navigate('/')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Invalid username or password')
      } else {
        setError(err instanceof Error ? err.message : 'Login failed')
      }
    } finally {
      setBusy(false)
    }
  }

  const inputStyle: React.CSSProperties = {
    width: '100%',
    padding: '9px 11px',
    fontSize: 13,
    background: 'var(--panel2)',
    border: '1px solid var(--line)',
    borderRadius: 8,
    color: 'var(--text)',
    outline: 'none',
    boxSizing: 'border-box',
  }

  const labelStyle: React.CSSProperties = {
    display: 'block',
    fontSize: 11,
    fontWeight: 700,
    color: 'var(--muted-raw)',
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    marginBottom: 6,
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'var(--bg)',
        color: 'var(--text)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: 360,
          background: 'var(--panel)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          padding: 28,
        }}
      >
        {/* Brand */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.5px', color: 'var(--blue)' }}>
            aMaze
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted-raw)', marginTop: 2 }}>
            Sign in to the Control Plane
          </div>
        </div>

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 14 }}>
            <label htmlFor="username" style={labelStyle}>Username</label>
            <input
              id="username"
              name="username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={e => setUsername(e.target.value)}
              style={inputStyle}
              autoFocus
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label htmlFor="password" style={labelStyle}>Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              style={inputStyle}
            />
          </div>

          {error && (
            <div
              role="alert"
              style={{
                marginBottom: 14,
                padding: '9px 12px',
                borderRadius: 8,
                fontSize: 12.5,
                background: 'rgba(224,82,82,.12)',
                border: '1px solid rgba(224,82,82,.35)',
                color: '#ffb3b3',
              }}
            >
              {error}
            </div>
          )}

          <Button
            type="submit"
            disabled={busy || !username.trim() || !password}
            style={{ width: '100%', background: 'var(--blue)', color: '#0b1020', fontWeight: 700 }}
          >
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
      </div>
    </div>
  )
}
