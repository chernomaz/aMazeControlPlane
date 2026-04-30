import { apiFetch } from './client'

export type LlmProvider =
  | 'openai'
  | 'anthropic'
  | 'gemini'
  | 'mistral'
  | 'cohere'
  | 'custom'

export interface LlmEntry {
  provider: LlmProvider
  model: string
  base_url?: string
}

export interface CreateLlmInput {
  provider: LlmProvider
  model: string
  api_key_ref: string
  base_url?: string
}

export function listLlms() {
  return apiFetch<LlmEntry[]>('/llms')
}

export function createLlm(input: CreateLlmInput) {
  // Strip empty base_url so the backend treats it as absent.
  const body: CreateLlmInput = { ...input }
  if (!body.base_url || body.base_url.trim() === '') {
    delete body.base_url
  }
  return apiFetch<LlmEntry>('/llms', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
