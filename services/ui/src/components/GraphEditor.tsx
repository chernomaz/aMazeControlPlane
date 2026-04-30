import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type Connection,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Plus, Trash2, X, CheckCircle2, AlertTriangle } from 'lucide-react'

import type { Graph, Step, CallType } from '@/api/policy'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { StepNode, FinishNode, type StepNodeData } from './StepNode'

// ─── Types ────────────────────────────────────────────────────────────────────

interface GraphEditorProps {
  value: Graph
  onChange: (next: Graph) => void
  allowedTools: string[]
  allowedAgents: string[]
  readOnly?: boolean
  className?: string
}

const FINISH_ID = 'finish'
const LEVEL_GAP = 100 // y between levels (60px node + 40px breathing room ≈ 100)
const SIBLING_GAP = 200 // x between siblings (180px node + gap)

const nodeTypes = { step: StepNode, finish: FinishNode }

// ─── Layout (BFS from start_step) ────────────────────────────────────────────

function computeLayout(g: Graph): Map<number, { x: number; y: number }> {
  const stepMap = new Map(g.steps.map((s) => [s.step_id, s]))
  const levels = new Map<number, number>()
  const queue: number[] = []

  if (stepMap.has(g.start_step)) {
    levels.set(g.start_step, 0)
    queue.push(g.start_step)
  }
  while (queue.length) {
    const cur = queue.shift()!
    const step = stepMap.get(cur)
    if (!step) continue
    for (const next of step.next_steps) {
      if (!levels.has(next) && stepMap.has(next)) {
        levels.set(next, (levels.get(cur) ?? 0) + 1)
        queue.push(next)
      }
    }
  }
  // Orphans (unreachable from start) get pushed onto a synthetic last level
  // so the user sees them and can wire them up.
  let maxLevel = 0
  for (const lvl of levels.values()) if (lvl > maxLevel) maxLevel = lvl
  for (const s of g.steps) {
    if (!levels.has(s.step_id)) {
      maxLevel += 1
      levels.set(s.step_id, maxLevel)
    }
  }

  const perLevel = new Map<number, number[]>()
  for (const [sid, lvl] of levels) {
    const arr = perLevel.get(lvl) ?? []
    arr.push(sid)
    perLevel.set(lvl, arr)
  }

  const positions = new Map<number, { x: number; y: number }>()
  for (const [lvl, sids] of perLevel) {
    sids.forEach((sid, idx) => {
      const total = sids.length
      positions.set(sid, {
        x: (idx - (total - 1) / 2) * SIBLING_GAP,
        y: lvl * LEVEL_GAP,
      })
    })
  }
  return positions
}

// ─── Round-trip helpers ──────────────────────────────────────────────────────

export function graphToFlow(g: Graph): { nodes: Node[]; edges: Edge[] } {
  const positions = computeLayout(g)

  const stepNodes: Node[] = g.steps.map((s) => ({
    id: String(s.step_id),
    type: 'step',
    position: positions.get(s.step_id) ?? { x: 0, y: 0 },
    data: {
      step_id: s.step_id,
      call_type: s.call_type,
      callee_id: s.callee_id,
      max_loops: s.max_loops,
      isStart: s.step_id === g.start_step,
    } satisfies StepNodeData,
  }))

  // Synthetic finish node — placed below the deepest level, centered.
  let maxLvl = 0
  for (const p of positions.values()) {
    const lvl = Math.round(p.y / LEVEL_GAP)
    if (lvl > maxLvl) maxLvl = lvl
  }
  const finishNode: Node = {
    id: FINISH_ID,
    type: 'finish',
    position: { x: 0, y: (maxLvl + 1) * LEVEL_GAP },
    data: {},
    deletable: false,
  }

  const edges: Edge[] = []
  for (const s of g.steps) {
    if (s.next_steps.length === 0) {
      edges.push({
        id: `${s.step_id}->finish`,
        source: String(s.step_id),
        target: FINISH_ID,
        animated: true,
        style: { stroke: '#1fbf75' },
      })
    } else {
      for (const n of s.next_steps) {
        edges.push({
          id: `${s.step_id}->${n}`,
          source: String(s.step_id),
          target: String(n),
          animated: true,
          style: { stroke: '#5da9ff' },
        })
      }
    }
  }

  return { nodes: [...stepNodes, finishNode], edges }
}

export function flowToGraph(nodes: Node[], edges: Edge[]): Graph {
  const stepNodes = nodes.filter((n) => n.id !== FINISH_ID)

  const incoming = new Set(edges.filter((e) => e.target !== FINISH_ID).map((e) => e.target))
  // start = lowest step_id with no incoming edge from another real step;
  // fall back to lowest step_id if every node has an incoming edge.
  let start = Number.POSITIVE_INFINITY
  for (const n of stepNodes) {
    const sid = (n.data as StepNodeData).step_id
    if (!incoming.has(n.id) && sid < start) start = sid
  }
  if (!isFinite(start) && stepNodes.length > 0) {
    start = Math.min(...stepNodes.map((n) => (n.data as StepNodeData).step_id))
  }
  if (!isFinite(start)) start = 1

  const steps: Step[] = stepNodes.map((n) => {
    const d = n.data as StepNodeData
    const next_steps = edges
      .filter((e) => e.source === n.id && e.target !== FINISH_ID)
      .map((e) => parseInt(e.target, 10))
      .filter((v) => Number.isFinite(v))
    return {
      step_id: d.step_id,
      call_type: d.call_type,
      callee_id: d.callee_id,
      max_loops: d.max_loops,
      next_steps,
    }
  })

  return { start_step: start, steps }
}

// ─── Validation ──────────────────────────────────────────────────────────────

export function validateGraph(g: Graph): { ok: true } | { ok: false; errors: string[] } {
  const errors: string[] = []
  const ids = g.steps.map((s) => s.step_id)
  const idSet = new Set(ids)

  if (g.steps.length === 0) {
    return { ok: false, errors: ['Graph is empty: add at least one step.'] }
  }
  if (ids.length !== idSet.size) {
    const seen = new Set<number>()
    for (const id of ids) {
      if (seen.has(id)) errors.push(`Duplicate step_id: ${id}`)
      seen.add(id)
    }
  }
  if (!idSet.has(g.start_step)) {
    errors.push(`start_step ${g.start_step} is not in the steps list.`)
  }
  for (const s of g.steps) {
    if (s.max_loops < 1) {
      errors.push(`step ${s.step_id}: max_loops must be >= 1 (got ${s.max_loops}).`)
    }
    if (!s.callee_id || s.callee_id.trim() === '') {
      errors.push(`step ${s.step_id}: callee_id is empty.`)
    }
    for (const n of s.next_steps) {
      if (!idSet.has(n)) {
        errors.push(`step ${s.step_id}: next_steps references non-existent step ${n}.`)
      }
    }
  }

  // Reachability from start_step (BFS)
  if (idSet.has(g.start_step)) {
    const stepMap = new Map(g.steps.map((s) => [s.step_id, s]))
    const reachable = new Set<number>([g.start_step])
    const queue = [g.start_step]
    while (queue.length) {
      const cur = queue.shift()!
      const step = stepMap.get(cur)
      if (!step) continue
      for (const n of step.next_steps) {
        if (!reachable.has(n) && idSet.has(n)) {
          reachable.add(n)
          queue.push(n)
        }
      }
    }
    for (const id of ids) {
      if (!reachable.has(id)) {
        errors.push(`step ${id}: unreachable from start_step ${g.start_step}.`)
      }
    }
    // At least one terminal step (next_steps empty) must be reachable
    const terminalReached = g.steps.some(
      (s) => s.next_steps.length === 0 && reachable.has(s.step_id),
    )
    if (!terminalReached) {
      errors.push('No terminal step (with empty next_steps) is reachable from start_step.')
    }
  }

  return errors.length === 0 ? { ok: true } : { ok: false, errors }
}

// ─── Inner component (must live under <ReactFlowProvider>) ───────────────────

function GraphEditorInner({
  value,
  onChange,
  allowedTools,
  allowedAgents,
  readOnly,
}: GraphEditorProps) {
  const initial = useMemo(() => graphToFlow(value), [])
  // We deliberately seed nodes/edges from `value` only on first mount; after
  // that the editor owns the React Flow state and pushes back via onChange.
  // (Re-initialising on every prop change would clobber in-progress edits.)
  // eslint-disable-next-line react-hooks/exhaustive-deps

  const [nodes, setNodes, onNodesChange] = useNodesState(initial.nodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initial.edges)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [validation, setValidation] = useState<
    { ok: true } | { ok: false; errors: string[] } | null
  >(null)

  // Push changes upstream whenever the graph mutates.
  const skipFirst = useRef(true)
  useEffect(() => {
    if (skipFirst.current) {
      skipFirst.current = false
      return
    }
    onChange(flowToGraph(nodes, edges))
  }, [nodes, edges, onChange])

  // Mark current start in node data so the badge renders correctly.
  // We pick the smallest step_id with no incoming edge.
  useEffect(() => {
    const stepNodes = nodes.filter((n) => n.id !== FINISH_ID)
    if (stepNodes.length === 0) return
    const incoming = new Set(edges.filter((e) => e.target !== FINISH_ID).map((e) => e.target))
    let startId = Number.POSITIVE_INFINITY
    for (const n of stepNodes) {
      const sid = (n.data as StepNodeData).step_id
      if (!incoming.has(n.id) && sid < startId) startId = sid
    }
    if (!isFinite(startId)) {
      startId = Math.min(...stepNodes.map((n) => (n.data as StepNodeData).step_id))
    }

    // Only patch if mismatched, to avoid an infinite update loop.
    let mutated = false
    const patched = nodes.map((n) => {
      if (n.id === FINISH_ID) return n
      const d = n.data as StepNodeData
      const want = d.step_id === startId
      if ((d.isStart ?? false) !== want) {
        mutated = true
        return { ...n, data: { ...d, isStart: want } }
      }
      return n
    })
    if (mutated) setNodes(patched)
  }, [nodes, edges, setNodes])

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly) return
      // Reject self-loops at the UI layer (graph semantics allow them, but
      // they're almost always a click-mistake at this size).
      if (connection.source === connection.target) return
      // Reject edges originating from the finish node (it's a sink).
      if (connection.source === FINISH_ID) return
      const stroke = connection.target === FINISH_ID ? '#1fbf75' : '#5da9ff'
      setEdges((eds) => addEdge({ ...connection, animated: true, style: { stroke } }, eds))
    },
    [readOnly, setEdges],
  )

  function nextStepId(): number {
    const ids = nodes
      .filter((n) => n.id !== FINISH_ID)
      .map((n) => (n.data as StepNodeData).step_id)
    return ids.length === 0 ? 1 : Math.max(...ids) + 1
  }

  function addStep(call_type: CallType) {
    if (readOnly) return
    const id = nextStepId()
    // Find an empty slot below the deepest existing level.
    let maxY = 0
    for (const n of nodes) if (n.position.y > maxY) maxY = n.position.y
    const newNode: Node = {
      id: String(id),
      type: 'step',
      position: { x: 0, y: maxY + LEVEL_GAP },
      data: {
        step_id: id,
        call_type,
        callee_id: '',
        max_loops: 1,
        isStart: false,
      } satisfies StepNodeData,
    }
    setNodes((ns) => [...ns, newNode])
    setSelectedId(String(id))
  }

  function updateNode(nodeId: string, patch: Partial<StepNodeData>) {
    setNodes((ns) =>
      ns.map((n) =>
        n.id === nodeId
          ? { ...n, data: { ...(n.data as StepNodeData), ...patch } }
          : n,
      ),
    )
  }

  function deleteNode(nodeId: string) {
    if (nodeId === FINISH_ID) return
    setNodes((ns) => ns.filter((n) => n.id !== nodeId))
    setEdges((es) => es.filter((e) => e.source !== nodeId && e.target !== nodeId))
    setSelectedId(null)
  }

  function runValidate() {
    setValidation(validateGraph(flowToGraph(nodes, edges)))
  }

  const selectedNode = nodes.find((n) => n.id === selectedId && n.id !== FINISH_ID)

  return (
    <>
      {/* Toolbar */}
      <div className="flex items-center gap-2 border-b border-amaze-line px-3 py-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => addStep('tool')}
          disabled={readOnly}
          className="border-amaze-cyan/40 text-amaze-cyan hover:bg-amaze-cyan/10"
        >
          <Plus className="h-3.5 w-3.5" /> Add tool step
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => addStep('agent')}
          disabled={readOnly}
          className="border-amaze-violet/40 text-amaze-violet hover:bg-amaze-violet/10"
        >
          <Plus className="h-3.5 w-3.5" /> Add agent step
        </Button>
        <div className="ml-auto flex items-center gap-2">
          {validation && validation.ok && (
            <span className="inline-flex items-center gap-1 text-xs text-amaze-green">
              <CheckCircle2 className="h-3.5 w-3.5" /> valid
            </span>
          )}
          {validation && !validation.ok && (
            <span
              className="inline-flex items-center gap-1 text-xs text-amaze-red"
              title={validation.errors.join('\n')}
            >
              <AlertTriangle className="h-3.5 w-3.5" /> {validation.errors.length} issue
              {validation.errors.length === 1 ? '' : 's'}
            </span>
          )}
          <Button variant="outline" size="sm" onClick={runValidate}>
            Validate
          </Button>
        </div>
      </div>

      {/* Canvas + side panel */}
      <div className="relative" style={{ height: 480 }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={readOnly ? undefined : onNodesChange}
          onEdgesChange={readOnly ? undefined : onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, n) => setSelectedId(n.id)}
          onPaneClick={() => setSelectedId(null)}
          nodeTypes={nodeTypes}
          fitView
          nodesDraggable={!readOnly}
          nodesConnectable={!readOnly}
          elementsSelectable
          snapToGrid
          snapGrid={[20, 20]}
          deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
          className="!bg-amaze-bg"
        >
          <Background color="#2b3a59" gap={20} />
          <Controls className="!bg-amaze-panel !border-amaze-line" />
          <MiniMap
            nodeStrokeColor="#2b3a59"
            nodeColor={(n) => {
              if (n.id === FINISH_ID) return '#1fbf75'
              const t = (n.data as StepNodeData).call_type
              return t === 'tool' ? '#43d1c6' : '#a97cff'
            }}
            maskColor="rgba(11,16,32,0.6)"
            className="!bg-amaze-panel !border !border-amaze-line"
          />
        </ReactFlow>

        {selectedNode && !readOnly && (
          <NodeConfigPanel
            node={selectedNode}
            allSteps={nodes
              .filter((n) => n.id !== FINISH_ID && n.id !== selectedNode.id)
              .map((n) => (n.data as StepNodeData).step_id)}
            allowedTools={allowedTools}
            allowedAgents={allowedAgents}
            edges={edges}
            onUpdate={(patch) => updateNode(selectedNode.id, patch)}
            onUpdateNextSteps={(targetIds) => {
              setEdges((es) => {
                const filtered = es.filter((e) => e.source !== selectedNode.id)
                if (targetIds.length === 0) {
                  return [
                    ...filtered,
                    {
                      id: `${selectedNode.id}->finish`,
                      source: selectedNode.id,
                      target: FINISH_ID,
                      animated: true,
                      style: { stroke: '#1fbf75' },
                    },
                  ]
                }
                return [
                  ...filtered,
                  ...targetIds.map((tid) => ({
                    id: `${selectedNode.id}->${tid}`,
                    source: selectedNode.id,
                    target: String(tid),
                    animated: true,
                    style: { stroke: '#5da9ff' },
                  })),
                ]
              })
            }}
            onDelete={() => deleteNode(selectedNode.id)}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>

      {validation && !validation.ok && (
        <div className="border-t border-amaze-line bg-amaze-red/10 px-3 py-2">
          <ul className="space-y-0.5 text-xs text-amaze-red">
            {validation.errors.map((e, i) => (
              <li key={i}>· {e}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

// ─── Side config panel ───────────────────────────────────────────────────────

interface NodeConfigPanelProps {
  node: Node
  allSteps: number[]
  allowedTools: string[]
  allowedAgents: string[]
  edges: Edge[]
  onUpdate: (patch: Partial<StepNodeData>) => void
  onUpdateNextSteps: (targetIds: number[]) => void
  onDelete: () => void
  onClose: () => void
}

function NodeConfigPanel({
  node,
  allSteps,
  allowedTools,
  allowedAgents,
  edges,
  onUpdate,
  onUpdateNextSteps,
  onDelete,
  onClose,
}: NodeConfigPanelProps) {
  const d = node.data as StepNodeData
  const calleeOptions = d.call_type === 'tool' ? allowedTools : allowedAgents

  const currentNext = edges
    .filter((e) => e.source === node.id && e.target !== FINISH_ID)
    .map((e) => parseInt(e.target, 10))
    .filter(Number.isFinite)

  const goesToFinish = edges.some((e) => e.source === node.id && e.target === FINISH_ID)

  function toggleNext(stepId: number) {
    const set = new Set(currentNext)
    if (set.has(stepId)) set.delete(stepId)
    else set.add(stepId)
    onUpdateNextSteps([...set])
  }

  function setTerminal() {
    onUpdateNextSteps([])
  }

  return (
    <div className="absolute right-3 top-3 z-10 w-60 rounded-lg border border-amaze-line bg-amaze-panel p-3 shadow-xl">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-amaze-text">Step #{d.step_id}</h3>
        <Button variant="ghost" size="icon" className="h-6 w-6" onClick={onClose}>
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="space-y-3">
        <div className="space-y-1">
          <Label className="text-xs text-amaze-muted">call_type</Label>
          <Input value={d.call_type} disabled className="h-7 text-xs" />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-amaze-muted">callee_id</Label>
          {calleeOptions.length > 0 ? (
            <Select
              value={d.callee_id || ''}
              onValueChange={(v) => onUpdate({ callee_id: v })}
            >
              <SelectTrigger className="h-7 text-xs">
                <SelectValue placeholder="select…" />
              </SelectTrigger>
              <SelectContent>
                {calleeOptions.map((opt) => (
                  <SelectItem key={opt} value={opt}>
                    {opt}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Input
              value={d.callee_id}
              onChange={(e) => onUpdate({ callee_id: e.target.value })}
              placeholder={d.call_type === 'tool' ? 'tool name' : 'agent id'}
              className="h-7 text-xs"
            />
          )}
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-amaze-muted">max_loops</Label>
          <Input
            type="number"
            min={1}
            value={d.max_loops}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10)
              onUpdate({ max_loops: Number.isFinite(n) && n >= 1 ? n : 1 })
            }}
            className="h-7 text-xs"
          />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-amaze-muted">next_steps</Label>
          {allSteps.length === 0 ? (
            <p className="text-[11px] text-amaze-muted">No other steps yet.</p>
          ) : (
            <div className="max-h-28 space-y-1 overflow-y-auto rounded border border-amaze-line bg-amaze-panel2/50 p-1.5">
              {allSteps.map((sid) => (
                <label
                  key={sid}
                  className="flex cursor-pointer items-center gap-2 px-1 py-0.5 text-xs"
                >
                  <input
                    type="checkbox"
                    checked={currentNext.includes(sid)}
                    onChange={() => toggleNext(sid)}
                    className="accent-amaze-blue"
                  />
                  <span className="font-mono text-amaze-text">#{sid}</span>
                </label>
              ))}
            </div>
          )}
          <label className="flex cursor-pointer items-center gap-2 pt-1 text-xs">
            <input
              type="checkbox"
              checked={goesToFinish && currentNext.length === 0}
              onChange={() => setTerminal()}
              className="accent-amaze-green"
            />
            <span className="text-amaze-green">terminal (→ finish)</span>
          </label>
        </div>

        <Button
          variant="destructive"
          size="sm"
          className="w-full"
          onClick={onDelete}
        >
          <Trash2 className="h-3.5 w-3.5" /> Delete step
        </Button>
      </div>
    </div>
  )
}

// ─── Public component ────────────────────────────────────────────────────────

export function GraphEditor(props: GraphEditorProps): ReactElement {
  return (
    <div
      className={`rounded-lg border border-amaze-line bg-amaze-panel/40 ${props.className ?? ''}`}
    >
      <ReactFlowProvider>
        <GraphEditorInner {...props} />
      </ReactFlowProvider>
    </div>
  )
}
