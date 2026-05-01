import { apiFetch } from './client'

export type AgentState = 'pending' | 'approved-no-policy' | 'approved-with-policy'

export interface AgentPolicySummary {
  mode: 'strict' | 'flexible'
  allowed_tools?: string[]
  allowed_agents?: string[]
}

export interface Agent {
  agent_id: string
  state: AgentState
  registered_at: number | null
  endpoint: string | null
  policy_summary?: AgentPolicySummary
}

export interface ApproveAgentResponse {
  agent_id: string
  approved: boolean
}

export function listAgents() {
  return apiFetch<Agent[]>('/agents')
}

export function approveAgent(agentId: string) {
  return apiFetch<ApproveAgentResponse>(`/agents/${encodeURIComponent(agentId)}/approve`, {
    method: 'POST',
  })
}

export function rejectAgent(agentId: string) {
  return apiFetch<ApproveAgentResponse>(`/agents/${encodeURIComponent(agentId)}/reject`, {
    method: 'POST',
  })
}

// --- Send message (S4-T3.3) ----------------------------------------------

export interface SendMessageDenialAlert {
  reason: string
  agent_id: string
  alert: Record<string, unknown>
  human: string
  count: number
}

export interface SendMessageResponse {
  /** Verbatim body returned by the agent's /chat endpoint. */
  response: unknown
  /** trace_id of the most recent audit record, if any. */
  trace_id: string | null
  /** Populated when a proxy denial fired during this message. */
  denial: SendMessageDenialAlert | null
}

export function removeAgent(agentId: string) {
  return apiFetch<{ removed: string }>(`/agents/${encodeURIComponent(agentId)}`, {
    method: 'DELETE',
  })
}

export function sendAgentMessage(agentId: string, prompt: string) {
  return apiFetch<SendMessageResponse>(
    `/agents/${encodeURIComponent(agentId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify({ prompt }),
    },
  )
}
