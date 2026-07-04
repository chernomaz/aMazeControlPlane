import { apiFetch } from './client'

export interface UserRecord {
  user_id: string
  username: string
  role: string
}

export const listUsers = () => apiFetch<UserRecord[]>('/auth/users')

export const createUser = (username: string, password: string, role: string) =>
  apiFetch<UserRecord>('/auth/users', {
    method: 'POST',
    body: JSON.stringify({ username, password, role }),
  })

export const deleteUser = (userId: string) =>
  apiFetch<void>(`/auth/users/${encodeURIComponent(userId)}`, { method: 'DELETE' })

// Reassign an agent to a single owner (admin-only).
export const assignAgent = (agentId: string, userId: string) =>
  apiFetch<{ agent_id: string; owner: string }>(
    `/agents/${encodeURIComponent(agentId)}/assign`,
    { method: 'POST', body: JSON.stringify({ user_id: userId }) },
  )
