import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Flag } from 'lucide-react'
import type { CallType } from '@/api/policy'

// Visual style follows services/ui_mock/index.html (strict-mode graph
// canvas, lines ~615-800): dark panels, color-coded by call_type, with
// max_loops shown on the node and arrowhead edges.

export interface StepNodeData {
  step_id: number
  call_type: CallType
  callee_id: string
  max_loops: number
  isStart?: boolean
  // Index signature so this satisfies XYFlow's `Record<string, unknown>` data constraint.
  [key: string]: unknown
}

const CALL_TYPE_STYLE: Record<CallType, { border: string; bg: string; text: string }> = {
  tool: {
    border: 'border-amaze-cyan/60',
    bg: 'bg-amaze-cyan/10',
    text: 'text-amaze-cyan',
  },
  agent: {
    border: 'border-amaze-violet/60',
    bg: 'bg-amaze-violet/10',
    text: 'text-amaze-violet',
  },
}

export function StepNode({ data, selected }: NodeProps) {
  const d = data as StepNodeData
  const style = CALL_TYPE_STYLE[d.call_type]
  return (
    <div
      className={`min-w-[150px] rounded-lg border-2 ${style.border} ${style.bg} px-3 py-2 text-sm shadow-md transition-all ${
        selected ? 'ring-2 ring-amaze-blue ring-offset-2 ring-offset-amaze-bg' : ''
      }`}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!h-2 !w-2 !border-amaze-line !bg-amaze-muted"
      />
      <div className="flex items-center justify-between gap-2">
        <span className={`font-mono text-xs font-semibold ${style.text}`}>
          {d.call_type}:{d.callee_id || '<unset>'}
        </span>
        {d.isStart && (
          <span
            className="inline-flex items-center rounded bg-amaze-yellow/20 px-1.5 py-0.5 text-[10px] font-semibold text-amaze-yellow"
            title="start step"
          >
            start
          </span>
        )}
      </div>
      <div className="mt-1 flex items-center gap-2 text-[10px] text-amaze-muted">
        <span>#{d.step_id}</span>
        <span>·</span>
        <span>loops: {d.max_loops}</span>
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!h-2 !w-2 !border-amaze-line !bg-amaze-muted"
      />
    </div>
  )
}

// ─── Synthetic finish terminal node ──────────────────────────────────────────

export function FinishNode({ selected }: NodeProps) {
  return (
    <div
      className={`flex min-w-[110px] items-center gap-2 rounded-lg border-2 border-amaze-green/60 bg-amaze-green/10 px-3 py-2 text-sm shadow-md transition-all ${
        selected ? 'ring-2 ring-amaze-blue ring-offset-2 ring-offset-amaze-bg' : ''
      }`}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!h-2 !w-2 !border-amaze-line !bg-amaze-muted"
      />
      <Flag className="h-3.5 w-3.5 text-amaze-green" />
      <span className="text-xs font-semibold text-amaze-green">finish</span>
    </div>
  )
}
