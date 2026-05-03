// Policy API client + types.
//
// T3.2 stubbed the Graph/Step types so the GraphEditor could compile in
// isolation. T3.1 (this file) extends with the full Policy schema and
// the GET / PUT /policy/{agent_id} client. Type names below are kept
// stable for T3.2's GraphEditor:
//   export type CallType
//   export interface Step
//   export interface Graph
//
// Mirrors services/proxy/policy.py (Pydantic v2). `extra="forbid"` on the
// backend means we MUST send only the fields it accepts — never include
// stray UI-only state in the payload.

import { apiFetch } from './client'

// ---------------------------------------------------------------------------
// Graph types — kept compatible with T3.2's stub.
// ---------------------------------------------------------------------------

export type CallType = 'tool' | 'agent' | 'llm'

export interface Step {
  step_id: number
  call_type: CallType
  callee_id: string
  max_loops: number
  next_steps: number[]
}

export interface Graph {
  start_step: number
  steps: Step[]
}

// ---------------------------------------------------------------------------
// Policy schema (full).
// ---------------------------------------------------------------------------

export type PolicyMode = 'strict' | 'flexible'
export type ViolationAction = 'block' | 'allow'

export interface RateLimit {
  window: string // e.g. "10m", "1h", "30s" — `^\d+[smh]$`
  max_tokens: number
}

export interface Policy {
  name: string
  max_tokens_per_turn: number
  max_tool_calls_per_turn: number
  max_agent_calls_per_turn: number
  allowed_llm_providers: string[]
  token_rate_limits: RateLimit[]
  on_budget_exceeded: ViolationAction
  on_violation: ViolationAction
  mode: PolicyMode
  allowed_tools: string[]
  allowed_agents: string[]
  graph: Graph | null
}

export interface PutPolicyResponse {
  updated: boolean
  agent_id: string
}

// ---------------------------------------------------------------------------
// Client.
// ---------------------------------------------------------------------------

export function getPolicy(agentId: string) {
  return apiFetch<Policy>(`/policy/${encodeURIComponent(agentId)}`)
}

export function putPolicy(agentId: string, policy: Policy) {
  return apiFetch<PutPolicyResponse>(`/policy/${encodeURIComponent(agentId)}`, {
    method: 'PUT',
    body: JSON.stringify(policy),
  })
}
