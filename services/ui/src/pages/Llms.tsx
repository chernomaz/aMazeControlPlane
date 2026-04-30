import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { listLlms, type LlmEntry, type LlmProvider } from '@/api/llms'
import AddLlmModal from '@/components/AddLlmModal'

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  gemini: 'Google',
  mistral: 'Mistral',
  cohere: 'Cohere',
  custom: 'Custom',
}

// Map provider → Badge variant. Mock uses blue/violet/cyan/etc — we approximate
// with the limited variant set the shared Badge component exposes.
function badgeVariantForProvider(p: string): 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline' {
  switch (p) {
    case 'openai':
      return 'default'
    case 'anthropic':
      return 'secondary'
    case 'gemini':
      return 'success'
    case 'mistral':
      return 'warning'
    case 'cohere':
      return 'destructive'
    default:
      return 'outline'
  }
}

const ALL = '__all__'

export default function Llms() {
  const [modalOpen, setModalOpen] = useState(false)
  const [providerFilter, setProviderFilter] = useState<string>(ALL)
  const [toast, setToast] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery<LlmEntry[]>({
    queryKey: ['llms'],
    queryFn: listLlms,
    refetchInterval: 5000,
  })

  const llms = data ?? []

  const providers = useMemo(() => {
    const set = new Set<string>()
    llms.forEach((m) => set.add(m.provider))
    return Array.from(set).sort()
  }, [llms])

  const filtered = useMemo(() => {
    if (providerFilter === ALL) return llms
    return llms.filter((m) => m.provider === providerFilter)
  }, [llms, providerFilter])

  // Auto-dismiss toast after 3s.
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3000)
    return () => clearTimeout(t)
  }, [toast])

  return (
    <div>
      {/* Top bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          justifyContent: 'space-between',
          marginBottom: 24,
          gap: 16,
        }}
      >
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>
            LLM Providers
          </h1>
          <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
            Configure language models via LiteLLM — one interface for all providers.
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ minWidth: 180 }}>
            <Select value={providerFilter} onValueChange={setProviderFilter}>
              <SelectTrigger>
                <SelectValue placeholder="All providers" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All providers</SelectItem>
                {providers.map((p) => (
                  <SelectItem key={p} value={p}>
                    {PROVIDER_LABELS[p as LlmProvider] ?? p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button onClick={() => setModalOpen(true)}>
            <Plus size={14} />
            Add LLM
          </Button>
        </div>
      </div>

      {/* List card */}
      <div
        style={{
          background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
          border: '1px solid var(--line)',
          borderRadius: 14,
          overflow: 'hidden',
        }}
      >
        {isLoading ? (
          <div style={{ padding: 28, color: 'var(--muted-raw)', fontSize: 13 }}>
            Loading…
          </div>
        ) : error ? (
          <div style={{ padding: 28, color: 'var(--red)', fontSize: 13 }}>
            Failed to load LLMs: {(error as Error).message}
          </div>
        ) : llms.length === 0 ? (
          <div
            style={{
              padding: '40px 28px',
              color: 'var(--muted-raw)',
              fontSize: 13,
              textAlign: 'center',
            }}
          >
            No LLM models configured yet. Click "Add LLM" to register one.
          </div>
        ) : filtered.length === 0 ? (
          <div style={{ padding: 28, color: 'var(--muted-raw)', fontSize: 13 }}>
            No models match the selected provider.
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--line)' }}>
                <Th>Provider</Th>
                <Th>Model</Th>
                <Th>Base URL</Th>
                <Th style={{ width: 200, textAlign: 'right' }}>Actions</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((m) => (
                <tr
                  key={`${m.provider}/${m.model}`}
                  style={{ borderBottom: '1px solid var(--line)' }}
                >
                  <Td>
                    <Badge variant={badgeVariantForProvider(m.provider)}>
                      {PROVIDER_LABELS[m.provider as LlmProvider] ?? m.provider}
                    </Badge>
                  </Td>
                  <Td style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}>
                    {m.model}
                  </Td>
                  <Td
                    style={{
                      fontFamily: 'JetBrains Mono, monospace',
                      fontSize: 12,
                      color: m.base_url ? 'var(--text)' : 'var(--muted-raw)',
                    }}
                  >
                    {m.base_url ?? 'default'}
                  </Td>
                  <Td style={{ textAlign: 'right' }}>
                    <div style={{ display: 'inline-flex', gap: 6 }}>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled
                        title="S4-Phase 6"
                      >
                        Test
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled
                        title="S4-Phase 6"
                      >
                        Disable
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <AddLlmModal
        open={modalOpen}
        onOpenChange={setModalOpen}
        onSuccess={(msg) => setToast(msg)}
      />

      {/* Toast */}
      {toast && (
        <div
          role="status"
          style={{
            position: 'fixed',
            bottom: 24,
            right: 24,
            background: 'var(--panel)',
            border: '1px solid var(--line)',
            color: 'var(--text)',
            borderRadius: 10,
            padding: '12px 18px',
            fontSize: 13,
            fontWeight: 600,
            boxShadow: '0 8px 24px rgba(0,0,0,.4)',
            zIndex: 999,
          }}
        >
          {toast}
        </div>
      )}
    </div>
  )
}

function Th({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <th
      style={{
        textAlign: 'left',
        padding: '12px 16px',
        fontWeight: 600,
        fontSize: 11,
        textTransform: 'uppercase',
        letterSpacing: '0.5px',
        color: 'var(--muted-raw)',
        ...style,
      }}
    >
      {children}
    </th>
  )
}

function Td({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return <td style={{ padding: '12px 16px', verticalAlign: 'middle', ...style }}>{children}</td>
}
