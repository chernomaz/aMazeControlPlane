import { useMemo } from 'react'
import type { SequenceStep, SequenceStepStatus } from '@/api/traces'

interface Props {
  steps: SequenceStep[]
  selectedIndex?: number | null
  onSelectStep?: (index: number) => void
}

const COLOR_BY_STATUS: Record<SequenceStepStatus, string> = {
  real: 'var(--blue)',
  mocked: 'var(--violet)',
  assertion: 'var(--cyan)',
  failed: 'var(--red)',
}

const LLM_PROVIDERS = new Set(['openai', 'anthropic', 'google', 'mistral', 'cohere', 'azure', 'bedrock', 'vertex'])

function normalizeLane(name: string): string {
  if (!name) return name
  const lower = name.toLowerCase()
  // "openai/gpt-4..." or "anthropic/claude-..." → "llm"
  if (lower.includes('/')) {
    const prefix = lower.split('/')[0]
    if (LLM_PROVIDERS.has(prefix)) return 'llm'
  }
  if (LLM_PROVIDERS.has(lower)) return 'llm'
  return name
}

const MIN_LANE_WIDTH = 140
const ROW_HEIGHT = 40
const HEADER_HEIGHT = 40
const TOP_PADDING = 16
const BOTTOM_PADDING = 16
const SIDE_PADDING = 24
// Approximate px per character at fontSize 11, JetBrains Mono
const CHAR_WIDTH_PX = 7.2

export default function SequenceDiagram({ steps, selectedIndex, onSelectStep }: Props) {
  const { lanes, laneIndex, laneWidth } = useMemo(() => {
    // Preserve first-seen order across from/to fields.
    const order: string[] = []
    const seen = new Set<string>()
    const see = (name: string) => {
      if (!name) return
      if (seen.has(name)) return
      seen.add(name)
      order.push(name)
    }
    // Force "user" first if it appears so the diagram reads left-to-right.
    for (const s of steps) {
      see(normalizeLane(s.from))
      see(normalizeLane(s.to))
    }
    const idx: Record<string, number> = {}
    order.forEach((name, i) => {
      idx[name] = i
    })
    // Compute lane width wide enough for the longest label + padding.
    const maxLen = order.reduce((m, n) => Math.max(m, n.length), 0)
    const computed = Math.ceil(maxLen * CHAR_WIDTH_PX) + 32
    const laneWidth = Math.max(MIN_LANE_WIDTH, computed)
    return { lanes: order, laneIndex: idx, laneWidth }
  }, [steps])

  if (steps.length === 0 || lanes.length === 0) {
    return (
      <div
        style={{
          color: 'var(--muted-raw)',
          fontSize: 13,
          padding: 24,
          textAlign: 'center',
        }}
      >
        No sequence steps recorded.
      </div>
    )
  }

  const width = SIDE_PADDING * 2 + Math.max(1, lanes.length - 1) * laneWidth + laneWidth
  const height =
    TOP_PADDING + HEADER_HEIGHT + steps.length * ROW_HEIGHT + BOTTOM_PADDING

  const laneX = (i: number) => SIDE_PADDING + laneWidth / 2 + i * laneWidth

  return (
    <svg
      role="img"
      aria-label="Conversation sequence diagram"
      width={width}
      height={height}
      style={{ display: 'block', minWidth: '100%' }}
    >
      {/* Lane verticals */}
      {lanes.map((name, i) => {
        const x = laneX(i)
        return (
          <g key={`lane-${name}`}>
            <rect
              x={x - laneWidth / 2 + 8}
              y={TOP_PADDING}
              width={laneWidth - 16}
              height={HEADER_HEIGHT - 8}
              rx={6}
              fill="var(--panel)"
              stroke="var(--line)"
            />
            <text
              x={x}
              y={TOP_PADDING + HEADER_HEIGHT / 2 + 1}
              textAnchor="middle"
              dominantBaseline="middle"
              fontFamily="JetBrains Mono, ui-monospace, monospace"
              fontSize={11}
              fontWeight={600}
              fill="var(--text)"
            >
              {name}
            </text>
            <line
              x1={x}
              y1={TOP_PADDING + HEADER_HEIGHT}
              x2={x}
              y2={height - BOTTOM_PADDING}
              stroke="var(--line)"
              strokeDasharray="3 4"
            />
          </g>
        )
      })}

      {/* Arrows */}
      <defs>
        {(['real', 'mocked', 'assertion', 'failed'] as SequenceStepStatus[]).map((s) => (
          <marker
            key={`arrow-${s}`}
            id={`arrow-${s}`}
            viewBox="0 0 10 10"
            refX="8"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill={COLOR_BY_STATUS[s]} />
          </marker>
        ))}
      </defs>

      {steps.map((step, i) => {
        const fromIdx = laneIndex[normalizeLane(step.from)] ?? 0
        const toIdx = laneIndex[normalizeLane(step.to)] ?? fromIdx
        const x1 = laneX(fromIdx)
        const x2 = laneX(toIdx)
        const y = TOP_PADDING + HEADER_HEIGHT + i * ROW_HEIGHT + ROW_HEIGHT / 2
        const color = COLOR_BY_STATUS[step.status] ?? COLOR_BY_STATUS.real
        const isSelected = selectedIndex === i
        const sameLane = fromIdx === toIdx
        const labelX = sameLane ? x1 + 36 : (x1 + x2) / 2
        const labelAnchor = sameLane ? 'start' : 'middle'

        return (
          <g
            key={`step-${i}`}
            style={{ cursor: onSelectStep ? 'pointer' : 'default' }}
            onClick={() => onSelectStep?.(i)}
          >
            {isSelected && (
              <rect
                x={SIDE_PADDING}
                y={y - ROW_HEIGHT / 2 + 2}
                width={width - SIDE_PADDING * 2}
                height={ROW_HEIGHT - 4}
                fill="rgba(93,169,255,.08)"
                stroke="var(--blue)"
                strokeOpacity={0.4}
                rx={6}
              />
            )}
            {sameLane ? (
              // Self-call: small loop on the same lane.
              <path
                d={`M ${x1} ${y - 6} C ${x1 + 30} ${y - 14}, ${x1 + 30} ${y + 14}, ${x1} ${y + 6}`}
                fill="none"
                stroke={color}
                strokeWidth={isSelected ? 2.5 : 1.8}
                markerEnd={`url(#arrow-${step.status})`}
              />
            ) : (
              <line
                x1={x1}
                y1={y}
                x2={x2}
                y2={y}
                stroke={color}
                strokeWidth={isSelected ? 2.5 : 1.8}
                markerEnd={`url(#arrow-${step.status})`}
              />
            )}
            <text
              x={labelX}
              y={y - 6}
              textAnchor={labelAnchor}
              fontFamily="Inter, ui-sans-serif, system-ui, sans-serif"
              fontSize={11}
              fontWeight={500}
              fill="var(--text)"
              style={{ pointerEvents: 'none' }}
            >
              {step.label}
            </text>
            {/* Wide invisible hit-target so clicks register reliably */}
            <rect
              x={SIDE_PADDING}
              y={y - ROW_HEIGHT / 2}
              width={width - SIDE_PADDING * 2}
              height={ROW_HEIGHT}
              fill="transparent"
            />
          </g>
        )
      })}
    </svg>
  )
}
