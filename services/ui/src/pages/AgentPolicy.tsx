import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Plus, Trash2 } from 'lucide-react'

import { listAgents } from '@/api/agents'
import { listMcpServers } from '@/api/mcp'
import {
  getPolicy,
  putPolicy,
  type Graph,
  type Policy,
  type PolicyMode,
  type RateLimit,
  type ViolationAction,
} from '@/api/policy'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { GraphEditor, validateGraph } from '@/components/GraphEditor'

// LLM providers known to the policy schema (services/proxy/policy.py).
const LLM_PROVIDERS = ['openai', 'anthropic', 'gemini', 'mistral', 'cohere', 'custom'] as const

const RATE_WINDOW_RE = /^\d+[smh]$/

interface Toast {
  id: number
  msg: string
  kind: 'success' | 'error'
}

function defaultPolicy(agentId: string): Policy {
  return {
    name: agentId,
    max_tokens_per_turn: 0,
    max_tool_calls_per_turn: 0,
    max_agent_calls_per_turn: 0,
    allowed_llm_providers: [],
    token_rate_limits: [],
    on_budget_exceeded: 'block',
    on_violation: 'block',
    mode: 'flexible',
    allowed_tools: [],
    allowed_agents: [],
    graph: null,
  }
}

// Deep equality good enough for the policy shape (no functions, no Dates).
function isEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export default function AgentPolicy() {
  const { agentId = '' } = useParams<{ agentId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [toasts, setToasts] = useState<Toast[]>([])

  const pushToast = (msg: string, kind: 'success' | 'error' = 'success') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, msg, kind }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500)
  }

  // ── Queries ────────────────────────────────────────────────────────────
  const policyQ = useQuery({
    queryKey: ['policy', agentId],
    queryFn: () => getPolicy(agentId),
    enabled: !!agentId,
    retry: false,
  })

  const agentsQ = useQuery({ queryKey: ['agents'], queryFn: listAgents })
  const mcpQ = useQuery({ queryKey: ['mcp_servers'], queryFn: listMcpServers })

  // Local edit state — mirrors the loaded policy. 404 → show a fresh default
  // (lets the operator create a policy for an approved-no-policy agent).
  const [draft, setDraft] = useState<Policy | null>(null)

  useEffect(() => {
    if (policyQ.data) {
      setDraft(policyQ.data)
    } else if (policyQ.isError && (policyQ.error as { status?: number } | null)?.status === 404) {
      setDraft(defaultPolicy(agentId))
    }
  }, [policyQ.data, policyQ.isError, policyQ.error, agentId])

  // ── Derived collections for checklists ────────────────────────────────
  const allTools = useMemo(() => {
    const seen = new Set<string>()
    const list: string[] = []
    for (const s of mcpQ.data ?? []) {
      if (!s.approved) continue
      for (const t of s.tools) {
        if (!seen.has(t)) {
          seen.add(t)
          list.push(t)
        }
      }
    }
    return list.sort()
  }, [mcpQ.data])

  const allAgents = useMemo(() => {
    return (agentsQ.data ?? [])
      .filter((a) => a.agent_id !== agentId)
      .map((a) => a.agent_id)
      .sort()
  }, [agentsQ.data, agentId])

  // ── Validation ─────────────────────────────────────────────────────────
  const errors = useMemo(() => {
    const out: { field: string; msg: string }[] = []
    if (!draft) return out

    if (!Number.isInteger(draft.max_tokens_per_turn) || draft.max_tokens_per_turn < 0) {
      out.push({ field: 'max_tokens_per_turn', msg: 'must be an integer ≥ 0' })
    }
    if (!Number.isInteger(draft.max_tool_calls_per_turn) || draft.max_tool_calls_per_turn < 0) {
      out.push({ field: 'max_tool_calls_per_turn', msg: 'must be an integer ≥ 0' })
    }
    if (!Number.isInteger(draft.max_agent_calls_per_turn) || draft.max_agent_calls_per_turn < 0) {
      out.push({ field: 'max_agent_calls_per_turn', msg: 'must be an integer ≥ 0' })
    }

    draft.token_rate_limits.forEach((r, i) => {
      if (!RATE_WINDOW_RE.test(r.window)) {
        out.push({ field: `rate.${i}.window`, msg: `row ${i + 1}: window must match \\d+[smh] (e.g. 10m, 1h)` })
      }
      if (!Number.isInteger(r.max_tokens) || r.max_tokens <= 0) {
        out.push({ field: `rate.${i}.max_tokens`, msg: `row ${i + 1}: max_tokens must be > 0` })
      }
    })

    if (draft.mode === 'strict') {
      if (!draft.graph) {
        out.push({ field: 'graph', msg: 'strict mode requires an execution graph' })
      } else {
        const result = validateGraph(draft.graph)
        if (!result.ok) {
          for (const e of result.errors) out.push({ field: 'graph', msg: e })
        }
      }
    }

    return out
  }, [draft])

  const isDirty = useMemo(() => {
    if (!draft) return false
    return !isEqual(draft, policyQ.data ?? defaultPolicy(agentId))
  }, [draft, policyQ.data, agentId])

  const isValid = errors.length === 0

  // ── Save ──────────────────────────────────────────────────────────────
  const save = useMutation({
    mutationFn: (p: Policy) => putPolicy(agentId, p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['policy', agentId] })
      qc.invalidateQueries({ queryKey: ['agents'] })
      pushToast('Policy saved — live on next request', 'success')
      setTimeout(() => navigate('/agents'), 600)
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : 'Save failed'
      pushToast(`Save failed: ${msg}`, 'error')
    },
  })

  const onSave = () => {
    if (!draft || !isValid) return
    // Strip `graph` when mode is flexible — the backend ignores it but a
    // null is the cleanest payload for `extra="forbid"` validation.
    const payload: Policy = {
      ...draft,
      graph: draft.mode === 'strict' ? draft.graph : null,
    }
    save.mutate(payload)
  }

  // ── Render guards ─────────────────────────────────────────────────────
  if (!agentId) {
    return <ErrorBanner msg="Missing agent_id in URL." />
  }
  if (policyQ.isLoading) {
    return <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>Loading policy…</div>
  }
  const errStatus = (policyQ.error as { status?: number } | null)?.status
  if (policyQ.isError && errStatus !== 404) {
    return <ErrorBanner msg={`Failed to load policy: ${(policyQ.error as Error)?.message ?? 'unknown'}`} />
  }
  if (!draft) {
    return <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>Loading policy…</div>
  }

  // ── Mutators ───────────────────────────────────────────────────────────
  const update = (patch: Partial<Policy>) => setDraft((d) => (d ? { ...d, ...patch } : d))
  const toggleProvider = (p: string) =>
    update({
      allowed_llm_providers: draft.allowed_llm_providers.includes(p)
        ? draft.allowed_llm_providers.filter((x) => x !== p)
        : [...draft.allowed_llm_providers, p],
    })
  const toggleTool = (t: string) =>
    update({
      allowed_tools: draft.allowed_tools.includes(t)
        ? draft.allowed_tools.filter((x) => x !== t)
        : [...draft.allowed_tools, t],
    })
  const toggleAgent = (a: string) =>
    update({
      allowed_agents: draft.allowed_agents.includes(a)
        ? draft.allowed_agents.filter((x) => x !== a)
        : [...draft.allowed_agents, a],
    })

  const addRateRow = () =>
    update({ token_rate_limits: [...draft.token_rate_limits, { window: '10m', max_tokens: 1000 }] })
  const removeRateRow = (i: number) =>
    update({ token_rate_limits: draft.token_rate_limits.filter((_, idx) => idx !== i) })
  const editRateRow = (i: number, patch: Partial<RateLimit>) =>
    update({
      token_rate_limits: draft.token_rate_limits.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
    })

  const setMode = (m: PolicyMode) => {
    if (m === draft.mode) return
    if (m === 'strict') {
      // Provide an empty starter graph if none yet — the GraphEditor needs
      // something to render and a single start node is the minimum.
      const graph: Graph = draft.graph ?? { start_step: 1, steps: [] }
      update({ mode: 'strict', graph })
    } else {
      update({ mode: 'flexible' })
    }
  }

  // ── Layout ─────────────────────────────────────────────────────────────
  return (
    <div style={{ paddingBottom: 88 /* leave room for sticky save bar */ }}>
      {/* Top bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 }}>
        <Button variant="ghost" size="sm" onClick={() => navigate('/agents')} style={{ gap: 6 }}>
          <ArrowLeft size={14} /> Back
        </Button>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px' }}>
            Policy ·{' '}
            <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', color: 'var(--blue)' }}>
              {agentId}
            </span>
          </h1>
          <p style={{ color: 'var(--muted-raw)', fontSize: 13, marginTop: 2 }}>
            Edits go straight to Redis. Policies are picked up on the next proxy request — no restart.
          </p>
        </div>
      </div>

      {/* Card 1: Enforcement Mode */}
      <Card title="Enforcement Mode">
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'auto 1fr 1fr',
            gap: 24,
            alignItems: 'flex-start',
          }}
        >
          <div>
            <SectionLabel>Mode</SectionLabel>
            <RadioGroup
              value={draft.mode}
              options={[
                { value: 'strict', label: 'Strict' },
                { value: 'flexible', label: 'Flexible' },
              ]}
              onChange={(v) => setMode(v as PolicyMode)}
            />
            <Hint>Strict = ordered graph. Flexible = allowed set, any order.</Hint>
          </div>
          <div>
            <SectionLabel>On Violation</SectionLabel>
            <RadioGroup
              value={draft.on_violation}
              options={[
                { value: 'block', label: 'Block', danger: true },
                { value: 'allow', label: 'Allow' },
              ]}
              onChange={(v) => update({ on_violation: v as ViolationAction })}
            />
          </div>
          <div>
            <SectionLabel>On Budget Exceeded</SectionLabel>
            <RadioGroup
              value={draft.on_budget_exceeded}
              options={[
                { value: 'block', label: 'Block', danger: true },
                { value: 'allow', label: 'Allow' },
              ]}
              onChange={(v) => update({ on_budget_exceeded: v as ViolationAction })}
            />
          </div>
        </div>
      </Card>

      {/* Card 2: Per-turn Limits */}
      <Card title="Per-turn Limits" subtitle="A turn = one session lifetime. 0 = unlimited.">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
          <NumberField
            label="Max Tokens / Turn"
            value={draft.max_tokens_per_turn}
            onChange={(n) => update({ max_tokens_per_turn: n })}
            error={errors.find((e) => e.field === 'max_tokens_per_turn')?.msg}
            placeholder="0 = unlimited"
          />
          <NumberField
            label="Max Tool Calls / Turn"
            value={draft.max_tool_calls_per_turn}
            onChange={(n) => update({ max_tool_calls_per_turn: n })}
            error={errors.find((e) => e.field === 'max_tool_calls_per_turn')?.msg}
            placeholder="0 = unlimited"
          />
          <NumberField
            label="Max Agent Calls / Turn"
            value={draft.max_agent_calls_per_turn}
            onChange={(n) => update({ max_agent_calls_per_turn: n })}
            error={errors.find((e) => e.field === 'max_agent_calls_per_turn')?.msg}
            placeholder="0 = unlimited"
          />
        </div>
      </Card>

      {/* Card 3: Allowed LLM Providers */}
      <Card title="Allowed LLM Providers" subtitle="Empty list = all providers blocked.">
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
          {LLM_PROVIDERS.map((p) => (
            <Checkbox
              key={p}
              label={p}
              checked={draft.allowed_llm_providers.includes(p)}
              onChange={() => toggleProvider(p)}
              accent="var(--blue)"
              mono
            />
          ))}
        </div>
      </Card>

      {/* Card 4: Rate Limits */}
      <Card title="Token Rate Limits" subtitle="Token budgets per rolling time window (RedisTimeSeries).">
        {draft.token_rate_limits.length === 0 ? (
          <div style={{ color: 'var(--muted-raw)', fontSize: 13, marginBottom: 10 }}>
            No rate limits configured.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 8 }}>
            {draft.token_rate_limits.map((r, i) => {
              const winErr = errors.find((e) => e.field === `rate.${i}.window`)?.msg
              const tokErr = errors.find((e) => e.field === `rate.${i}.max_tokens`)?.msg
              return (
                <div
                  key={i}
                  style={{ display: 'grid', gridTemplateColumns: '120px 1fr 36px', gap: 8, alignItems: 'start' }}
                >
                  <div>
                    <NativeInput
                      value={r.window}
                      onChange={(v) => editRateRow(i, { window: v })}
                      placeholder="10m"
                      invalid={!!winErr}
                    />
                    {winErr && <FieldError msg={winErr} />}
                  </div>
                  <div>
                    <NativeInput
                      type="number"
                      value={String(r.max_tokens)}
                      onChange={(v) => editRateRow(i, { max_tokens: Number(v) || 0 })}
                      placeholder="max tokens"
                      invalid={!!tokErr}
                    />
                    {tokErr && <FieldError msg={tokErr} />}
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => removeRateRow(i)}
                    style={{ borderColor: 'var(--red)', color: 'var(--red)', height: 36 }}
                    title="Remove row"
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              )
            })}
          </div>
        )}
        <Button variant="ghost" size="sm" onClick={addRateRow}>
          <Plus size={14} /> Add window
        </Button>
      </Card>

      {/* Card 5: mode-conditional */}
      {draft.mode === 'flexible' ? (
        <Card title="Allowed Capabilities" subtitle="Checklists below come from approved MCP servers and approved peer agents.">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <Checklist
              title="Allowed Tools"
              accent="var(--cyan)"
              items={allTools}
              empty={
                mcpQ.isLoading
                  ? 'Loading tools…'
                  : 'No tools published by approved MCP servers yet.'
              }
              isChecked={(t) => draft.allowed_tools.includes(t)}
              onToggle={toggleTool}
            />
            <Checklist
              title="Allowed Agents"
              accent="var(--violet)"
              items={allAgents}
              empty={agentsQ.isLoading ? 'Loading agents…' : 'No other agents registered.'}
              isChecked={(a) => draft.allowed_agents.includes(a)}
              onToggle={toggleAgent}
            />
          </div>
        </Card>
      ) : (
        <Card title="Execution Graph" subtitle="Strict mode — every call must follow this ordered graph.">
          <div style={{ minHeight: 360 }}>
            <GraphEditor
              value={draft.graph ?? { start_step: 1, steps: [] }}
              onChange={(g: Graph) => update({ graph: g })}
              allowedTools={allTools}
              allowedAgents={allAgents}
            />
          </div>
        </Card>
      )}

      {/* Inline error block above the sticky save bar */}
      {errors.length > 0 && (
        <div
          style={{
            background: 'rgba(224,82,82,.08)',
            border: '1px solid rgba(224,82,82,.3)',
            borderRadius: 10,
            padding: 12,
            color: '#ffb3b3',
            fontSize: 12,
            marginTop: 4,
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: 6 }}>
            {errors.length} validation issue{errors.length === 1 ? '' : 's'}:
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {errors.map((e, i) => (
              <li key={i}>{e.msg}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Sticky bottom action bar */}
      <div
        style={{
          position: 'fixed',
          bottom: 0,
          left: 230,
          right: 0,
          padding: '12px 32px',
          background: 'rgba(11,16,32,.92)',
          borderTop: '1px solid var(--line)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-end',
          gap: 10,
          zIndex: 50,
        }}
      >
        {!isDirty && <Badge variant="outline">No changes</Badge>}
        {isDirty && isValid && <Badge variant="warning">Unsaved changes</Badge>}
        <Button variant="ghost" onClick={() => navigate('/agents')} disabled={save.isPending}>
          Cancel
        </Button>
        <Button onClick={onSave} disabled={!isDirty || !isValid || save.isPending}>
          {save.isPending ? 'Saving…' : 'Save policy'}
        </Button>
      </div>

      {/* Toast stack */}
      <div
        style={{
          position: 'fixed',
          bottom: 80,
          right: 24,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
          zIndex: 100,
        }}
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            style={{
              padding: '10px 14px',
              borderRadius: 10,
              fontSize: 13,
              fontWeight: 500,
              background: t.kind === 'success' ? 'rgba(31,191,117,.18)' : 'rgba(224,82,82,.18)',
              border: `1px solid ${t.kind === 'success' ? 'var(--green)' : 'var(--red)'}`,
              color: t.kind === 'success' ? '#7be3a8' : '#ffb3b3',
              minWidth: 220,
              boxShadow: '0 8px 24px rgba(0,0,0,.4)',
            }}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Local sub-components
// ─────────────────────────────────────────────────────────────────────

function Card({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <div
      style={{
        background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
        border: '1px solid var(--line)',
        borderRadius: 14,
        padding: 18,
        marginBottom: 14,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: '0.5px',
          textTransform: 'uppercase',
          color: 'var(--muted-raw)',
          marginBottom: 4,
        }}
      >
        {title}
      </div>
      {subtitle && (
        <div style={{ fontSize: 12, color: 'var(--muted-raw)', marginBottom: 12 }}>{subtitle}</div>
      )}
      {!subtitle && <div style={{ marginBottom: 12 }} />}
      {children}
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: '0.5px',
        textTransform: 'uppercase',
        color: 'var(--muted-raw)',
        marginBottom: 6,
      }}
    >
      {children}
    </div>
  )
}

function Hint({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 6, maxWidth: 220 }}>
      {children}
    </div>
  )
}

function RadioGroup<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string; danger?: boolean }[]
  onChange: (v: T) => void
}) {
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--line)', borderRadius: 8, padding: 2 }}>
      {options.map((o) => {
        const active = o.value === value
        const baseColor = o.danger && active ? 'var(--red)' : 'var(--blue)'
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            style={{
              padding: '6px 14px',
              fontSize: 12,
              fontWeight: 600,
              borderRadius: 6,
              cursor: 'pointer',
              background: active ? `${baseColor === 'var(--red)' ? 'rgba(224,82,82,.18)' : 'rgba(93,169,255,.16)'}` : 'transparent',
              color: active ? baseColor : 'var(--muted-raw)',
              border: 'none',
              transition: 'background-color .12s, color .12s',
            }}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

function NumberField({
  label,
  value,
  onChange,
  error,
  placeholder,
}: {
  label: string
  value: number
  onChange: (n: number) => void
  error?: string
  placeholder?: string
}) {
  return (
    <div>
      <SectionLabel>{label}</SectionLabel>
      <NativeInput
        type="number"
        value={String(value)}
        onChange={(v) => onChange(Number.parseInt(v, 10) || 0)}
        placeholder={placeholder}
        invalid={!!error}
      />
      {error && <FieldError msg={error} />}
    </div>
  )
}

function NativeInput({
  type = 'text',
  value,
  onChange,
  placeholder,
  invalid,
}: {
  type?: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  invalid?: boolean
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: '100%',
        padding: '8px 10px',
        fontSize: 13,
        background: 'rgba(11,16,32,.55)',
        border: `1px solid ${invalid ? 'var(--red)' : 'var(--line)'}`,
        borderRadius: 8,
        color: 'var(--text)',
        fontFamily: type === 'number' ? 'JetBrains Mono, ui-monospace, monospace' : undefined,
        outline: 'none',
      }}
    />
  )
}

function FieldError({ msg }: { msg: string }) {
  return (
    <div style={{ fontSize: 11, color: 'var(--red)', marginTop: 4 }}>
      {msg}
    </div>
  )
}

function Checkbox({
  label,
  checked,
  onChange,
  accent = 'var(--blue)',
  mono,
}: {
  label: string
  checked: boolean
  onChange: () => void
  accent?: string
  mono?: boolean
}) {
  return (
    <label
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 10px',
        background: checked ? 'rgba(255,255,255,.04)' : 'transparent',
        border: '1px solid var(--line)',
        borderRadius: 8,
        cursor: 'pointer',
        fontSize: 13,
        userSelect: 'none',
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        style={{ accentColor: accent, cursor: 'pointer' }}
      />
      <span style={{ fontFamily: mono ? 'JetBrains Mono, ui-monospace, monospace' : undefined, color: accent }}>
        {label}
      </span>
    </label>
  )
}

function Checklist({
  title,
  accent,
  items,
  empty,
  isChecked,
  onToggle,
}: {
  title: string
  accent: string
  items: string[]
  empty: string
  isChecked: (s: string) => boolean
  onToggle: (s: string) => void
}) {
  return (
    <div
      style={{
        background: 'rgba(11,16,32,.45)',
        border: '1px solid var(--line)',
        borderRadius: 10,
        padding: 12,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: '0.5px',
          textTransform: 'uppercase',
          color: accent,
          marginBottom: 8,
        }}
      >
        {title}
      </div>
      {items.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--muted-raw)' }}>{empty}</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {items.map((item) => (
            <label
              key={item}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '5px 0',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              <input
                type="checkbox"
                checked={isChecked(item)}
                onChange={() => onToggle(item)}
                style={{ accentColor: accent }}
              />
              <span style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace', color: accent }}>
                {item}
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

function ErrorBanner({ msg }: { msg: string }) {
  return (
    <div
      style={{
        background: 'rgba(224,82,82,.08)',
        border: '1px solid rgba(224,82,82,.3)',
        borderRadius: 10,
        padding: 14,
        color: 'var(--red)',
        marginBottom: 16,
        fontSize: 13,
      }}
    >
      {msg}
    </div>
  )
}
