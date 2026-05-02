import { apiFetch } from './client'

// Time-window tokens accepted by GET /agents/:id/stats?range=...
// Mirrors `_RANGE_SECONDS` in services/orchestrator/routers/agents.py.
export type StatsRange = '1h' | '24h' | '7d'

export type PolicyHealth = 'ok' | 'warn' | 'fail'

export interface DashboardKpi {
  calls: number
  unique_tools: number
  avg_latency_ms: number | null
  critical_alerts: number
  policy_health: PolicyHealth
  total_tokens: number
}

export interface TimeSeriesPoint {
  ts: number // unix seconds
  value: number
}

export interface CategoryBucket {
  label: string
  count: number
  color?: string
}

export interface DashboardPayload {
  kpi: DashboardKpi
  calls_over_time: TimeSeriesPoint[]
  latency_per_call: TimeSeriesPoint[]
  tokens: CategoryBucket[]
  tools: CategoryBucket[]
  a2a: CategoryBucket[]
  alerts: CategoryBucket[]
}

/**
 * GET /agents/:id/stats?range=<range>
 *
 * Returns the per-agent dashboard payload. Server-side range tokens are
 * fixed (`1h`, `24h`, `7d`); see the orchestrator router for the canonical
 * list. Errors are surfaced as `ApiError` from the shared fetch wrapper.
 */
export function getAgentStats(agentId: string, range: StatsRange = '24h') {
  const qs = new URLSearchParams({ range })
  return apiFetch<DashboardPayload>(
    `/agents/${encodeURIComponent(agentId)}/stats?${qs.toString()}`,
  )
}
