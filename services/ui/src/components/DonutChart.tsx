import * as React from 'react'
import { cn } from '@/lib/utils'

export interface DonutSegment {
  label: string
  count: number
  color?: string
}

interface DonutChartProps {
  segments: DonutSegment[]
  title?: string
  size?: number
  thickness?: number
  centerLabel?: string
  centerSubLabel?: string
  onSegmentClick?: (s: DonutSegment) => void
  selectedLabel?: string
  legend?: 'right' | 'bottom' | 'none'
  className?: string
  empty?: React.ReactNode
}

const PALETTE = [
  '#5da9ff',
  '#27d18d',
  '#f5b740',
  '#ef4444',
  '#a855f7',
  '#06b6d4',
  '#f97316',
  '#94a3b8',
]

interface ResolvedSegment extends DonutSegment {
  color: string
  index: number
}

export function DonutChart(props: DonutChartProps): React.ReactElement {
  const {
    segments,
    title,
    size = 160,
    thickness = 24,
    centerLabel,
    centerSubLabel,
    onSegmentClick,
    selectedLabel,
    legend = 'right',
    className,
    empty,
  } = props

  const [hoverLabel, setHoverLabel] = React.useState<string | null>(null)

  const resolved: ResolvedSegment[] = segments.map((s, i) => ({
    ...s,
    color: s.color ?? PALETTE[i % PALETTE.length],
    index: i,
  }))

  const total = resolved.reduce((sum, s) => sum + s.count, 0)
  const radius = (size - thickness) / 2
  const cx = size / 2
  const cy = size / 2
  const circumference = 2 * Math.PI * radius

  const renderable = resolved.filter((s) => s.count > 0)

  const renderCenter = () => (
    <>
      <text
        x={cx}
        y={cy - 2}
        textAnchor="middle"
        dominantBaseline="central"
        style={{
          fontFamily:
            "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
          fontWeight: 700,
          fontSize: Math.max(14, size * 0.18),
          fill: 'var(--text, #e8eefc)',
        }}
      >
        {centerLabel ?? String(total)}
      </text>
      {centerSubLabel && (
        <text
          x={cx}
          y={cy + size * 0.13}
          textAnchor="middle"
          dominantBaseline="central"
          style={{
            fontSize: Math.max(9, size * 0.075),
            fill: 'var(--muted, #9fb0d1)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
          }}
        >
          {centerSubLabel}
        </text>
      )}
    </>
  )

  const renderEmpty = () => (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle
        cx={cx}
        cy={cy}
        r={radius}
        fill="none"
        stroke="var(--line, #2a3348)"
        strokeWidth={thickness}
        opacity={0.4}
      />
      <text
        x={cx}
        y={cy}
        textAnchor="middle"
        dominantBaseline="central"
        style={{
          fontSize: Math.max(10, size * 0.085),
          fill: 'var(--muted, #9fb0d1)',
        }}
      >
        No data
      </text>
    </svg>
  )

  const renderSvg = () => {
    if (total <= 0) {
      if (empty !== undefined) {
        return (
          <div
            style={{ width: size, height: size }}
            className="flex items-center justify-center"
          >
            {empty}
          </div>
        )
      }
      return renderEmpty()
    }

    let cumulative = 0
    const isClickable = !!onSegmentClick

    return (
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        style={{ overflow: 'visible' }}
      >
        {/* Track */}
        <circle
          cx={cx}
          cy={cy}
          r={radius}
          fill="none"
          stroke="var(--line, #2a3348)"
          strokeWidth={thickness}
          opacity={0.25}
        />

        {/* Segments. Single-segment case: full ring. */}
        {renderable.length === 1 ? (
          (() => {
            const seg = renderable[0]
            const isSelected = selectedLabel === seg.label
            const isHover = hoverLabel === seg.label
            const dim =
              (selectedLabel && !isSelected) ||
              (hoverLabel && !isHover)
            return (
              <circle
                key={seg.label}
                cx={cx}
                cy={cy}
                r={radius}
                fill="none"
                stroke={seg.color}
                strokeWidth={isSelected ? thickness + 4 : thickness}
                opacity={dim ? (selectedLabel ? 0.5 : 0.6) : 1}
                style={{
                  transition:
                    'stroke-width 200ms ease, opacity 200ms ease',
                  cursor: isClickable ? 'pointer' : 'default',
                }}
                onMouseEnter={() => setHoverLabel(seg.label)}
                onMouseLeave={() => setHoverLabel(null)}
                onClick={() => onSegmentClick?.(seg)}
              />
            )
          })()
        ) : (
          renderable.map((seg) => {
            const fraction = seg.count / total
            const length = fraction * circumference
            const offset = -cumulative * circumference
            cumulative += fraction

            const isSelected = selectedLabel === seg.label
            const isHover = hoverLabel === seg.label
            const dim =
              (selectedLabel && !isSelected) ||
              (hoverLabel && !isHover && !selectedLabel)

            // Tiny gap between segments for visual separation when there's >1.
            const gap = Math.min(2, length * 0.04)
            const visibleLength = Math.max(length - gap, 0.001)

            const segmentThickness = isHover
              ? thickness + 3
              : isSelected
              ? thickness + 4
              : thickness

            return (
              <circle
                key={seg.label}
                cx={cx}
                cy={cy}
                r={radius}
                fill="none"
                stroke={seg.color}
                strokeWidth={segmentThickness}
                strokeDasharray={`${visibleLength} ${circumference}`}
                strokeDashoffset={offset}
                opacity={dim ? (selectedLabel ? 0.5 : 0.6) : 1}
                style={{
                  transform: `rotate(-90deg)`,
                  transformOrigin: `${cx}px ${cy}px`,
                  transition:
                    'stroke-dashoffset 400ms ease, stroke-dasharray 400ms ease, stroke-width 200ms ease, opacity 200ms ease',
                  cursor: isClickable ? 'pointer' : 'default',
                }}
                onMouseEnter={() => setHoverLabel(seg.label)}
                onMouseLeave={() => setHoverLabel(null)}
                onClick={() => onSegmentClick?.(seg)}
              />
            )
          })
        )}

        {renderCenter()}
      </svg>
    )
  }

  const renderLegend = () => {
    if (legend === 'none') return null
    const isClickable = !!onSegmentClick

    return (
      <div
        className={cn(
          'donut-legend',
          legend === 'right'
            ? 'flex flex-col gap-1'
            : 'flex flex-row flex-wrap gap-x-4 gap-y-1 justify-center',
        )}
        style={{ fontSize: 11 }}
      >
        {resolved.map((seg) => {
          const isFaded = seg.count === 0
          const isSelected = selectedLabel === seg.label
          const isHover = hoverLabel === seg.label
          return (
            <div
              key={seg.label}
              onMouseEnter={() => seg.count > 0 && setHoverLabel(seg.label)}
              onMouseLeave={() => setHoverLabel(null)}
              onClick={() => seg.count > 0 && onSegmentClick?.(seg)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '2px 3px',
                borderRadius: 4,
                cursor: isClickable && seg.count > 0 ? 'pointer' : 'default',
                opacity: isFaded ? 0.4 : 1,
                background:
                  isSelected || isHover
                    ? 'rgba(255,255,255,0.04)'
                    : 'transparent',
                transition: 'background 150ms ease',
              }}
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 2,
                  background: seg.color,
                  flexShrink: 0,
                  display: 'inline-block',
                }}
              />
              <span
                style={{
                  color: 'var(--muted, #9fb0d1)',
                  flex: 1,
                  fontFamily:
                    "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: 10,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {seg.label}
              </span>
              <span
                style={{
                  fontWeight: 700,
                  color: 'var(--text, #e8eefc)',
                  fontSize: 11,
                }}
              >
                {seg.count}
              </span>
            </div>
          )
        })}
      </div>
    )
  }

  const body = (
    <div
      className={cn(
        'flex',
        legend === 'bottom'
          ? 'flex-col items-center gap-3'
          : 'flex-row items-center gap-4',
      )}
    >
      <div style={{ flexShrink: 0 }}>{renderSvg()}</div>
      {legend !== 'none' && (
        <div style={{ minWidth: 0, flex: legend === 'right' ? 1 : undefined }}>
          {renderLegend()}
        </div>
      )}
    </div>
  )

  if (title) {
    return (
      <div
        className={cn(
          'rounded-lg border bg-card text-card-foreground shadow-sm p-3',
          className,
        )}
      >
        <div
          className="section-h"
          style={{
            marginBottom: 8,
            fontSize: 10,
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            color: 'var(--muted, #9fb0d1)',
          }}
        >
          {title}
        </div>
        {body}
      </div>
    )
  }

  return <div className={className}>{body}</div>
}

export default DonutChart
