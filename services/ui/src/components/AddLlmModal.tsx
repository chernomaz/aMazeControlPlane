import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { ApiError } from '@/api/client'
import { createLlm, type CreateLlmInput, type LlmProvider } from '@/api/llms'

const PROVIDER_OPTIONS: { value: LlmProvider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'gemini', label: 'Google Gemini' },
  { value: 'mistral', label: 'Mistral' },
  { value: 'cohere', label: 'Cohere' },
  { value: 'custom', label: 'Custom / Local' },
]

interface AddLlmModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSuccess?: (msg: string) => void
}

export default function AddLlmModal({ open, onOpenChange, onSuccess }: AddLlmModalProps) {
  const queryClient = useQueryClient()

  const [provider, setProvider] = useState<LlmProvider>('openai')
  const [model, setModel] = useState('')
  const [apiKeyRef, setApiKeyRef] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setProvider('openai')
    setModel('')
    setApiKeyRef('')
    setBaseUrl('')
    setError(null)
  }

  const mutation = useMutation({
    mutationFn: (input: CreateLlmInput) => createLlm(input),
    onSuccess: (entry) => {
      queryClient.invalidateQueries({ queryKey: ['llms'] })
      onSuccess?.(`Added ${entry.provider}/${entry.model}`)
      reset()
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        setError('A model with that name is already registered.')
      } else if (err instanceof Error) {
        setError(err.message)
      } else {
        setError('Failed to add LLM.')
      }
    },
  })

  const trimmedModel = model.trim()
  const trimmedKeyRef = apiKeyRef.trim()
  const trimmedBaseUrl = baseUrl.trim()
  const formValid = !!provider && !!trimmedModel && !!trimmedKeyRef

  // Live LiteLLM preview — mirrors what the backend will append to config/litellm.yaml.
  const previewModel = trimmedModel || '<model>'
  const previewKey = trimmedKeyRef || '<API_KEY_ENV>'
  const yamlLines: string[] = [
    'model_list:',
    `  - model_name: ${previewModel}`,
    `    litellm_params:`,
    `      model: ${provider}/${previewModel}`,
    `      api_key: os.environ/${previewKey}`,
  ]
  if (trimmedBaseUrl) {
    yamlLines.push(`      api_base: ${trimmedBaseUrl}`)
  }
  const yamlPreview = yamlLines.join('\n')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!formValid || mutation.isPending) return
    setError(null)
    mutation.mutate({
      provider,
      model: trimmedModel,
      api_key_ref: trimmedKeyRef,
      base_url: trimmedBaseUrl || undefined,
    })
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      reset()
    }
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-w-3xl"
        style={{
          background: 'var(--panel)',
          border: '1px solid var(--line)',
          color: 'var(--text)',
        }}
      >
        <DialogHeader>
          <DialogTitle>Add LLM Provider</DialogTitle>
          <DialogDescription style={{ color: 'var(--muted-raw)' }}>
            Configure a new model via LiteLLM. The YAML on the right is appended to
            <code style={{ marginLeft: 4, fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}>
              config/litellm.yaml
            </code>
            .
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 20,
              marginTop: 8,
            }}
          >
            {/* Form column */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <div>
                <Label htmlFor="llm-provider">Provider</Label>
                <Select
                  value={provider}
                  onValueChange={(v) => setProvider(v as LlmProvider)}
                >
                  <SelectTrigger id="llm-provider" style={{ marginTop: 6 }}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PROVIDER_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div>
                <Label htmlFor="llm-model">Model name</Label>
                <Input
                  id="llm-model"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="gpt-4.1-mini"
                  style={{ marginTop: 6, fontFamily: 'JetBrains Mono, monospace' }}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>

              <div>
                <Label htmlFor="llm-key-ref">API Key env var</Label>
                <Input
                  id="llm-key-ref"
                  value={apiKeyRef}
                  onChange={(e) => setApiKeyRef(e.target.value)}
                  placeholder="OPENAI_API_KEY"
                  style={{ marginTop: 6, fontFamily: 'JetBrains Mono, monospace' }}
                  autoComplete="off"
                  spellCheck={false}
                />
                <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 4 }}>
                  Reads from container env at request time.
                </div>
              </div>

              <div>
                <Label htmlFor="llm-base-url">Base URL (optional)</Label>
                <Input
                  id="llm-base-url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.openai.com/v1 (leave blank for default)"
                  style={{ marginTop: 6, fontFamily: 'JetBrains Mono, monospace' }}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>

              {error && (
                <div
                  role="alert"
                  style={{
                    background: 'rgba(224, 82, 82, 0.12)',
                    border: '1px solid rgba(224, 82, 82, 0.4)',
                    color: 'var(--red)',
                    borderRadius: 6,
                    padding: '8px 12px',
                    fontSize: 12,
                  }}
                >
                  {error}
                </div>
              )}
            </div>

            {/* Preview column */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: 'var(--muted-raw)',
                  marginBottom: 6,
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                }}
              >
                LiteLLM Config Preview
              </div>
              <pre
                style={{
                  flex: 1,
                  margin: 0,
                  background: '#0a0f1d',
                  border: '1px solid var(--line)',
                  borderRadius: 8,
                  padding: 14,
                  fontFamily: 'JetBrains Mono, monospace',
                  fontSize: 13,
                  lineHeight: 1.55,
                  color: '#cdd9f4',
                  overflow: 'auto',
                  whiteSpace: 'pre',
                  minHeight: 240,
                }}
              >
                {yamlPreview}
              </pre>
              <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 6 }}>
                Appended to <code style={{ fontFamily: 'JetBrains Mono, monospace' }}>config/litellm.yaml</code> on submit.
              </div>
            </div>
          </div>

          <div
            style={{
              display: 'flex',
              justifyContent: 'flex-end',
              gap: 8,
              marginTop: 22,
              paddingTop: 16,
              borderTop: '1px solid var(--line)',
            }}
          >
            <Button
              type="button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!formValid || mutation.isPending}>
              {mutation.isPending ? 'Adding…' : 'Add LLM'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
