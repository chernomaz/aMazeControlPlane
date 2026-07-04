import { apiFetch } from './client'

export interface Me {
  user_id: string
  username: string
  role: string
}

export const login = (username: string, password: string) =>
  apiFetch<Me>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })

export const logout = () => apiFetch<void>('/auth/logout', { method: 'POST' })

export const getMe = () => apiFetch<Me>('/auth/me')
