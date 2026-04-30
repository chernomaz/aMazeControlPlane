import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom'
import './index.css'
import App from './App'
import Llms from './pages/Llms'
import McpServers from './pages/McpServers'
import Agents from './pages/Agents'
import AgentPolicy from './pages/AgentPolicy'
import AgentDashboard from './pages/AgentDashboard'
import Traces from './pages/Traces'
import TraceDetail from './pages/TraceDetail'
import Alerts from './pages/Alerts'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
})

const router = createBrowserRouter([
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
      { path: 'traces', element: <Traces /> },
      { path: 'traces/:traceId', element: <TraceDetail /> },
      { path: 'alerts', element: <Alerts /> },
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
