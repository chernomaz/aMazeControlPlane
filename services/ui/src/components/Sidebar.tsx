import { NavLink, useLocation, useSearchParams, useNavigate } from 'react-router-dom'
import { Sun, Server, Users, Activity, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useQuery } from '@tanstack/react-query'
import { listAgents, type Agent } from '@/api/agents'
import { useState } from 'react'

type Filter = 'all' | 'pending' | 'approved'

function AgentSidebarCard({ agent, selected, onClick }: {
  agent: Agent
  selected: boolean
  onClick: () => void
}) {
  const hasPol = agent.state === 'approved-with-policy'
  const isPending = agent.state === 'pending'
  const isOnline = agent.endpoint && !isPending

  return (
    <div
      onClick={onClick}
      style={{
        padding: '9px 11px',
        borderRadius: 8,
        border: `1px solid ${selected ? 'var(--blue)' : 'var(--line)'}`,
        background: selected ? 'rgba(93,169,255,.08)' : 'var(--panel2)',
        cursor: 'pointer',
        transition: 'all .13s',
        flexShrink: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontWeight: 700, fontSize: 12.5, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 130 }}>
          {agent.agent_id}
        </span>
        <span style={{
          width: 7, height: 7, borderRadius: '50%', display: 'inline-block', flexShrink: 0,
          background: isPending ? 'var(--yellow)' : isOnline ? 'var(--green)' : 'var(--muted-raw)',
        }} />
      </div>
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {agent.policy_summary?.mode && (
          <span style={{ fontSize: 9, padding: '1px 6px', borderRadius: 4, background: 'rgba(93,169,255,.15)', color: 'var(--blue)', border: '1px solid rgba(93,169,255,.3)' }}>
            {agent.policy_summary.mode}
          </span>
        )}
        {isPending ? (
          <span style={{ fontSize: 9, padding: '1px 6px', borderRadius: 4, background: 'rgba(255,188,0,.15)', color: 'var(--yellow)', border: '1px solid rgba(255,188,0,.3)' }}>
            ⏳ pending
          </span>
        ) : hasPol ? (
          <span style={{ fontSize: 9, padding: '1px 6px', borderRadius: 4, background: 'rgba(31,191,117,.12)', color: 'var(--green)', border: '1px solid rgba(31,191,117,.3)' }}>
            policy ✓
          </span>
        ) : (
          <span style={{ fontSize: 9, padding: '1px 6px', borderRadius: 4, background: 'rgba(255,188,0,.1)', color: 'var(--yellow)', border: '1px solid rgba(255,188,0,.25)' }}>
            ⚠ no policy
          </span>
        )}
      </div>
    </div>
  )
}

function NavItem({ to, label, icon: Icon, badge }: {
  to: string; label: string; icon: React.ElementType; badge?: number
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn('nav-item', 'flex items-center gap-2.5 cursor-pointer rounded-lg select-none transition-colors', isActive ? 'is-active' : '')
      }
      style={({ isActive }) => ({
        padding: '9px 14px',
        margin: '2px 8px',
        fontSize: 13,
        fontWeight: 500,
        color: isActive ? 'var(--blue)' : 'var(--muted-raw)',
        background: isActive ? 'rgba(93,169,255,.12)' : 'transparent',
        textDecoration: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      })}
    >
      <Icon size={16} strokeWidth={2} />
      <span>{label}</span>
      {typeof badge === 'number' && badge > 0 && (
        <span style={{ marginLeft: 'auto', fontSize: 10, padding: '1px 7px', borderRadius: 999, fontWeight: 700, background: 'rgba(224,82,82,.16)', color: '#ffb3b3' }}>
          {badge}
        </span>
      )}
    </NavLink>
  )
}

export default function Sidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const isAgents = location.pathname === '/agents'

  const { data } = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 5_000,
    enabled: isAgents,
  })

  const agents: Agent[] = data ?? []
  const selectedId = searchParams.get('id')
  const pendingCount = agents.filter(a => a.state === 'pending').length

  const filtered = agents.filter(a => {
    if (filter === 'pending' && a.state !== 'pending') return false
    if (filter === 'approved' && a.state === 'pending') return false
    if (search.trim() && !a.agent_id.toLowerCase().includes(search.trim().toLowerCase())) return false
    return true
  })

  const filterBtnStyle = (f: Filter): React.CSSProperties => ({
    flex: 1,
    padding: '5px',
    fontSize: 11,
    fontWeight: 600,
    background: filter === f ? 'var(--blue)' : 'transparent',
    color: filter === f ? '#0b1020' : 'var(--muted-raw)',
    border: 'none',
    borderRadius: 5,
    cursor: 'pointer',
  })

  return (
    <aside
      style={{
        width: 230,
        height: '100vh',
        background: 'var(--panel)',
        borderRight: '1px solid var(--line)',
        position: 'fixed',
        top: 0,
        left: 0,
        bottom: 0,
        zIndex: 10,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Brand */}
      <div style={{ padding: '18px 16px 14px', flexShrink: 0 }}>
        <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: '-0.5px', color: 'var(--blue)' }}>aMaze</div>
        <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 2 }}>Control Plane</div>
      </div>

      {/* Top nav: LLMs, MCP, Agents */}
      <div style={{ flexShrink: 0, padding: '4px 0' }}>
        <NavItem to="/llms" label="LLM Providers" icon={Sun} />
        <NavItem to="/mcp-servers" label="MCP Servers" icon={Server} />
        <NavLink
          to="/agents"
          className={({ isActive }) => cn('nav-item', 'flex items-center gap-2.5 cursor-pointer rounded-lg select-none transition-colors', isActive ? 'is-active' : '')}
          style={({ isActive }) => ({
            padding: '9px 14px', margin: '2px 8px', fontSize: 13, fontWeight: 500,
            color: isActive ? 'var(--blue)' : 'var(--muted-raw)',
            background: isActive ? 'rgba(93,169,255,.12)' : 'transparent',
            textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 10,
          })}
        >
          <Users size={16} strokeWidth={2} />
          <span>Agents</span>
          {pendingCount > 0 && (
            <span style={{ marginLeft: 'auto', fontSize: 10, padding: '1px 7px', borderRadius: 999, fontWeight: 700, background: 'rgba(242,185,75,.16)', color: 'var(--yellow)' }}>
              {pendingCount}
            </span>
          )}
        </NavLink>
      </div>

      {/* Agent list — between Agents and Traces, scrollable */}
      {isAgents && (
        <div style={{
          padding: '8px 12px 10px',
          borderTop: '1px solid var(--line)',
          borderBottom: '1px solid var(--line)',
          display: 'flex',
          flexDirection: 'column',
          flex: 1,
          minHeight: 0,
          overflow: 'hidden',
        }}>
          {/* Search */}
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="🔍 Search…"
            style={{
              width: '100%',
              marginBottom: 8,
              padding: '7px 10px',
              fontSize: 12,
              background: 'var(--panel2)',
              border: '1px solid var(--line)',
              borderRadius: 8,
              color: 'var(--text)',
              outline: 'none',
              boxSizing: 'border-box',
            }}
          />

          {/* Filter buttons */}
          <div style={{ display: 'flex', gap: 4, marginBottom: 8, background: 'var(--panel2)', border: '1px solid var(--line)', borderRadius: 8, padding: 3, flexShrink: 0 }}>
            <button style={filterBtnStyle('all')} onClick={() => setFilter('all')}>All</button>
            <button style={filterBtnStyle('pending')} onClick={() => setFilter('pending')}>Pending</button>
            <button style={filterBtnStyle('approved')} onClick={() => setFilter('approved')}>Approved</button>
          </div>

          {/* Scrollable agent cards */}
          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6, paddingRight: 2 }}>
            {filtered.length === 0 && (
              <div style={{ color: 'var(--muted-raw)', fontSize: 12, padding: '8px 2px' }}>
                No agents{filter !== 'all' ? ` (${filter})` : ''} yet.
              </div>
            )}
            {filtered.map(a => (
              <AgentSidebarCard
                key={a.agent_id}
                agent={a}
                selected={a.agent_id === selectedId}
                onClick={() => navigate(`/agents?id=${encodeURIComponent(a.agent_id)}`)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Bottom nav: Traces, Alerts */}
      <div style={{ flexShrink: 0, padding: '4px 0' }}>
        <NavItem to="/traces" label="Traces" icon={Activity} />
        <NavItem to="/alerts" label="Alerts" icon={AlertTriangle} badge={0} />
      </div>

      {/* Platform status */}
      <div style={{ padding: '12px 16px', borderTop: '1px solid var(--line)', fontSize: 11, color: 'var(--muted-raw)', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', display: 'inline-block', background: 'var(--green)' }} />
          Platform online
        </div>
        <div style={{ marginTop: 4 }}>Redis · Proxy · Jaeger</div>
      </div>
    </aside>
  )
}
