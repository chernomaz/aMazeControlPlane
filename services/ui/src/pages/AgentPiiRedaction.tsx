import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, Info } from 'lucide-react'

import { getPolicy, type Policy } from '@/api/policy'
import { listMcpServers, type McpServer } from '@/api/mcp'
import {
  getPiiConfig, putPiiConfig, previewPii,
  emptyPiiConfig, emptyToolConfig,
  PII_ENTITIES, PII_LABEL,
  type PiiConfig, type PiiEntity, type ToolPiiConfig,
} from '@/api/pii'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Union of allowed_tools + graph steps with call_type='tool' — the set of
// tools the agent is currently authorized to call.
function collectTools(policy: Policy): string[] {
  const set = new Set<string>(policy.allowed_tools ?? [])
  for (const step of policy.graph?.steps ?? []) {
    if (step.call_type === 'tool' && step.callee_id) set.add(step.callee_id)
  }
  return Array.from(set).sort()
}

interface ToolSchemaField {
  name: string
  type: string        // "string" | "integer" | ... | "object" | "unknown"
  description?: string
}

interface ToolSchema {
  server: string
  params: ToolSchemaField[]
}

// Extract param name + JSON type from an MCP inputSchema (JSON Schema).
//
// Properties whose `type` field is entirely missing are dropped — those are
// almost always wrapper artifacts, e.g. LangChain/FastMCP's implicit
// RunnableConfig `config` param on tools defined with `@traceable`. Real
// tool params always declare a JSON type.
function schemaFields(schema: Record<string, unknown> | undefined): ToolSchemaField[] {
  if (!schema || typeof schema !== 'object') return []
  const props = (schema as Record<string, Record<string, unknown> | undefined>)['properties']
  if (!props || typeof props !== 'object') return []
  const out: ToolSchemaField[] = []
  for (const [name, prop] of Object.entries(props)) {
    if (!prop || typeof prop !== 'object') continue
    const t = (prop as Record<string, unknown>)['type']
    if (typeof t !== 'string') continue  // wrapper artifact — skip
    const desc = (prop as Record<string, unknown>)['description']
    out.push({
      name,
      type: t,
      description: typeof desc === 'string' ? desc : undefined,
    })
  }
  return out
}

// Map tool name → {server, params} from the flat MCP server list.
function toolSchemaIndex(servers: McpServer[]): Record<string, ToolSchema> {
  const out: Record<string, ToolSchema> = {}
  for (const s of servers) {
    for (const t of s.tools ?? []) {
      out[t.name] = { server: s.name, params: schemaFields(t.inputSchema) }
    }
  }
  return out
}

// PII types are only meaningful for string-shaped fields. `text` is the
// marker used by the response-body row (which is always redactable — the
// backend walks every string in the tool result).
function isRedactable(type: string): boolean {
  return type === 'string' || type === 'text'
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function EntityPills({
  value,
  onChange,
  disabled = false,
}: {
  value: PiiEntity[]
  onChange: (next: PiiEntity[]) => void
  disabled?: boolean
}) {
  const set = new Set(value)
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {PII_ENTITIES.map(e => {
        const on = set.has(e)
        return (
          <button
            key={e}
            type="button"
            disabled={disabled}
            onClick={() => {
              const next = new Set(set)
              if (on) next.delete(e); else next.add(e)
              onChange(PII_ENTITIES.filter(x => next.has(x)))  // canonical order
            }}
            style={{
              padding: '4px 10px',
              borderRadius: 999,
              fontSize: 11.5,
              fontWeight: on ? 700 : 500,
              cursor: disabled ? 'not-allowed' : 'pointer',
              opacity: disabled ? 0.4 : 1,
              border: `1px solid ${on ? 'var(--blue)' : 'var(--line)'}`,
              background: on ? 'var(--blue)' : 'transparent',
              color: on ? 'white' : 'var(--muted-raw)',
            }}
          >
            {PII_LABEL[e]}
          </button>
        )
      })}
    </div>
  )
}

function ParamRow({
  name,
  type,
  desc,
  value,
  onChange,
}: {
  name: string
  type: string
  desc?: string
  value: PiiEntity[]
  onChange: (next: PiiEntity[]) => void
}) {
  const disabled = !isRedactable(type)
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '210px 1fr',
        gap: 16,
        padding: '10px 0',
        borderTop: '1px solid var(--line)',
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <code style={{ fontSize: 12.5, color: 'var(--text)', fontWeight: 500 }}>{name}</code>
        <span
          style={{
            display: 'inline-block',
            width: 'fit-content',
            padding: '1px 6px',
            fontSize: 10,
            fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
            color: 'var(--muted-raw)',
            background: 'rgba(255,255,255,0.05)',
            borderRadius: 3,
          }}
        >
          {type}
        </span>
        {desc && (
          <span style={{ fontSize: 11, color: 'var(--muted-raw)' }}>{desc}</span>
        )}
      </div>
      <div>
        {disabled ? (
          <div style={{ fontSize: 11, color: 'var(--muted-raw)', fontStyle: 'italic', paddingTop: 5 }}>
            Non-string field — nothing to redact.
          </div>
        ) : (
          <EntityPills value={value} onChange={onChange} />
        )}
      </div>
    </div>
  )
}

function ToolCard({
  toolName,
  schema,
  config,
  expanded,
  onToggle,
  onChange,
}: {
  toolName: string
  schema: ToolSchema | undefined
  config: ToolPiiConfig
  expanded: boolean
  onToggle: () => void
  onChange: (next: ToolPiiConfig) => void
}) {
  const params = schema?.params ?? []
  const server = schema?.server

  const totalRedactions =
    Object.values(config.input).reduce((n, r) => n + r.entities.length, 0) +
    (config.output?.entities.length ?? 0)

  return (
    <div
      style={{
        background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
        border: `1px solid ${expanded ? 'rgba(99,102,241,0.28)' : 'var(--line)'}`,
        borderRadius: 12,
        marginBottom: 10,
        overflow: 'hidden',
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        style={{
          width: '100%',
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          background: 'transparent',
          border: 'none',
          color: 'var(--text)',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        <ChevronRight
          size={16}
          style={{
            color: expanded ? 'var(--blue)' : 'var(--muted-raw)',
            transform: expanded ? 'rotate(90deg)' : 'none',
            transition: 'transform 0.15s',
            flex: 'none',
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>{toolName}</div>
          <div style={{ fontSize: 11, color: 'var(--muted-raw)', marginTop: 2 }}>
            {server ? <>MCP server: <code style={{ color: 'var(--muted-raw)' }}>{server}</code></> : 'server: (not in MCP catalog)'}
            {' · '}
            {params.length} param{params.length === 1 ? '' : 's'}
          </div>
        </div>
        <span
          style={{
            padding: '3px 9px',
            borderRadius: 999,
            fontSize: 11,
            fontWeight: 600,
            background: totalRedactions ? 'rgba(99,102,241,0.15)' : 'rgba(255,255,255,0.04)',
            color: totalRedactions ? 'var(--blue)' : 'var(--muted-raw)',
          }}
        >
          {totalRedactions ? `${totalRedactions} redaction${totalRedactions === 1 ? '' : 's'}` : 'no redactions'}
        </span>
      </button>

      {expanded && (
        <div style={{ padding: '8px 18px 18px', borderTop: '1px solid var(--line)' }}>
          {/* Input parameters */}
          <div style={{ marginTop: 8 }}>
            <div style={{
              fontSize: 11, fontWeight: 700, letterSpacing: '0.5px',
              textTransform: 'uppercase', color: 'var(--muted-raw)', marginBottom: 8,
            }}>
              Input parameters
              {schema === undefined && ' (schema unknown — enter manually)'}
            </div>

            {params.length === 0 ? (
              <ManualParamEditor
                config={config}
                onChange={onChange}
              />
            ) : (
              params.map(p => (
                <ParamRow
                  key={p.name}
                  name={p.name}
                  type={p.type}
                  desc={p.description}
                  value={config.input[p.name]?.entities ?? []}
                  onChange={(entities) => {
                    const nextInput = { ...config.input }
                    if (entities.length === 0) delete nextInput[p.name]
                    else nextInput[p.name] = { entities }
                    onChange({ ...config, input: nextInput })
                  }}
                />
              ))
            )}
          </div>

          {/* Output */}
          <div style={{ marginTop: 18 }}>
            <div style={{
              fontSize: 11, fontWeight: 700, letterSpacing: '0.5px',
              textTransform: 'uppercase', color: 'var(--muted-raw)', marginBottom: 8,
            }}>
              Response body (free-form text)
            </div>
            <ParamRow
              name="response body"
              type="text"
              desc="Applied to every string in the tool result"
              value={config.output?.entities ?? []}
              onChange={(entities) => {
                onChange({ ...config, output: entities.length ? { entities } : null })
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// Fallback for tools without a known inputSchema — user types param names.
function ManualParamEditor({
  config,
  onChange,
}: {
  config: ToolPiiConfig
  onChange: (next: ToolPiiConfig) => void
}) {
  const [newName, setNewName] = useState('')
  const entries = Object.entries(config.input)
  return (
    <div>
      {entries.length === 0 && (
        <div style={{ fontSize: 12, color: 'var(--muted-raw)', padding: '4px 0 8px' }}>
          No parameters configured. Add one below.
        </div>
      )}
      {entries.map(([name, rule]) => (
        <ParamRow
          key={name}
          name={name}
          type="string"
          value={rule.entities}
          onChange={(entities) => {
            const next = { ...config.input }
            if (entities.length === 0) delete next[name]
            else next[name] = { entities }
            onChange({ ...config, input: next })
          }}
        />
      ))}
      <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="parameter name"
          style={{
            padding: '5px 9px',
            fontSize: 12,
            background: '#0b1223',
            border: '1px solid var(--line)',
            borderRadius: 6,
            color: 'var(--text)',
            fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
            outline: 'none',
            width: 200,
          }}
        />
        <button
          type="button"
          disabled={!newName.trim() || newName in config.input}
          onClick={() => {
            const key = newName.trim()
            if (!key || key in config.input) return
            onChange({ ...config, input: { ...config.input, [key]: { entities: [] } } })
            setNewName('')
          }}
          style={{
            padding: '5px 12px',
            fontSize: 12,
            borderRadius: 6,
            border: '1px solid var(--line)',
            background: 'transparent',
            color: 'var(--muted-raw)',
            cursor: newName.trim() && !(newName in config.input) ? 'pointer' : 'not-allowed',
          }}
        >
          Add parameter
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Preview panel
// ---------------------------------------------------------------------------

function PreviewPanel({
  agentId,
  toolName,
  config,
}: {
  agentId: string
  toolName: string | null
  config: ToolPiiConfig | null
}) {
  const [inputText, setInputText] = useState('email me at john.doe@acme.com about Jane Smith')
  const [responseText, setResponseText] = useState('Contact ops at 415-555-2019 or ops@acme.com')
  const [redacted, setRedacted] = useState<{ input: string; response: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // Pick the first param that has at least one entity configured — that's
  // the one worth showing in the preview. If no input rules exist, we still
  // show the input textarea but flag the "→ tool" side as "no rules".
  const configuredInputParam = useMemo(() => {
    if (!config) return null
    const entry = Object.entries(config.input).find(([, r]) => r.entities.length > 0)
    return entry ? { name: entry[0], entities: entry[1].entities } : null
  }, [config])

  const outputEntities = config?.output?.entities ?? []
  const hasInputRules = configuredInputParam !== null
  const hasOutputRules = outputEntities.length > 0

  useEffect(() => {
    setRedacted(null)
    setErr(null)
  }, [toolName, configuredInputParam?.entities, outputEntities.join(',')])

  async function runPreview() {
    if (!toolName || !config) return
    setBusy(true); setErr(null)
    try {
      // Only send an input rule for the specific param we're demoing. Fall
      // back to a synthetic "query" key when no input rules are configured
      // (backend gets empty entities → passes through, which is what we
      // want to demonstrate).
      const paramName = configuredInputParam?.name ?? 'query'
      const paramEntities = configuredInputParam?.entities ?? []
      const r = await previewPii(agentId, {
        tool: toolName,
        input: { [paramName]: inputText },
        input_entities: { [paramName]: paramEntities },
        response_text: responseText,
        output_entities: outputEntities,
      })
      setRedacted({
        input: (r.input[paramName] as string) ?? '',
        response: r.response_text ?? '',
      })
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      style={{
        background: 'linear-gradient(180deg, var(--panel) 0%, #0f1627 100%)',
        border: '1px solid var(--line)',
        borderRadius: 12,
        padding: 18,
        marginTop: 20,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
        {toolName ? (
          <>Preview for <code style={{ background: 'rgba(99,102,241,0.15)', color: 'var(--blue)', padding: '1px 6px', borderRadius: 4, fontSize: 12 }}>{toolName}</code></>
        ) : 'Redaction preview'}
      </div>
      <div style={{ fontSize: 12, color: 'var(--muted-raw)', marginBottom: 14 }}>
        {toolName
          ? 'Only the rules configured on this tool apply. Expand a different tool above to see its preview.'
          : 'Expand a tool above to see what its redaction rules would do to a sample payload.'}
      </div>

      {!toolName || !config ? (
        <div style={{
          fontSize: 12, color: 'var(--muted-raw)', fontStyle: 'italic',
          padding: '12px 14px',
          background: '#060a17',
          border: '1px dashed var(--line)',
          borderRadius: 6,
        }}>
          Click a tool card above to preview its rules here. Each tool is previewed independently — rules from one tool never leak into another's preview.
        </div>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <div>
              <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase', color: 'var(--muted-raw)', marginBottom: 6 }}>
                Sample input {configuredInputParam ? `(${configuredInputParam.name})` : ''}
              </div>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                rows={3}
                style={{
                  width: '100%',
                  fontSize: 12,
                  fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
                  background: '#060a17',
                  border: '1px solid var(--line)',
                  borderRadius: 6,
                  padding: 10,
                  color: 'var(--text)',
                  resize: 'vertical',
                  outline: 'none',
                }}
              />
            </div>
            <div>
              <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase', color: 'var(--blue)', marginBottom: 6 }}>
                Redacted input → tool
              </div>
              {hasInputRules ? (
                <pre style={{
                  margin: 0,
                  fontSize: 12,
                  fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
                  background: '#060a17',
                  border: '1px solid var(--line)',
                  borderRadius: 6,
                  padding: 10,
                  color: 'var(--muted-raw)',
                  whiteSpace: 'pre-wrap',
                  minHeight: 66,
                }}>
                  {redacted?.input ?? '—'}
                </pre>
              ) : (
                <div style={{
                  fontSize: 12, color: 'var(--muted-raw)', fontStyle: 'italic',
                  padding: 10, minHeight: 66,
                  background: '#060a17',
                  border: '1px dashed var(--line)',
                  borderRadius: 6,
                }}>
                  No input redaction rules configured for this tool. The tool call would receive the sample input as-is.
                </div>
              )}
            </div>

            <div>
              <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase', color: 'var(--muted-raw)', marginBottom: 6 }}>
                Sample tool response
              </div>
              <textarea
                value={responseText}
                onChange={(e) => setResponseText(e.target.value)}
                rows={3}
                style={{
                  width: '100%',
                  fontSize: 12,
                  fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
                  background: '#060a17',
                  border: '1px solid var(--line)',
                  borderRadius: 6,
                  padding: 10,
                  color: 'var(--text)',
                  resize: 'vertical',
                  outline: 'none',
                }}
              />
            </div>
            <div>
              <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase', color: 'var(--blue)', marginBottom: 6 }}>
                Redacted response → agent
              </div>
              {hasOutputRules ? (
                <pre style={{
                  margin: 0,
                  fontSize: 12,
                  fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
                  background: '#060a17',
                  border: '1px solid var(--line)',
                  borderRadius: 6,
                  padding: 10,
                  color: 'var(--muted-raw)',
                  whiteSpace: 'pre-wrap',
                  minHeight: 66,
                }}>
                  {redacted?.response ?? '—'}
                </pre>
              ) : (
                <div style={{
                  fontSize: 12, color: 'var(--muted-raw)', fontStyle: 'italic',
                  padding: 10, minHeight: 66,
                  background: '#060a17',
                  border: '1px dashed var(--line)',
                  borderRadius: 6,
                }}>
                  No response redaction rules configured for this tool. The agent would receive the sample response as-is.
                </div>
              )}
            </div>
          </div>

          <div style={{ display: 'flex', gap: 10, marginTop: 12, alignItems: 'center' }}>
            <button
              type="button"
              onClick={runPreview}
              disabled={busy}
              style={{
                padding: '6px 14px',
                fontSize: 12,
                fontWeight: 600,
                borderRadius: 6,
                background: busy ? 'transparent' : 'var(--blue)',
                border: `1px solid ${busy ? 'var(--line)' : 'var(--blue)'}`,
                color: busy ? 'var(--muted-raw)' : 'white',
                cursor: busy ? 'wait' : 'pointer',
              }}
            >
              {busy ? 'Running…' : 'Run preview'}
            </button>
            {err && <span style={{ color: '#ef4444', fontSize: 12 }}>{err}</span>}
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function AgentPiiRedaction() {
  const { agentId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()

  const policyQ = useQuery({ queryKey: ['policy', agentId], queryFn: () => getPolicy(agentId) })
  const mcpQ = useQuery({ queryKey: ['mcp_servers'], queryFn: listMcpServers })
  const piiQ = useQuery({ queryKey: ['pii', agentId], queryFn: () => getPiiConfig(agentId) })

  const [pii, setPii] = useState<PiiConfig | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    if (piiQ.data) setPii(piiQ.data)
  }, [piiQ.data])

  const tools: string[] = useMemo(
    () => (policyQ.data ? collectTools(policyQ.data) : []),
    [policyQ.data],
  )

  const schemas = useMemo(
    () => (mcpQ.data ? toolSchemaIndex(mcpQ.data) : {}),
    [mcpQ.data],
  )

  // First render: auto-expand the first configured tool if any.
  useEffect(() => {
    if (expanded || !tools.length) return
    const withRules = tools.find(t => pii?.tools[t] && (
      Object.keys(pii.tools[t].input).length > 0 || (pii.tools[t].output?.entities.length ?? 0) > 0
    ))
    setExpanded(withRules ?? tools[0])
  }, [tools, pii, expanded])

  const initial = piiQ.data ?? emptyPiiConfig()
  const dirty = pii ? JSON.stringify(pii) !== JSON.stringify(initial) : false

  const putM = useMutation({
    mutationFn: (cfg: PiiConfig) => putPiiConfig(agentId, cfg),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pii', agentId] }),
  })

  const isLoading = policyQ.isLoading || mcpQ.isLoading || piiQ.isLoading
  const errorMsg =
    (policyQ.error as Error | null)?.message ??
    (mcpQ.error as Error | null)?.message ??
    (piiQ.error as Error | null)?.message

  const workingPii: PiiConfig = pii ?? emptyPiiConfig()

  return (
    <div style={{ padding: '24px 32px 120px', maxWidth: 1180 }}>
      <button
        onClick={() => navigate('/agents')}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '4px 8px', marginBottom: 12,
          background: 'transparent', border: 'none', cursor: 'pointer',
          color: 'var(--muted-raw)', fontSize: 12,
        }}
      >
        <ArrowLeft size={14} /> Agents
      </button>

      <div style={{ marginBottom: 18 }}>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: '-0.01em' }}>
          {agentId} <span style={{ color: 'var(--muted-raw)', fontWeight: 500, fontSize: 16 }}> · PII Redaction</span>
        </h1>
        <div style={{ fontSize: 12.5, color: 'var(--muted-raw)', marginTop: 4 }}>
          Rules apply on the next tool call. Existing traces are unaffected.
        </div>
      </div>

      {/* Tab bar — mirrors the array in AgentPolicy/AgentDashboard/AgentDebugger. */}
      <div style={{ display: 'flex', borderBottom: '1px solid var(--line)', marginBottom: 20 }}>
        {[
          { label: 'Dashboard', path: `/agents/${encodeURIComponent(agentId)}` },
          { label: 'Policy',    path: `/agents/${encodeURIComponent(agentId)}/policy` },
          { label: 'PII',       path: `/agents/${encodeURIComponent(agentId)}/pii` },
          { label: 'Debugger',  path: `/agents/${encodeURIComponent(agentId)}/debugger` },
        ].map((tab) => {
          const active = location.pathname === tab.path
          return (
            <button
              key={tab.path}
              onClick={() => navigate(tab.path)}
              style={{
                padding: '9px 22px', fontSize: 13, fontWeight: 600,
                border: 'none', background: 'transparent', cursor: 'pointer',
                color: active ? 'var(--blue)' : 'var(--muted-raw)',
                borderBottom: `2px solid ${active ? 'var(--blue)' : 'transparent'}`,
                marginBottom: -1,
              }}
            >
              {tab.label}
            </button>
          )
        })}
      </div>

      {/* Info banner */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 14px',
        background: 'rgba(34,211,238,0.06)',
        border: '1px solid rgba(34,211,238,0.15)',
        borderRadius: 8,
        marginBottom: 20,
        fontSize: 12, color: 'var(--muted-raw)',
      }}>
        <Info size={16} style={{ color: '#22d3ee', flex: 'none' }} />
        <div>
          Redaction runs in the proxy before the tool sees the input <em>and</em> before the response reaches the agent. Powered by Presidio's regex recognizers.
        </div>
      </div>

      {isLoading && <div style={{ color: 'var(--muted-raw)' }}>Loading…</div>}
      {errorMsg && (
        <div style={{ color: '#ef4444', fontSize: 12, marginBottom: 10 }}>Error: {errorMsg}</div>
      )}

      {!isLoading && policyQ.data && (
        <>
          <div style={{
            fontSize: 11, fontWeight: 700, letterSpacing: '0.5px',
            textTransform: 'uppercase', color: 'var(--muted-raw)',
            marginBottom: 10, marginTop: 4,
          }}>
            Tools configured for this agent · {tools.length}
          </div>

          {tools.length === 0 && (
            <div style={{
              fontSize: 12, color: 'var(--muted-raw)',
              padding: '12px 14px',
              background: 'var(--panel)',
              border: '1px solid var(--line)',
              borderRadius: 8,
            }}>
              This agent has no tools configured yet. Add tools on the Policy tab first.
            </div>
          )}

          {tools.map(tool => (
            <ToolCard
              key={tool}
              toolName={tool}
              schema={schemas[tool]}
              config={workingPii.tools[tool] ?? emptyToolConfig()}
              expanded={expanded === tool}
              onToggle={() => setExpanded(expanded === tool ? null : tool)}
              onChange={(next) => {
                setPii(prev => {
                  const base = prev ?? initial
                  const empty = (
                    Object.keys(next.input).length === 0 &&
                    (next.output === null || next.output.entities.length === 0)
                  )
                  const nextTools = { ...base.tools }
                  if (empty) delete nextTools[tool]
                  else nextTools[tool] = next
                  return { ...base, tools: nextTools }
                })
              }}
            />
          ))}

          <PreviewPanel
            agentId={agentId}
            toolName={expanded}
            config={expanded ? (workingPii.tools[expanded] ?? emptyToolConfig()) : null}
          />

          {/* Sticky footer */}
          <div style={{
            position: 'fixed', left: 220, right: 0, bottom: 0,
            background: 'rgba(11,16,32,0.92)',
            backdropFilter: 'blur(8px)',
            borderTop: '1px solid var(--line)',
            padding: '12px 32px',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            zIndex: 20,
          }}>
            <div style={{ fontSize: 12, color: 'var(--muted-raw)' }}>
              {dirty ? 'Unsaved changes · applies on next tool call' : 'All saved.'}
              {putM.error && <span style={{ color: '#ef4444', marginLeft: 12 }}>
                Save failed: {(putM.error as Error).message}
              </span>}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                onClick={() => piiQ.data && setPii(piiQ.data)}
                disabled={!dirty || putM.isPending}
                style={{
                  padding: '6px 14px', fontSize: 12, fontWeight: 600,
                  borderRadius: 6, background: 'transparent',
                  border: '1px solid var(--line)',
                  color: 'var(--muted-raw)',
                  cursor: dirty && !putM.isPending ? 'pointer' : 'not-allowed',
                  opacity: dirty && !putM.isPending ? 1 : 0.5,
                }}
              >
                Reset
              </button>
              <button
                type="button"
                onClick={() => pii && putM.mutate(pii)}
                disabled={!dirty || putM.isPending}
                style={{
                  padding: '6px 14px', fontSize: 12, fontWeight: 700,
                  borderRadius: 6,
                  background: dirty && !putM.isPending ? 'var(--blue)' : 'transparent',
                  border: `1px solid ${dirty && !putM.isPending ? 'var(--blue)' : 'var(--line)'}`,
                  color: dirty && !putM.isPending ? 'white' : 'var(--muted-raw)',
                  cursor: dirty && !putM.isPending ? 'pointer' : 'not-allowed',
                }}
              >
                {putM.isPending ? 'Saving…' : 'Save redaction rules'}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
