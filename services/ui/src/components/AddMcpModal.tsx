import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog'
import { Button } from './ui/button'
import { Input } from './ui/input'
import { Label } from './ui/label'
import { Textarea } from './ui/textarea'
import { createMcpServer } from '@/api/mcp'
import { ApiError } from '@/api/client'

interface AddMcpModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onToast?: (msg: string, kind: 'success' | 'error') => void
}

export default function AddMcpModal({ open, onOpenChange, onToast }: AddMcpModalProps) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [toolsRaw, setToolsRaw] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName('')
    setUrl('')
    setToolsRaw('')
    setError(null)
  }

  const mutation = useMutation({
    mutationFn: createMcpServer,
    onSuccess: (server) => {
      qc.invalidateQueries({ queryKey: ['mcp_servers'] })
      onToast?.(`${server.name} added`, 'success')
      reset()
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        setError(`A server named "${name}" already exists.`)
      } else if (err instanceof ApiError && err.status === 502) {
        setError('Could not reach the server to probe tools. Check the URL and try again.')
      } else {
        const msg = err instanceof Error ? err.message : 'Failed to add server'
        setError(msg)
        onToast?.(msg, 'error')
      }
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    const trimmedName = name.trim()
    const trimmedUrl = url.trim()
    const tools = toolsRaw
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)

    if (!trimmedName) {
      setError('Name is required.')
      return
    }
    if (!trimmedUrl) {
      setError('URL is required.')
      return
    }
    if (!/^https?:\/\//i.test(trimmedUrl)) {
      setError('URL must start with http:// or https://')
      return
    }

    mutation.mutate({
      name: trimmedName,
      url: trimmedUrl,
      tools: tools.length > 0 ? tools : undefined,
    })
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) reset()
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        style={{
          background: 'var(--panel)',
          borderColor: 'var(--line)',
          color: 'var(--text)',
        }}
      >
        <DialogHeader>
          <DialogTitle>Add MCP Server</DialogTitle>
          <DialogDescription style={{ color: 'var(--muted-raw)' }}>
            Manually register an MCP server. Leave the Tools field blank to auto-probe available tools.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Label htmlFor="mcp-name">Name</Label>
            <Input
              id="mcp-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="web-tools"
              autoComplete="off"
              required
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Label htmlFor="mcp-url">URL</Label>
            <Input
              id="mcp-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://web-tools-mcp:8200/sse"
              autoComplete="off"
              required
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Label htmlFor="mcp-tools">Tools (comma-separated)</Label>
            <Textarea
              id="mcp-tools"
              value={toolsRaw}
              onChange={(e) => setToolsRaw(e.target.value)}
              placeholder="web_search, fetch_url"
              rows={3}
            />
            <span style={{ fontSize: 11, color: 'var(--muted-raw)' }}>
              Leave blank to auto-probe tools from the server.
            </span>
          </div>

          {error && (
            <div
              style={{
                fontSize: 12,
                color: 'var(--red)',
                background: 'rgba(224,82,82,.1)',
                border: '1px solid rgba(224,82,82,.3)',
                borderRadius: 6,
                padding: '8px 10px',
              }}
            >
              {error}
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Adding…' : 'Add Server'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
