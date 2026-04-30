import { apiFetch } from './client'

export interface McpServer {
  name: string
  url: string
  tools: string[]
  approved: boolean
}

export interface McpApproveResponse {
  name: string
  approved: boolean
}

export interface CreateMcpServerInput {
  name: string
  url: string
  tools: string[]
}

export function listMcpServers() {
  return apiFetch<McpServer[]>('/mcp_servers')
}

export function approveMcpServer(name: string) {
  return apiFetch<McpApproveResponse>(`/mcp_servers/${encodeURIComponent(name)}/approve`, {
    method: 'POST',
  })
}

export function rejectMcpServer(name: string) {
  return apiFetch<McpApproveResponse>(`/mcp_servers/${encodeURIComponent(name)}/reject`, {
    method: 'POST',
  })
}

export function createMcpServer(input: CreateMcpServerInput) {
  return apiFetch<McpServer>('/mcp_servers', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
