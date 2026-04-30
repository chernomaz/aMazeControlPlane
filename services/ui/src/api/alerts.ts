import { apiFetch } from './client'

export type AlertRange = '1h' | '24h' | '7d'

export interface AlertReasonCount {
  label: string
  count: number
}

export interface AlertRecord {
  trace_id: string
  agent_id: string
  denial_reason: string
  ts: number
  alert: Record<string, unknown>
  tool: string | null
  target: string | null
}

export interface AlertsResponse {
  total: number
  by_reason: AlertReasonCount[]
  records: AlertRecord[]
}

export interface ListAlertsParams {
  agent?: string
  range?: AlertRange
  groupBy?: 'reason'
  limit?: number
}

export function listAlerts(params: ListAlertsParams = {}) {
  const qs = new URLSearchParams()
  if (params.agent) qs.set('agent', params.agent)
  qs.set('range', params.range ?? '24h')
  qs.set('groupBy', params.groupBy ?? 'reason')
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  return apiFetch<AlertsResponse>(`/alerts?${qs.toString()}`)
}
