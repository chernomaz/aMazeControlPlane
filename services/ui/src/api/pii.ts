// PII redaction API client + types.
//
// Mirrors services/proxy/policy.py PiiConfig / ToolPiiConfig / PiiRule and
// services/orchestrator/routers/pii.py endpoint contracts. All entity labels
// must match PII_ENTITY_LABELS in the backend — invalid labels return 422.

import { apiFetch } from './client'

// ---------------------------------------------------------------------------
// Canonical entity labels — must match PII_ENTITY_LABELS in policy.py.
// ---------------------------------------------------------------------------

export type PiiEntity =
  | 'EMAIL_ADDRESS'
  | 'CREDIT_CARD'
  | 'PHONE_NUMBER'
  | 'US_SSN'
  | 'IP_ADDRESS'
  | 'PERSON'
  | 'LOCATION'
  | 'URL'
  | 'IBAN_CODE'

export const PII_ENTITIES: PiiEntity[] = [
  'EMAIL_ADDRESS',
  'CREDIT_CARD',
  'PHONE_NUMBER',
  'US_SSN',
  'IP_ADDRESS',
  'PERSON',
  'LOCATION',
  'URL',
  'IBAN_CODE',
]

// Friendlier display labels used by the UI. The wire value stays canonical.
export const PII_LABEL: Record<PiiEntity, string> = {
  EMAIL_ADDRESS: 'Email',
  CREDIT_CARD:   'Credit Card',
  PHONE_NUMBER:  'Phone',
  US_SSN:        'SSN',
  IP_ADDRESS:    'IP Address',
  PERSON:        'Person',
  LOCATION:      'Address',
  URL:           'URL',
  IBAN_CODE:     'IBAN',
}

// ---------------------------------------------------------------------------
// Config shape.
// ---------------------------------------------------------------------------

export interface PiiRule {
  entities: PiiEntity[]
}

export interface ToolPiiConfig {
  input: Record<string, PiiRule>
  output: PiiRule | null
}

export interface PiiConfig {
  enabled: boolean
  tools: Record<string, ToolPiiConfig>
}

// Empty defaults — used when the backend returns 200 but the agent has no
// pii_config yet (the endpoint returns `PiiConfig()` in that case).
export function emptyPiiConfig(): PiiConfig {
  return { enabled: true, tools: {} }
}

export function emptyToolConfig(): ToolPiiConfig {
  return { input: {}, output: null }
}

// ---------------------------------------------------------------------------
// Preview endpoint payload.
// ---------------------------------------------------------------------------

export interface PiiPreviewRequest {
  tool: string
  input: Record<string, unknown>
  input_entities: Record<string, PiiEntity[]>
  response_text: string | null
  output_entities: PiiEntity[]
}

export interface PiiPreviewResponse {
  input: Record<string, unknown>
  response_text: string | null
}

// ---------------------------------------------------------------------------
// Client.
// ---------------------------------------------------------------------------

export function getPiiConfig(agentId: string) {
  return apiFetch<PiiConfig>(`/policy/${encodeURIComponent(agentId)}/pii`)
}

export interface PutPiiResponse {
  updated: boolean
  agent_id: string
  tools: number
}

export function putPiiConfig(agentId: string, config: PiiConfig) {
  return apiFetch<PutPiiResponse>(`/policy/${encodeURIComponent(agentId)}/pii`, {
    method: 'PUT',
    body: JSON.stringify(config),
  })
}

export function previewPii(agentId: string, req: PiiPreviewRequest) {
  return apiFetch<PiiPreviewResponse>(`/policy/${encodeURIComponent(agentId)}/pii/preview`, {
    method: 'POST',
    body: JSON.stringify(req),
  })
}
