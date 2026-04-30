import * as React from 'react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export interface LineChartPoint {
  ts: number // unix seconds
  value: number
}

export interface LineChartProps {
  data: LineChartPoint[]
  title?: string
  unit?: string
  height?: number
  color?: string
  className?: string
  empty?: React.ReactNode
}

const DEFAULT_COLOR = '#5da9ff' // amaze blue
const TEXT_MUTED = '#9fb0d1'
const GRID_LINE = '#2b3a59'

const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 12
const PAD_BOTTOM = 24

// Catmull-Rom → cubic Bezier path. Produces a smooth line through every point.
function smoothPath(points: Array<{ x: number; y: number }>): string {
  if (points.length === 0) return ''
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`
  let d = `M ${points[0].x} ${points[0].y}`
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] ?? points[i]
    const p1 = points[i]
    const p2 = points[i + 1]
    const p3 = points[i + 2] ?? p2
    const c1x = p1.x + (p2.x - p0.x) / 6
    const c1y = p1.y + (p2.y - p0.y) / 6
    const c2x = p2.x - (p3.x - p1.x) / 6
    const c2y = p2.y - (p3.y - p1.y) / 6
    d += ` C ${c1x} ${c1y}, ${c2x} ${c2y}, ${p2.x} ${p2.y}`
  }
  return d
}

function formatRelative(deltaSec: number): string {
  if (deltaSec <= 30) return 'now'
  if (deltaSec < 60) return `${Math.round(deltaSec)}s ago`
  if (deltaSec < 3600) return `${Math.round(deltaSec / 60)}m ago`
  if (deltaSec < 86400) return `${Math.round(deltaSec / 3600)}h ago`
  return `${Math.round(deltaSec / 86400)}d ago`
}

function formatAbsolute(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function LineChart({
  data,
  title,
  unit,
  height = 200,
  color = DEFAULT_COLOR,
  className,
  empty,
}: LineChartProps): React.ReactElement {
  const containerRef = React.useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = React.useState<number>(600)
  const [hoverIdx, setHoverIdx] = React.useState<number | null>(null)

  // Stable id for the gradient so multiple LineCharts on one page don't collide.
  const gradientId = React.useId()

  // Responsive width via ResizeObserver.
  React.useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const w = Math.max(80, Math.floor(e.contentRect.width))
        setWidth(w)
      }
    })
    ro.observe(el)
    // Seed once.
    setWidth(Math.max(80, Math.floor(el.getBoundingClientRect().width)))
    return () => ro.disconnect()
  }, [])

  const isEmpty = data.length === 0

  // Sort by ts to be safe; consumers typically pass already-sorted data.
  const sorted = React.useMemo(
    () => (isEmpty ? [] : [...data].sort((a, b) => a.ts - b.ts)),
    [data, isEmpty],
  )

  const W = width
  const H = height

  const layout = React.useMemo(() => {
    if (sorted.length === 0) return null
    const tsMin = sorted[0].ts
    const tsMax = sorted[sorted.length - 1].ts
    const tsSpan = Math.max(1, tsMax - tsMin)

    const values = sorted.map((p) => p.value)
    const vMin = Math.min(...values)
    const vMax = Math.max(...values)
    // Pad by 5% so the line never kisses the top/bottom edge.
    const vPad = vMax === vMin ? Math.max(1, Math.abs(vMax) * 0.05 || 1) : (vMax - vMin) * 0.05
    const yMin = vMin - vPad
    const yMax = vMax + vPad
    const ySpan = Math.max(1e-9, yMax - yMin)

    const innerW = Math.max(1, W - PAD_LEFT - PAD_RIGHT)
    const innerH = Math.max(1, H - PAD_TOP - PAD_BOTTOM)

    const points = sorted.map((p) => ({
      x: PAD_LEFT + ((p.ts - tsMin) / tsSpan) * innerW,
      y: PAD_TOP + (1 - (p.value - yMin) / ySpan) * innerH,
      ts: p.ts,
      value: p.value,
    }))

    const linePath = smoothPath(points)
    const baselineY = PAD_TOP + innerH
    const areaPath =
      points.length > 0
        ? `${linePath} L ${points[points.length - 1].x} ${baselineY} L ${points[0].x} ${baselineY} Z`
        : ''

    // ~5 x-axis ticks across the bottom — relative time labels.
    const TICK_COUNT = 5
    const now = Math.floor(Date.now() / 1000)
    const ticks: Array<{ x: number; label: string }> = []
    for (let i = 0; i < TICK_COUNT; i++) {
      const frac = i / (TICK_COUNT - 1)
      const ts = tsMin + frac * tsSpan
      const x = PAD_LEFT + frac * innerW
      const delta = Math.max(0, now - ts)
      ticks.push({ x, label: formatRelative(delta) })
    }

    return { points, linePath, areaPath, ticks, baselineY, innerW, innerH }
  }, [sorted, W, H])

  const handleMove = (e: React.MouseEvent<SVGRectElement>) => {
    if (!layout) return
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    // Map client x → svg viewBox x (we use viewBox 0 0 W H so they're equal).
    const px = ((e.clientX - rect.left) / rect.width) * W
    // Snap to nearest point by x (which is monotonic in ts).
    let best = 0
    let bestDist = Infinity
    for (let i = 0; i < layout.points.length; i++) {
      const d = Math.abs(layout.points[i].x - px)
      if (d < bestDist) {
        bestDist = d
        best = i
      }
    }
    setHoverIdx(best)
  }
  const handleLeave = () => setHoverIdx(null)

  const hovered = hoverIdx != null && layout ? layout.points[hoverIdx] : null

  const renderEmpty =
    empty ?? (
      <div
        className="flex items-center justify-center text-sm text-amaze-muted"
        style={{ height: H }}
      >
        No data
      </div>
    )

  // Tooltip horizontal placement: clamp inside chart area.
  const TOOLTIP_W = 160
  const tooltipLeft = hovered
    ? Math.max(4, Math.min(W - TOOLTIP_W - 4, hovered.x - TOOLTIP_W / 2))
    : 0

  return (
    <Card className={cn('bg-amaze-panel border-amaze-line text-amaze-text', className)}>
      {title ? (
        <CardHeader className="px-4 pt-3 pb-2">
          <CardTitle className="text-sm font-semibold tracking-tight text-amaze-text">
            {title}
          </CardTitle>
        </CardHeader>
      ) : null}
      <CardContent className={cn('px-2 pt-1', title ? 'pb-2' : 'py-2')}>
        <div ref={containerRef} className="relative w-full" style={{ height: H }}>
          {isEmpty || !layout ? (
            renderEmpty
          ) : (
            <>
              <svg
                width={W}
                height={H}
                viewBox={`0 0 ${W} ${H}`}
                preserveAspectRatio="none"
                className="block"
              >
                <defs>
                  <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={color} stopOpacity={0} />
                  </linearGradient>
                </defs>

                {/* Soft baseline */}
                <line
                  x1={PAD_LEFT}
                  y1={layout.baselineY}
                  x2={W - PAD_RIGHT}
                  y2={layout.baselineY}
                  stroke={GRID_LINE}
                  strokeOpacity={0.5}
                  strokeWidth={1}
                />

                {/* Gradient fill below the line */}
                <path d={layout.areaPath} fill={`url(#${gradientId})`} />

                {/* The line itself */}
                <path
                  d={layout.linePath}
                  fill="none"
                  stroke={color}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />

                {/* X-axis tick labels */}
                {layout.ticks.map((t, i) => (
                  <text
                    key={i}
                    x={t.x}
                    y={H - 6}
                    textAnchor={i === 0 ? 'start' : i === layout.ticks.length - 1 ? 'end' : 'middle'}
                    fontSize={10}
                    fill={TEXT_MUTED}
                    fontFamily="Inter, ui-sans-serif, system-ui, sans-serif"
                  >
                    {t.label}
                  </text>
                ))}

                {/* Hover affordances */}
                {hovered ? (
                  <>
                    <line
                      x1={hovered.x}
                      y1={PAD_TOP}
                      x2={hovered.x}
                      y2={layout.baselineY}
                      stroke={color}
                      strokeOpacity={0.45}
                      strokeWidth={1}
                      strokeDasharray="3 3"
                    />
                    <circle
                      cx={hovered.x}
                      cy={hovered.y}
                      r={4}
                      fill={color}
                      stroke="#0b1020"
                      strokeWidth={2}
                    />
                  </>
                ) : null}

                {/* Mouse capture overlay (must be last so it sits on top) */}
                <rect
                  x={0}
                  y={0}
                  width={W}
                  height={H}
                  fill="transparent"
                  onMouseMove={handleMove}
                  onMouseLeave={handleLeave}
                />
              </svg>

              {hovered ? (
                <div
                  className="pointer-events-none absolute rounded-md border border-amaze-line bg-amaze-bg/95 px-2 py-1 text-xs text-amaze-text shadow-lg"
                  style={{
                    left: tooltipLeft,
                    top: 4,
                    width: TOOLTIP_W,
                  }}
                >
                  <div className="font-semibold tabular-nums" style={{ color }}>
                    {hovered.value.toLocaleString()}
                    {unit ? <span className="ml-1 text-amaze-muted font-normal">{unit}</span> : null}
                  </div>
                  <div className="text-[10px] text-amaze-muted">{formatAbsolute(hovered.ts)}</div>
                </div>
              ) : null}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export default LineChart
