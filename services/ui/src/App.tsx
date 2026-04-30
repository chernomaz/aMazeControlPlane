import { Outlet } from 'react-router-dom'
import Sidebar from './components/Sidebar'

export default function App() {
  return (
    <div className="min-h-screen" style={{ background: 'var(--bg)', color: 'var(--text)' }}>
      <Sidebar />
      <main style={{ marginLeft: 230, minHeight: '100vh', padding: '28px 32px' }}>
        <Outlet />
      </main>
    </div>
  )
}
