import { apiFetch } from './client'

export interface McpTool {
  name: string
  description?: string
  inputSchema?: Record<string, unknown>
}

export interface McpServer {
  name: string
  url: string
  tools: McpTool[]
  approved: boolean
}

export interface McpApproveResponse {
  name: string
  approved: boolean
}

export interface CreateMcpServerInput {
  name: string
  url: string
  tools?: string[]  // optional — omit to trigger auto-probe
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
  const body: Record<string, unknown> = { name: input.name, url: input.url }
  if (input.tools && input.tools.length > 0) {
    body.tools = input.tools
  }
  return apiFetch<McpServer>('/mcp_servers', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
