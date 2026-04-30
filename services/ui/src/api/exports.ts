// Typed wrapper for GET /api/export — builds the query string and triggers a
// browser download of the returned ZIP. We deliberately go through fetch (not
// `window.open` / a raw <a href>) so non-2xx responses raise an error the
// caller can surface in the UI rather than dumping JSON into a new tab.

export interface ExportParams {
  /** Optional agent_id filter; omit for "all agents". */
  agent?: string
  /** Window start, unix seconds. */
  start: number
  /** Window end, unix seconds. */
  end: number
  /** Content slugs the backend understands (e.g. ["traces", "audit"]). */
  content: string[]
}

export async function downloadExport(params: ExportParams): Promise<void> {
  const qs = new URLSearchParams()
  if (params.agent) qs.set('agent', params.agent)
  qs.set('start', String(params.start))
  qs.set('end', String(params.end))
  qs.set('content', params.content.join(','))
  qs.set('format', 'zip')

  const resp = await fetch(`/api/export?${qs.toString()}`, { method: 'GET' })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const body = await resp.json()
      detail = body.detail ?? JSON.stringify(body)
    } catch {
      detail = (await resp.text().catch(() => resp.statusText)) || resp.statusText
    }
    throw new Error(`Export failed (${resp.status}): ${detail}`)
  }

  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url

  // Try to honour Content-Disposition; fall back to a sensible default that
  // includes the requested window so two consecutive exports never collide.
  const cd = resp.headers.get('Content-Disposition') ?? ''
  const m = cd.match(/filename="([^"]+)"/) ?? cd.match(/filename=([^;]+)/)
  a.download = (m?.[1] ?? `amaze-export-${params.start}-${params.end}.zip`).trim()

  document.body.appendChild(a)
  a.click()
  a.remove()
  // Defer revoke a tick — Safari occasionally cancels the download otherwise.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
