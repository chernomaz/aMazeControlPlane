const BASE = '/api'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    // Spread options FIRST, then set the merged headers LAST — otherwise
    // `...options` would re-introduce options.headers and clobber the
    // Content-Type default (any caller passing custom headers, e.g. the debug
    // X-Amaze-Debug-User, would otherwise send the body as text/plain → 422).
    ...options,
    // S7: send the amaze_session cookie on every control-plane request.
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (res.status === 204) return undefined as T
  if (res.status === 401 && !path.startsWith('/auth/')) {
    // S7: session missing/expired — bounce to the login page. The /auth/* guard
    // lets a failed login surface its own 401 to the form instead of redirecting.
    if (window.location.pathname !== '/login') {
      window.location.assign('/login')
    }
    throw new ApiError(401, 'not-authenticated')
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const body = await res.json()
      msg = body.detail ?? JSON.stringify(body)
    } catch {
      msg = await res.text().catch(() => res.statusText)
    }
    throw new ApiError(res.status, msg)
  }
  return res.json()
}

const DEBUG_USER_KEY = 'amaze_debug_user'

/**
 * Stable per-browser debug user id. Generated once and persisted in
 * localStorage so each browser gets its own isolated debugger session —
 * multiple users can step-debug the SAME agent independently. Sent as the
 * `X-Amaze-Debug-User` header on the debug endpoints and send-message.
 */
export function getDebugUserId(): string {
  let id = localStorage.getItem(DEBUG_USER_KEY)
  if (!id) {
    id =
      typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : `u-${Math.random().toString(36).slice(2)}-${Date.now()}`
    localStorage.setItem(DEBUG_USER_KEY, id)
  }
  return id
}

export function getWsUrl(sessionId: string) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/sessions/${sessionId}/stream`
}
