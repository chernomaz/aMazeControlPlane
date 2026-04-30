import { apiFetch } from './client'

export type TraceStatus = 'passed' | 'failed'

export interface TraceListItem {
  trace_id: string
  agent_id: string
  started_at: number | string
  duration_ms: number
  llm_calls: number
  tool_calls: number
  a2a_calls: number
  total_tokens: number
  violations: number
  status: TraceStatus
}

export interface TraceListResponse {
  traces: TraceListItem[]
  next_cursor: string | null
}

export interface ListTracesParams {
  agent?: string
  limit?: number
  offset?: string
}

export function listTraces(params: ListTracesParams = {}) {
  const qs = new URLSearchParams()
  if (params.agent) qs.set('agent', params.agent)
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', params.offset)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch<TraceListResponse>(`/traces${suffix}`)
}

export type SequenceStepStatus = 'real' | 'mocked' | 'assertion' | 'failed'

export interface SequenceStep {
  from: string
  to: string
  label: string
  status: SequenceStepStatus
}

export interface TraceSummary {
  prompt: string
  final_answer: string
  failure_details?: string | null
  policy_snapshot: unknown
}

export interface ToolBreakdownEntry {
  tool: string
  count: number
}

export interface TraceMetrics {
  total_tokens: number
  llm_calls: number
  tool_calls: number
  a2a_calls: number
  violations: number
  tool_breakdown: ToolBreakdownEntry[]
}

export interface TraceEdge {
  turn: number
  index: number
  ts: number | string
  type: string
  name: string
  indirect: boolean
  source: string
  model: string
  duration_ms: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  status: string
  input: string
  output: string
}

export interface TraceViolation {
  kind: string
  name: string
  turn: number | null
  index: number | null
  status: string
  details: string
}

export interface TraceDetail {
  trace_id: string
  title: string
  passed: boolean
  duration: number | string
  agent_id: string
  started_at: number | string
  ended_at: number | string
  summary: TraceSummary
  metrics: TraceMetrics
  sequence_steps: SequenceStep[]
  edges: TraceEdge[]
  violations_list: TraceViolation[]
}

export function getTrace(traceId: string) {
  return apiFetch<TraceDetail>(`/traces/${encodeURIComponent(traceId)}`)
}
