import { apiFetch } from './client'

export interface DebugStep {
  step_id: string
  agent: string        // which agent made this call (may differ from primary for A2A peers)
  phase: 'request' | 'response'
  kind: 'llm' | 'mcp' | 'a2a'
  target: string
  tool: string
  body: string
  status: 'paused' | 'continued'
}

export interface DebugCurrentResponse {
  paused: boolean
  step: DebugStep | null
  history: DebugStep[]
}

export const enableDebug  = (agentId: string, enabled: boolean) =>
  apiFetch<void>(`/agents/${encodeURIComponent(agentId)}/debug`, {
    method: 'PUT', body: JSON.stringify({ enabled }),
  })

export const getDebugCurrent = (agentId: string) =>
  apiFetch<DebugCurrentResponse>(`/agents/${encodeURIComponent(agentId)}/debug/current`)

export const postDebugNext = (agentId: string, step_id: string, override?: string) =>
  apiFetch<void>(`/agents/${encodeURIComponent(agentId)}/debug/next`, {
    method: 'POST', body: JSON.stringify({ step_id, override: override ?? null }),
  })

export const postDebugSkipAll = (agentId: string) =>
  apiFetch<void>(`/agents/${encodeURIComponent(agentId)}/debug/skip-all`, { method: 'POST' })
