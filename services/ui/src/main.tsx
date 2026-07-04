import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom'
import './index.css'
import App from './App'
import Login from './pages/Login'
import RequireAuth from './components/RequireAuth'
import Llms from './pages/Llms'
import McpServers from './pages/McpServers'
import Agents from './pages/Agents'
import AgentPolicy from './pages/AgentPolicy'
import AgentDashboard from './pages/AgentDashboard'
import AgentDebugger from './pages/AgentDebugger'
import Traces from './pages/Traces'
import TraceDetail from './pages/TraceDetail'
import Alerts from './pages/Alerts'
import Users from './pages/Users'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
})

const router = createBrowserRouter([
  // Login lives outside the app shell (no sidebar) and outside the auth guard.
  { path: '/login', element: <Login /> },
  // Everything else requires an authenticated session. RequireAuth resolves
  // GET /auth/me; on 401 it redirects to /login, otherwise it renders the
  // app shell (App = Sidebar + Outlet) and the child routes below.
  {
    element: <RequireAuth />,
    children: [
      {
        path: '/',
        element: <App />,
        children: [
          { index: true, element: <Navigate to="/agents" replace /> },
          { path: 'llms', element: <Llms /> },
          { path: 'mcp-servers', element: <McpServers /> },
          { path: 'agents', element: <Agents /> },
          { path: 'agents/:agentId', element: <AgentDashboard /> },
          { path: 'agents/:agentId/policy', element: <AgentPolicy /> },
          { path: 'agents/:agentId/debugger', element: <AgentDebugger /> },
          { path: 'traces', element: <Traces /> },
          { path: 'traces/:traceId', element: <TraceDetail /> },
          { path: 'alerts', element: <Alerts /> },
          { path: 'users', element: <Users /> },
        ],
      },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
