import { NavLink } from 'react-router-dom'
import { Sun, Server, Users, Activity, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/llms', label: 'LLM Providers', icon: Sun },
  { to: '/mcp-servers', label: 'MCP Servers', icon: Server },
  { to: '/agents', label: 'Agents', icon: Users },
  { to: '/traces', label: 'Traces', icon: Activity },
  { to: '/alerts', label: 'Alerts', icon: AlertTriangle, badge: 0 },
]

export default function Sidebar() {
  return (
    <aside
      className="flex flex-col"
      style={{
        width: 230,
        minHeight: '100vh',
        background: 'var(--panel)',
        borderRight: '1px solid var(--line)',
        position: 'fixed',
        top: 0,
        left: 0,
        bottom: 0,
        zIndex: 10,
      }}
    >
      <div style={{ padding: '18px 16px 14px' }}>
        <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: '-0.5px', color: 'var(--blue)' }}>
          aMaze
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 2 }}>Control Plane</div>
      </div>

      <nav style={{ padding: '12px 0', flex: 1 }}>
        {NAV.map(({ to, label, icon: Icon, badge }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                'nav-item',
                'flex items-center gap-2.5 cursor-pointer rounded-lg select-none transition-colors',
                isActive ? 'is-active' : '',
              )
            }
            style={({ isActive }) => ({
              padding: '9px 14px',
              margin: '2px 8px',
              fontSize: 13,
              fontWeight: 500,
              color: isActive ? 'var(--blue)' : 'var(--muted-raw)',
              background: isActive ? 'rgba(93,169,255,.12)' : 'transparent',
              textDecoration: 'none',
            })}
          >
            <Icon size={16} strokeWidth={2} />
            <span>{label}</span>
            {typeof badge === 'number' && badge > 0 && (
              <span
                style={{
                  marginLeft: 'auto',
                  fontSize: 10,
                  padding: '1px 7px',
                  borderRadius: 999,
                  fontWeight: 700,
                  background: 'rgba(224,82,82,.16)',
                  color: '#ffb3b3',
                }}
              >
                {badge}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <div
        style={{
          padding: '12px 16px',
          borderTop: '1px solid var(--line)',
          fontSize: 11,
          color: 'var(--muted-raw)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              display: 'inline-block',
              background: 'var(--green)',
            }}
          />
          Platform online
        </div>
        <div style={{ marginTop: 4 }}>Redis · Proxy · Jaeger</div>
      </div>
    </aside>
  )
}
