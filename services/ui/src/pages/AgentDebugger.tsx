import { useState, useRef, useEffect } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import { ArrowLeft, Bug, Send } from 'lucide-react'
import { Button } from '@/components/ui/button'
import SequenceDiagram from '@/components/SequenceDiagram'
import type { SequenceStep } from '@/api/traces'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { enableDebug, getDebugCurrent, postDebugNext, postDebugSkipAll } from '@/api/debug'
import { sendAgentMessage } from '@/api/agents'
import type { DebugStep } from '@/api/debug'

// ── Types ──────────────────────────────────────────────────────────────────

type DebugKind  = 'LLM' | 'TOOL' | 'AGENT'
type DebugPhase = 'REQUEST' | 'RESPONSE'
type DebugState = 'idle' | 'waiting' | 'paused' | 'done'

interface DebugCheckpoint {
  kind: DebugKind
  phase: DebugPhase
  label: string
  from: string
  to: string
  completedCount: number  // diagram shows steps 0..completedCount-1 as real
  pausedIndex: number     // this step is highlighted in the diagram
  editLabel: string       // label above the editable textarea
  editContent: string     // initial editable content
  editIsJson: boolean     // true → monospace JSON styling in the textarea
  readonly?: ReadonlyItem[] // read-only context blocks above the editor
}

// Maps ALL_STEPS index → CHECKPOINTS index (steps that have a breakpoint)
const STEP_TO_CHECKPOINT: Record<number, number> = { 3: 0, 4: 1, 5: 2, 6: 3 }

// ── Mock sequence steps (full conversation) ────────────────────────────────
// Each checkpoint controls how many are *visible* in the diagram.

const ALL_STEPS: SequenceStep[] = [
  { from: 'user',            to: 'agent-sdk',        label: 'message',                status: 'real' },
  { from: 'agent-sdk',       to: 'llm',              label: '① openai/gpt-4.1-mini',  status: 'real' },
  { from: 'llm',             to: 'agent-sdk',        label: 'tool_calls: web_search', status: 'real' },
  { from: 'agent-sdk',       to: 'tool:web_search',  label: 'web_search · args',      status: 'real' },
  { from: 'tool:web_search', to: 'agent-sdk',        label: 'web_search · result',    status: 'real' },
  { from: 'agent-sdk',       to: 'llm',              label: '② openai/gpt-4.1-mini',  status: 'real' },
  { from: 'llm',             to: 'agent-sdk',        label: '② response',             status: 'real' },
  { from: 'agent-sdk',       to: 'agent:agent-sdk1', label: 'agent-sdk1 · input',     status: 'real' },
  { from: 'agent:agent-sdk1',to: 'agent-sdk',        label: 'agent-sdk1 · output',    status: 'real' },
  { from: 'agent-sdk',       to: 'user',             label: 'reply',                  status: 'real' },
]

const CHECKPOINTS: DebugCheckpoint[] = [
  {
    kind: 'TOOL', phase: 'REQUEST',
    label: 'web_search · args', from: 'agent-sdk', to: 'tool:web_search',
    completedCount: 3, pausedIndex: 3,
    editLabel: 'Tool arguments (editable)',
    editIsJson: true,
    editContent: JSON.stringify({ query: 'latest Claude 4 benchmarks', num_results: 5 }, null, 2),
    readonly: [
      { label: 'method', content: 'tools/call  ·  tool: web_search' },
    ],
  },
  {
    kind: 'TOOL', phase: 'RESPONSE',
    label: 'web_search · result', from: 'tool:web_search', to: 'agent-sdk',
    completedCount: 4, pausedIndex: 4,
    editLabel: 'Result text (editable)',
    editIsJson: false,
    editContent: 'Claude 4 Sonnet achieves 72.5% on HumanEval, 89.3% on MMLU, outperforming GPT-4o on code generation tasks by 8.2%.',
    readonly: [
      { label: 'isError', content: 'false' },
    ],
  },
  {
    kind: 'LLM', phase: 'REQUEST',
    label: '② LLM request', from: 'agent-sdk', to: 'llm',
    completedCount: 5, pausedIndex: 5,
    editLabel: 'User message (editable)',
    editIsJson: false,
    editContent: 'What are the latest Claude 4 benchmarks?',
    readonly: [
      { label: 'system', content: 'You are a helpful research assistant.' },
      { label: 'history (2 prior turns)', content: '[assistant] tool_calls: web_search\n[tool] result: Claude 4 Sonnet achieves…' },
    ],
  },
  {
    kind: 'LLM', phase: 'RESPONSE',
    label: '② LLM response', from: 'llm', to: 'agent-sdk',
    completedCount: 6, pausedIndex: 6,
    editLabel: 'Assistant message (editable)',
    editIsJson: false,
    editContent: 'Based on recent benchmarks, Claude 4 Sonnet achieves impressive results: 72.5% on HumanEval and 89.3% on MMLU, outperforming GPT-4o on code tasks by ~8%.',
    readonly: [
      { label: 'model', content: 'gpt-4.1-mini' },
      { label: 'usage', content: 'prompt: 156 · completion: 98 · total: 254 tokens' },
    ],
  },
]

// Keep mock data available for reference (unused in live flow)
void STEP_TO_CHECKPOINT; void ALL_STEPS; void CHECKPOINTS

// ── Helpers ────────────────────────────────────────────────────────────────

function kindFromStep(s: DebugStep): DebugKind {
  if (s.kind === 'llm') return 'LLM'
  if (s.kind === 'a2a') return 'AGENT'
  return 'TOOL'
}

function phaseFromStep(s: DebugStep): DebugPhase {
  return s.phase === 'request' ? 'REQUEST' : 'RESPONSE'
}

function editLabelFor(s: DebugStep): string {
  const k = s.kind
  const p = s.phase
  if (k === 'mcp' && p === 'request') return 'Tool arguments'
  if (k === 'mcp' && p === 'response') return 'Result text'
  if (k === 'llm' && p === 'request') return 'User message'
  if (k === 'llm' && p === 'response') return 'Assistant message'
  if (k === 'a2a' && p === 'request') return 'Agent input'
  return 'Agent output'
}

function editIsJsonFor(s: DebugStep): boolean {
  // mcp request args are JSON; a2a bodies are JSON; llm message text is plain
  if (s.kind === 'mcp' && s.phase === 'request') return true
  if (s.kind === 'a2a') return true
  return false
}

// Extract just the editable portion from the raw body
function extractEditContent(s: DebugStep): string {
  try {
    const b = JSON.parse(s.body)
    if (s.kind === 'llm' && s.phase === 'request') {
      const msgs: {role:string;content:string}[] = b.messages ?? []
      const lastUser = [...msgs].reverse().find(m => m.role === 'user')
      return lastUser?.content ?? s.body
    }
    if (s.kind === 'llm' && s.phase === 'response') {
      return b.choices?.[0]?.message?.content ?? s.body
    }
    if (s.kind === 'mcp' && s.phase === 'request') {
      const args = b.params?.arguments
      return args !== undefined ? JSON.stringify(args, null, 2) : s.body
    }
    if (s.kind === 'mcp' && s.phase === 'response') {
      const content = b.result?.content
      if (Array.isArray(content)) {
        const texts = content.filter((c: {type:string}) => c.type === 'text')
        if (texts.length) return texts.map((c: {text:string}) => c.text).join('\n')
      }
      return b.result !== undefined ? JSON.stringify(b.result, null, 2) : s.body
    }
  } catch { /* not JSON */ }
  return s.body
}

// Reconstruct the full body with the user's edited portion substituted back
function buildOverride(s: DebugStep, edited: string): string {
  try {
    const b = JSON.parse(s.body)
    if (s.kind === 'llm' && s.phase === 'request') {
      const msgs = [...(b.messages ?? [])]
      const idx = [...msgs].map((m,i)=>({m,i})).reverse().find(({m})=>m.role==='user')?.i
      if (idx !== undefined) msgs[idx] = { ...msgs[idx], content: edited }
      return JSON.stringify({ ...b, messages: msgs })
    }
    if (s.kind === 'llm' && s.phase === 'response') {
      const choices = [...(b.choices ?? [])]
      if (choices[0]) choices[0] = { ...choices[0], message: { ...choices[0].message, content: edited } }
      return JSON.stringify({ ...b, choices })
    }
    if (s.kind === 'mcp' && s.phase === 'request') {
      const args = JSON.parse(edited)
      return JSON.stringify({ ...b, params: { ...b.params, arguments: args } })
    }
    if (s.kind === 'mcp' && s.phase === 'response') {
      const content = b.result?.content
      if (Array.isArray(content)) {
        const newContent = content.map((c: {type:string;text?:string}, i: number) =>
          i === 0 && c.type === 'text' ? { ...c, text: edited } : c
        )
        return JSON.stringify({ ...b, result: { ...b.result, content: newContent } })
      }
    }
  } catch { /* not JSON */ }
  return edited
}

interface ReadonlyItem { label: string; content: string }

// Build collapsible read-only context from the raw body
function extractReadonlyContext(s: DebugStep): ReadonlyItem[] {
  try {
    const b = JSON.parse(s.body)
    if (s.kind === 'llm' && s.phase === 'request') {
      const msgs: {role:string;content:string}[] = b.messages ?? []
      const lastUserIdx = [...msgs].map((m,i)=>({m,i})).reverse().find(({m})=>m.role==='user')?.i ?? -1
      const items: ReadonlyItem[] = []
      const sys = msgs.find(m => m.role === 'system')
      if (sys) items.push({ label: 'system', content: sys.content })
      const hist = msgs.filter((_,i) => i !== lastUserIdx && msgs[i]?.role !== 'system')
      if (hist.length) items.push({ label: `history (${hist.length} msg)`, content: hist.map(m=>`[${m.role}] ${m.content}`).join('\n\n') })
      return items
    }
    if (s.kind === 'llm' && s.phase === 'response') {
      const items: ReadonlyItem[] = []
      if (b.model) items.push({ label: 'model', content: b.model })
      const u = b.usage
      if (u) items.push({ label: 'usage', content: `prompt: ${u.prompt_tokens} · completion: ${u.completion_tokens} · total: ${u.total_tokens} tokens` })
      return items
    }
    if (s.kind === 'mcp' && s.phase === 'request') {
      const items: ReadonlyItem[] = []
      if (b.method) items.push({ label: 'method', content: b.method + (s.tool ? `  ·  tool: ${s.tool}` : '') })
      return items
    }
    if (s.kind === 'mcp' && s.phase === 'response') {
      const items: ReadonlyItem[] = []
      if (b.result?.isError !== undefined) items.push({ label: 'isError', content: String(b.result.isError) })
      return items
    }
  } catch { /* not JSON */ }
  return []
}

function liveStepToSequenceStep(s: DebugStep, agentLabel: string): SequenceStep {
  // Use step.agent if set (peer agent routed into primary's session),
  // otherwise fall back to the primary agent label.
  const caller = s.agent || agentLabel
  const from = s.phase === 'request' ? caller : (s.target || s.kind)
  const to   = s.phase === 'request' ? (s.target || s.kind) : caller
  const label = s.tool ? `${s.tool} · ${s.phase}` : `${s.kind} · ${s.phase}`
  return { from, to, label, status: 'real' }
}

// ── Badges ─────────────────────────────────────────────────────────────────

function KindBadge({ kind, small = false }: { kind: DebugKind; small?: boolean }) {
  const cfg = {
    LLM:   { color: 'var(--blue)',   bg: 'rgba(93,169,255,.14)',  border: 'rgba(93,169,255,.3)' },
    TOOL:  { color: 'var(--cyan)',   bg: 'rgba(67,209,198,.14)',  border: 'rgba(67,209,198,.3)' },
    AGENT: { color: 'var(--violet)', bg: 'rgba(169,124,255,.14)', border: 'rgba(169,124,255,.3)' },
  }[kind]
  return (
    <span style={{
      fontSize: small ? 9 : 11, fontWeight: 700,
      padding: small ? '1px 5px' : '2px 9px', borderRadius: 4,
      background: cfg.bg, color: cfg.color, border: `1px solid ${cfg.border}`,
      letterSpacing: '.06em', flexShrink: 0,
    }}>
      {kind}
    </span>
  )
}

function PhaseBadge({ phase, small = false }: { phase: DebugPhase; small?: boolean }) {
  const cfg = {
    REQUEST:  { color: 'var(--yellow)', bg: 'rgba(242,185,75,.14)',  border: 'rgba(242,185,75,.3)' },
    RESPONSE: { color: 'var(--green)',  bg: 'rgba(31,191,117,.14)', border: 'rgba(31,191,117,.3)' },
  }[phase]
  return (
    <span style={{
      fontSize: small ? 9 : 11, fontWeight: 700,
      padding: small ? '1px 5px' : '2px 9px', borderRadius: 4,
      background: cfg.bg, color: cfg.color, border: `1px solid ${cfg.border}`,
      letterSpacing: '.06em', flexShrink: 0,
    }}>
      {phase}
    </span>
  )
}

// ── Inspector panel ────────────────────────────────────────────────────────

interface InspectorProps {
  step: DebugStep
  agentId: string
  stepNum: number
  totalSteps: number
  editContent: string
  isModified: boolean
  isSaved: boolean
  onEdit: (v: string) => void
  onSave: () => void
  onNext: () => void
  onSkipAll: () => void
}

function InspectorPanel({ step, agentId, stepNum, totalSteps, editContent, isModified, isSaved, onEdit, onSave, onNext, onSkipAll }: InspectorProps) {
  const kind = kindFromStep(step)
  const phase = phaseFromStep(step)
  const label = editLabelFor(step)
  const editIsJson = editIsJsonFor(step)
  const readonlyItems = extractReadonlyContext(step)
  const [expandedCtx, setExpandedCtx] = useState<Set<number>>(new Set())
  const toggleCtx = (i: number) =>
    setExpandedCtx(prev => { const s = new Set(prev); s.has(i) ? s.delete(i) : s.add(i); return s })

  const caller    = step.agent || agentId || 'agent'
  const fromLabel = step.phase === 'request' ? caller : (step.target || step.kind)
  const toLabel   = step.phase === 'request' ? (step.target || step.kind) : caller

  return (
    <div style={{
      flex: '0 0 42%', display: 'flex', flexDirection: 'column',
      background: 'var(--panel2)', overflow: 'hidden',
    }}>
      {/* Scrollable content area */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '18px 18px 0' }}>

        {/* Step header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ display: 'flex', gap: 6 }}>
            <KindBadge kind={kind} />
            <PhaseBadge phase={phase} />
          </div>
          <span style={{ fontSize: 11, color: 'var(--muted-raw)', fontVariantNumeric: 'tabular-nums' }}>
            Step {stepNum + 1} of {totalSteps}
          </span>
        </div>

        <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text)', marginBottom: 2 }}>
          {step.tool ? `${step.tool} · ${step.phase}` : `${step.kind} · ${step.phase}`}
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted-raw)', fontFamily: 'JetBrains Mono, monospace', marginBottom: 14 }}>
          {fromLabel} → {toLabel}
        </div>

        {/* Read-only context blocks */}
        {readonlyItems.map((item, i) => (
          <div key={i} style={{ marginBottom: 8 }}>
            <button
              onClick={() => toggleCtx(i)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6, width: '100%',
                background: 'var(--panel)', border: '1px solid var(--line)',
                borderRadius: expandedCtx.has(i) ? '6px 6px 0 0' : 6,
                padding: '5px 10px', cursor: 'pointer', textAlign: 'left',
              }}
            >
              <span style={{ fontSize: 10, color: 'var(--muted-raw)', transform: expandedCtx.has(i) ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}>▶</span>
              <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--muted-raw)', flex: 1 }}>{item.label}</span>
              <span style={{ fontSize: 10, color: 'var(--muted-raw)' }}>{expandedCtx.has(i) ? 'collapse' : 'expand'}</span>
            </button>
            {expandedCtx.has(i) && (
              <div style={{
                background: '#0d1628', border: '1px solid var(--line)', borderTop: 'none',
                borderRadius: '0 0 6px 6px', padding: '8px 10px',
                fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
                color: 'var(--muted-raw)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                lineHeight: 1.6,
              }}>
                {item.content}
              </div>
            )}
          </div>
        ))}

        {/* Editable field label */}
        <div style={{
          fontSize: 11, fontWeight: 700, color: 'var(--muted-raw)',
          textTransform: 'uppercase', letterSpacing: '.07em',
          margin: '14px 0 6px',
        }}>
          {label}
          {isSaved && (
            <span style={{ marginLeft: 8, color: 'var(--green)', fontWeight: 600, textTransform: 'none', letterSpacing: 0 }}>
              ✓ saved
            </span>
          )}
        </div>

        {/* Editable textarea */}
        <div style={{ position: 'relative', marginBottom: 14 }}>
          {isModified && !isSaved && (
            <span style={{
              position: 'absolute', top: 7, right: 8, zIndex: 1,
              fontSize: 10, padding: '1px 6px', fontWeight: 700, letterSpacing: '.05em',
              background: 'rgba(242,185,75,.18)', color: 'var(--yellow)',
              border: '1px solid rgba(242,185,75,.35)', borderRadius: 4,
            }}>
              unsaved
            </span>
          )}
          <textarea
            value={editContent}
            onChange={e => onEdit(e.target.value)}
            spellCheck={false}
            style={{
              width: '100%', minHeight: editIsJson ? 160 : 100, resize: 'vertical',
              background: '#0d1628',
              border: `1px solid ${isModified && !isSaved ? 'var(--yellow)' : isSaved ? 'var(--green)' : 'var(--line)'}`,
              borderRadius: 7, padding: '9px 11px',
              fontFamily: editIsJson ? 'JetBrains Mono, ui-monospace, monospace' : 'inherit',
              fontSize: editIsJson ? 11.5 : 13,
              lineHeight: 1.6, color: 'var(--text)', outline: 'none',
              boxSizing: 'border-box', transition: 'border-color .15s',
            }}
          />
        </div>
      </div>

      {/* Sticky footer — always visible */}
      <div style={{
        padding: '12px 18px', borderTop: '1px solid var(--line)',
        background: 'var(--panel2)', display: 'flex', flexDirection: 'column', gap: 8,
      }}>
        {/* Save row — always visible; active only when there are unsaved edits */}
        <Button
          onClick={onSave}
          disabled={!isModified || isSaved}
          variant="ghost"
          style={{
            width: '100%', fontSize: 13, fontWeight: 700,
            border: `1px solid ${isModified && !isSaved ? 'var(--yellow)' : 'var(--line)'}`,
            color: isModified && !isSaved ? 'var(--yellow)' : 'var(--muted-raw)',
            opacity: isModified && !isSaved ? 1 : 0.45,
            transition: 'border-color .15s, color .15s, opacity .15s',
          }}
        >
          {isSaved ? '✓ Saved' : 'Save Changes'}
        </Button>
        {/* Next / skip row */}
        <div style={{ display: 'flex', gap: 8 }}>
          <Button
            variant="ghost"
            onClick={onSkipAll}
            style={{ flex: 1, border: '1px solid var(--line)', fontSize: 13 }}
          >
            Skip All
          </Button>
          <Button
            onClick={onNext}
            style={{ flex: 2, background: 'var(--blue)', color: '#0b1020', fontWeight: 700, fontSize: 13 }}
          >
            ▶ Next Step
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── Read-only inspector (shown in done state when user clicks a step) ──────

interface ReadOnlyInspectorProps {
  history: DebugStep[]
  selectedStepIdx: number | null
}

function ReadOnlyInspector({ history, selectedStepIdx }: ReadOnlyInspectorProps) {
  if (selectedStepIdx === null || !history[selectedStepIdx]) {
    return (
      <div style={{
        flex: '0 0 42%', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        background: 'var(--panel2)', gap: 10, padding: 32,
      }}>
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--muted-raw)" strokeWidth="1.5" style={{ opacity: .4 }}>
          <polyline points="9 18 15 12 9 6"/>
        </svg>
        <div style={{ fontSize: 13, color: 'var(--muted-raw)', textAlign: 'center', maxWidth: 220 }}>
          Click an arrow in the diagram to inspect that step.
        </div>
      </div>
    )
  }

  const step = history[selectedStepIdx]
  const kind = kindFromStep(step)
  const phase = phaseFromStep(step)
  const label = editLabelFor(step)
  const editIsJson = editIsJsonFor(step)

  return (
    <div style={{
      flex: '0 0 42%', display: 'flex', flexDirection: 'column',
      background: 'var(--panel2)', overflow: 'hidden',
    }}>
      <div style={{ flex: 1, overflowY: 'auto', padding: '18px 18px 12px' }}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ display: 'flex', gap: 6 }}>
            <KindBadge kind={kind} />
            <PhaseBadge phase={phase} />
          </div>
          <span style={{ fontSize: 10, padding: '2px 8px', borderRadius: 4, background: 'rgba(31,191,117,.12)', color: 'var(--green)', border: '1px solid rgba(31,191,117,.3)', fontWeight: 700 }}>
            ✓ completed
          </span>
        </div>
        <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text)', marginBottom: 2 }}>
          {step.tool ? `${step.tool} · ${step.phase}` : `${step.kind} · ${step.phase}`}
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted-raw)', fontFamily: 'JetBrains Mono, monospace', marginBottom: 14 }}>
          {step.target}
        </div>

        {/* Content field label */}
        <div style={{
          fontSize: 11, fontWeight: 700, color: 'var(--muted-raw)',
          textTransform: 'uppercase', letterSpacing: '.07em', margin: '14px 0 6px',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          {label}
        </div>
        {/* Content field (read-only) */}
        <div style={{
          background: '#0d1628', border: '1px solid var(--line)', borderRadius: 7,
          padding: '9px 11px', fontFamily: editIsJson ? 'JetBrains Mono, monospace' : 'inherit',
          fontSize: editIsJson ? 11.5 : 13, lineHeight: 1.6, color: 'var(--text)',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>
          {step.body}
        </div>
      </div>
    </div>
  )
}

// ── Sequence panel ─────────────────────────────────────────────────────────

interface SeqPanelProps {
  steps: SequenceStep[]
  pausedLabel: string | null
  pausedLabelColor?: string
  selectedIndex: number | null
  onSelectStep: (i: number) => void
}

function SequencePanel({ steps, pausedLabel, pausedLabelColor, selectedIndex, onSelectStep }: SeqPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Scroll to the bottom whenever a new step is appended so the latest arrow
  // is always visible. All previous arrows remain visible above — the user
  // can scroll up to review them at any time.
  const prevLenRef = useRef(0)
  useEffect(() => {
    if (steps.length <= prevLenRef.current) {
      prevLenRef.current = steps.length
      return
    }
    prevLenRef.current = steps.length
    const container = containerRef.current
    if (!container) return
    container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' })
  }, [steps.length])

  return (
    <div ref={containerRef} style={{
      flex: '0 0 58%', display: 'flex', flexDirection: 'column',
      background: 'var(--panel)', borderRight: '1px solid var(--line)',
      overflowY: 'auto', padding: '18px 14px 18px 18px',
    }}>
      <div style={{
        fontSize: 10, fontWeight: 700, color: 'var(--muted-raw)',
        textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 10,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span>Conversation Sequence</span>
        {steps.length > 0 && (
          <span style={{ fontWeight: 500, letterSpacing: 0, textTransform: 'none', fontSize: 11 }}>
            {steps.length} step{steps.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>
      {pausedLabel && (() => {
        const color  = pausedLabelColor ?? 'var(--yellow)'
        const bg     = pausedLabelColor ? 'rgba(31,191,117,.07)' : 'rgba(242,185,75,.07)'
        const border = pausedLabelColor ? 'rgba(31,191,117,.28)' : 'rgba(242,185,75,.28)'
        return (
          <div style={{
            background: bg, border: `1px solid ${border}`,
            borderRadius: 8, padding: '7px 11px', marginBottom: 12,
            fontSize: 12, color, fontWeight: 600,
            display: 'flex', alignItems: 'center', gap: 7,
          }}>
            <span>{pausedLabel}</span>
          </div>
        )
      })()}
      <div style={{ overflowX: 'auto' }}>
        {steps.length > 0
          ? <SequenceDiagram steps={steps} selectedIndex={selectedIndex} onSelectStep={onSelectStep} />
          : <div style={{ color: 'var(--muted-raw)', fontSize: 13 }}>Waiting for first call…</div>
        }
      </div>
    </div>
  )
}

// ── Shared debugger body ───────────────────────────────────────────────────

export function DebuggerPanel({ embedded = false, agentId = '' }: { embedded?: boolean; agentId?: string }) {
  const [msgInput, setMsgInput]             = useState('')
  const [sessionActive, setSessionActive]   = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const queryClient = useQueryClient()

  // Live step editor state
  const [editContent, setEditContent]       = useState('')
  const [isModified, setIsModified]         = useState(false)
  const [isSaved, setIsSaved]               = useState(false)
  const [selectedStep, setSelectedStep]     = useState<number | null>(null)

  // Track which step_id is currently being edited (prevents stale editor on step change)
  const [activeStepId, setActiveStepId]     = useState<string | null>(null)

  // afterNext: stay in 'waiting' between steps so UI doesn't flash to 'done'
  const [afterNext, setAfterNext]           = useState(false)
  const afterNextTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // How many consecutive polls have returned liveStep=null while afterNext=true.
  // When this hits 5 (≈5 s) we give up and show 'done' — handles the case where
  // the agent finishes but sendAgentMessage stays pending (proxy holding a connection).
  const noStepPollCountRef = useRef(0)

  // Cleanup timer on unmount
  useEffect(() => () => {
    if (afterNextTimerRef.current) clearTimeout(afterNextTimerRef.current)
  }, [])

  // Poll GET /debug/current every 1s — also serves as keepalive for the enabled key
  const { data: debugData } = useQuery({
    queryKey: ['debug', agentId],
    queryFn: () => getDebugCurrent(agentId),
    refetchInterval: 1000,
    enabled: !!agentId,
  })

  const nextMutation = useMutation({
    mutationFn: ({ step_id, override }: { step_id: string; override?: string }) =>
      postDebugNext(agentId, step_id, override),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['debug', agentId] }),
  })

  const skipMutation = useMutation({
    mutationFn: () => postDebugSkipAll(agentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['debug', agentId] }),
  })

  const liveStep     = debugData?.step ?? null
  const isLivePaused = debugData?.paused ?? false
  const history      = debugData?.history ?? []

  const debugState: DebugState =
    !sessionActive                                              ? 'idle'
    : isLivePaused && liveStep                                 ? 'paused'
    : sessionActive && (history.length === 0 || afterNext)     ? 'waiting'
    : 'done'

  // Interval-based fallback: when afterNext is true, run a real setInterval
  // (1 s) so the count is driven by wall-clock time, not by debugData reference
  // changes.  TanStack Query's structural-sharing can keep the same `data`
  // reference across polls when the response is identical, which would prevent
  // a useEffect([debugData]) from firing.
  useEffect(() => {
    if (!afterNext) {
      noStepPollCountRef.current = 0
      return
    }
    const id = setInterval(() => {
      if (isLivePaused) {
        // A new step appeared — reset and let it handle itself.
        noStepPollCountRef.current = 0
        return
      }
      noStepPollCountRef.current += 1
      if (noStepPollCountRef.current >= 5) {
        setAfterNext(false)
        noStepPollCountRef.current = 0
      }
    }, 1000)
    return () => clearInterval(id)
  }, [afterNext, isLivePaused])  // eslint-disable-line react-hooks/exhaustive-deps

  // Sync editor + auto-highlight + clear afterNext when liveStep changes
  useEffect(() => {
    if (!liveStep) return
    // New paused step arrived — clear the between-steps waiting flag
    setAfterNext(false)
    if (afterNextTimerRef.current) {
      clearTimeout(afterNextTimerRef.current)
      afterNextTimerRef.current = null
    }
    if (liveStep.step_id !== activeStepId) {
      setActiveStepId(liveStep.step_id)
      setEditContent(extractEditContent(liveStep))
      setIsModified(false)
      setIsSaved(false)
      // Auto-highlight this step in the sequence diagram
      const idx = history.findIndex(s => s.step_id === liveStep.step_id)
      if (idx >= 0) setSelectedStep(idx)
    }
  }, [liveStep?.step_id])  // eslint-disable-line react-hooks/exhaustive-deps

  const visibleSteps: SequenceStep[] = history.map(s => liveStepToSequenceStep(s, agentId || 'agent'))
  const liveStepIdx = liveStep ? history.findIndex(s => s.step_id === liveStep.step_id) : -1

  const handleSend = async () => {
    if (!msgInput.trim() || !agentId) return
    const prompt = msgInput.trim()
    await enableDebug(agentId, true)
    setSessionActive(true)
    setActiveStepId(null)
    setEditContent('')
    setIsModified(false)
    setIsSaved(false)
    setSelectedStep(null)
    setMsgInput('')
    // Clear any lingering afterNext from the previous session
    setAfterNext(false)
    if (afterNextTimerRef.current) {
      clearTimeout(afterNextTimerRef.current)
      afterNextTimerRef.current = null
    }
    // Fire and forget — proxy pauses arrive via polling.
    // When the agent finishes, clear afterNext so the UI transitions to 'done'.
    sendAgentMessage(agentId, prompt)
      .catch(() => {})
      .finally(() => {
        setAfterNext(false)
        if (afterNextTimerRef.current) {
          clearTimeout(afterNextTimerRef.current)
          afterNextTimerRef.current = null
        }
      })
  }

  const handleEdit = (v: string) => {
    setEditContent(v)
    setIsModified(v !== (liveStep ? extractEditContent(liveStep) : ''))
    setIsSaved(false)
  }

  const handleSave = () => setIsSaved(true)

  const handleNext = () => {
    if (!liveStep) return
    nextMutation.mutate({
      step_id: liveStep.step_id,
      // Reconstruct the full body with the edited portion swapped in
      override: isModified ? buildOverride(liveStep, editContent) : undefined,
    })
    setIsModified(false)
    setIsSaved(false)
    // Stay in 'waiting' until the next paused step arrives (or 90 s max)
    setAfterNext(true)
    if (afterNextTimerRef.current) clearTimeout(afterNextTimerRef.current)
    afterNextTimerRef.current = setTimeout(() => {
      setAfterNext(false)
      afterNextTimerRef.current = null
    }, 90_000)
  }

  const handleSkipAll = () => skipMutation.mutate()

  const panelHeight = embedded ? 580 : 'calc(100vh - 180px)'
  const panelMargin = embedded ? '0 -32px 0 -32px' : '0 -32px -28px -32px'

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {/* Message input */}
      <div style={{ marginBottom: 12 }}>
        <div style={{
          background: 'var(--panel)', border: '1px solid var(--line)',
          borderRadius: 10, padding: '8px 12px', display: 'flex',
          gap: 8, alignItems: 'center',
        }}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#9fb0d1" strokeWidth="2" style={{ flexShrink: 0 }}>
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
          <input
            ref={inputRef}
            type="text"
            value={msgInput}
            onChange={e => setMsgInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSend()}
            placeholder={debugState === 'waiting' ? 'Waiting for agent…' : debugState === 'done' ? 'Send another message to start a new debug session…' : 'Send a message to trigger debug session…'}
            disabled={debugState === 'waiting'}
            style={{
              flex: 1, background: 'transparent', border: 'none',
              outline: 'none', fontSize: 13, color: 'var(--text)', padding: '5px 0',
            }}
          />
          <Button
            size="sm"
            onClick={handleSend}
            disabled={debugState === 'waiting' || !msgInput.trim()}
            style={{ background: 'var(--blue)', color: '#0b1020' }}
          >
            <Send size={12} />
            Send
          </Button>
        </div>
      </div>

      {/* Main two-panel area */}
      <div style={{
        display: 'flex', margin: panelMargin,
        height: panelHeight, borderTop: '1px solid var(--line)',
      }}>
        <SequencePanel
          steps={visibleSteps}
          pausedLabel={
            debugState === 'paused' && liveStep
              ? (liveStep.tool || liveStep.kind)
              : debugState === 'done' ? '✓ Session complete — click any arrow to inspect'
              : null
          }
          pausedLabelColor={debugState === 'done' ? 'var(--green)' : undefined}
          selectedIndex={selectedStep}
          onSelectStep={i => setSelectedStep(i)}
        />

        {debugState === 'idle' && (
          <div style={{
            flex: '0 0 42%', display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
            background: 'var(--panel2)', gap: 10, color: 'var(--muted-raw)', fontSize: 13,
          }}>
            <Bug size={26} style={{ opacity: .3 }} />
            <span>Send a message to start a debug session.</span>
          </div>
        )}

        {debugState === 'waiting' && (
          <div style={{
            flex: '0 0 42%', display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
            background: 'var(--panel2)', gap: 10,
          }}>
            <div style={{ fontSize: 22 }}>⏳</div>
            <div style={{ fontSize: 13, color: 'var(--muted-raw)' }}>Waiting for agent to make a call…</div>
          </div>
        )}

        {debugState === 'paused' && liveStep && (
          <InspectorPanel
            step={liveStep}
            agentId={agentId}
            stepNum={liveStepIdx >= 0 ? liveStepIdx : history.length - 1}
            totalSteps={history.length}
            editContent={editContent}
            isModified={isModified}
            isSaved={isSaved}
            onEdit={handleEdit}
            onSave={handleSave}
            onNext={handleNext}
            onSkipAll={handleSkipAll}
          />
        )}

        {debugState === 'done' && (
          <ReadOnlyInspector
            history={history}
            selectedStepIdx={selectedStep}
          />
        )}
      </div>
    </div>
  )
}

// ── Tab bar (standalone route only) ───────────────────────────────────────

function AgentTabBar({ agentId }: { agentId: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const tabs = [
    { label: 'Dashboard', path: `/agents/${encodeURIComponent(agentId)}` },
    { label: 'Policy',    path: `/agents/${encodeURIComponent(agentId)}/policy` },
    { label: 'PII',       path: `/agents/${encodeURIComponent(agentId)}/pii` },
    { label: 'Debugger',  path: `/agents/${encodeURIComponent(agentId)}/debugger` },
  ]
  return (
    <div style={{ display: 'flex', borderBottom: '1px solid var(--line)', marginBottom: 16, marginTop: 12 }}>
      {tabs.map(tab => {
        const active = location.pathname === tab.path
        return (
          <button key={tab.path} onClick={() => navigate(tab.path)} style={{
            padding: '9px 22px', fontSize: 13, fontWeight: 600,
            border: 'none', background: 'transparent', cursor: 'pointer',
            color: active ? 'var(--blue)' : 'var(--muted-raw)',
            borderBottom: `2px solid ${active ? 'var(--blue)' : 'transparent'}`,
            marginBottom: -1,
          }}>
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}

// ── Standalone route page ──────────────────────────────────────────────────

export default function AgentDebugger() {
  const { agentId = '' } = useParams<{ agentId: string }>()
  const navigate = useNavigate()
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <Button variant="ghost" size="sm" onClick={() => navigate('/agents')} style={{ paddingLeft: 6 }}>
          <ArrowLeft size={14} /> Agents
        </Button>
        <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.3px', fontFamily: 'JetBrains Mono, ui-monospace, monospace', margin: 0 }}>
          {agentId}
        </h1>
      </div>
      <AgentTabBar agentId={agentId} />
      <DebuggerPanel agentId={agentId} />
    </div>
  )
}
