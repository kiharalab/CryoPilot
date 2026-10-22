// Per-residue DAQ profile on a canvas: the score as a line over a diverging
// wash (blue above zero, red below), residues without a score as ticks under
// the plot. The canvas only redraws when data, size, or theme change; hover
// moves a crosshair and readout drawn as HTML on top. Click or Enter picks a
// residue; the arrow keys walk residues from the keyboard.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { useApp } from '../../store/app'
import { cx } from '../../lib/cx'
import { METRICS, residueValue } from './store'
import { RUG, drawProfile, formatScore, hitTest, layoutSegments, readTokens, xOf, yScale } from './profileCanvas'

/**
 * Props:
 *   chains=[{id, residues, start, end}]   residues sorted by number
 *   metric: 'aa_mean' | 'aa'
 *   domain                                 symmetric y-range (± domain)
 *   height                                 CSS pixels, axis included
 *   compact                                chains side by side, no axes (the dock under the viewer)
 *   cursor={chain, resnum}                 the residue last picked, marked with a line
 *   onPick(residue, chainId)               click or Enter
 *   label                                  accessible name of the chart
 */
export function DaqProfile({ chains, metric, domain, height = 112, compact = false, cursor, onPick, label, className }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const [width, setWidth] = useState(0)
  const [hover, setHover] = useState(null)
  const [fontsReady, setFontsReady] = useState(() => !document.fonts || document.fonts.status === 'loaded')
  const theme = useApp((state) => state.resolvedTheme)
  const liveId = useId()

  useEffect(() => {
    const element = wrapRef.current
    if (!element) return undefined
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (fontsReady || !document.fonts) return undefined
    let cancelled = false
    document.fonts.ready.then(() => {
      if (!cancelled) setFontsReady(true)
    })
    return () => {
      cancelled = true
    }
  }, [fontsReady])

  const geometry = useMemo(() => layoutSegments(chains, width, height, compact), [chains, width, height, compact])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !width) return
    drawProfile(canvas, geometry, { metric, domain, compact, tokens: readTokens() })
  }, [geometry, metric, domain, compact, theme, fontsReady, width])

  // A hovered residue from another chain list (after a data change) is dropped.
  useEffect(() => setHover(null), [chains])

  const hovered = hover ? geometry.segments[hover.segment]?.chain.residues[hover.index] : null
  const hoveredSegment = hover ? geometry.segments[hover.segment] : null

  const pointAt = useCallback(
    (clientX) => {
      const rect = wrapRef.current.getBoundingClientRect()
      return hitTest(geometry, clientX - rect.left)
    },
    [geometry],
  )

  const pick = (target) => {
    const segment = geometry.segments[target?.segment]
    const residue = segment?.chain.residues[target.index]
    if (residue && onPick) onPick(residue, segment.chain.id)
  }

  const step = (delta) => {
    setHover((current) => {
      const segments = geometry.segments
      if (!segments.length) return null
      let start = current
      if (!start) {
        const cursorSegment = cursor ? segments.findIndex((segment) => segment.chain.id === cursor.chain) : -1
        if (cursorSegment >= 0) {
          const index = segments[cursorSegment].chain.residues.findIndex((residue) => residue.resnum === cursor.resnum)
          start = { segment: cursorSegment, index: Math.max(0, index) }
        } else {
          return { segment: 0, index: 0 }
        }
      }
      let { segment, index } = start
      index += delta
      while (index < 0 && segment > 0) {
        segment -= 1
        index += segments[segment].chain.residues.length
      }
      while (index >= segments[segment].chain.residues.length && segment < segments.length - 1) {
        index -= segments[segment].chain.residues.length
        segment += 1
      }
      index = Math.max(0, Math.min(segments[segment].chain.residues.length - 1, index))
      return { segment, index }
    })
  }

  const onKeyDown = (event) => {
    const big = event.shiftKey ? 10 : 1
    if (event.key === 'ArrowRight') step(big)
    else if (event.key === 'ArrowLeft') step(-big)
    else if (event.key === 'Home') setHover(geometry.segments.length ? { segment: 0, index: 0 } : null)
    else if (event.key === 'End') {
      const last = geometry.segments.length - 1
      setHover(last >= 0 ? { segment: last, index: geometry.segments[last].chain.residues.length - 1 } : null)
    } else if ((event.key === 'Enter' || event.key === ' ') && hover) pick(hover)
    else if (event.key === 'Escape') setHover(null)
    else return
    event.preventDefault()
  }

  const y = yScale(geometry, domain)
  const hoverX = hovered && hoveredSegment ? xOf(hoveredSegment, hovered.resnum) : null
  const hoverValue = hovered ? residueValue(hovered, metric) : null
  const cursorSegment = cursor ? geometry.segments.find((segment) => segment.chain.id === cursor.chain) : null
  const cursorX =
    cursorSegment && cursor.resnum >= cursorSegment.chain.start && cursor.resnum <= cursorSegment.chain.end ? xOf(cursorSegment, cursor.resnum) : null
  const other = metric === 'aa_mean' ? 'aa' : 'aa_mean'
  const tooltipLeft = hoverX !== null && hoverX > width - 180 ? hoverX - 172 : (hoverX ?? 0) + 12

  return (
    <div
      ref={wrapRef}
      role="group"
      tabIndex={0}
      aria-label={label}
      aria-describedby={liveId}
      onKeyDown={onKeyDown}
      onPointerMove={(event) => setHover(pointAt(event.clientX))}
      onPointerLeave={() => setHover(null)}
      onBlur={() => setHover(null)}
      onClick={(event) => pick(pointAt(event.clientX))}
      className={cx('relative w-full select-none rounded-md outline-offset-0', onPick && 'cursor-crosshair', className)}
      style={{ height }}
    >
      <canvas ref={canvasRef} aria-hidden className="absolute inset-0 h-full w-full" />
      {cursorX !== null ? (
        <span
          aria-hidden
          className="pointer-events-none absolute w-px bg-accent"
          style={{ left: Math.round(cursorX), top: geometry.plotTop - 2, height: geometry.plotHeight + 4 }}
        />
      ) : null}
      {hovered && hoverX !== null ? (
        <>
          <span
            aria-hidden
            className="pointer-events-none absolute w-px bg-line-strong"
            style={{ left: Math.round(hoverX), top: geometry.plotTop - 2, height: geometry.plotHeight + RUG + 4 }}
          />
          {hoverValue !== null ? (
            <span
              aria-hidden
              className={cx(
                'pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-canvas',
                hoverValue < 0 ? 'bg-[var(--daq-neg)]' : 'bg-[var(--daq-pos)]',
              )}
              style={{ left: hoverX, top: y(hoverValue) }}
            />
          ) : null}
          <div
            aria-hidden
            className="pointer-events-none absolute z-10 w-40 rounded-lg border border-line bg-elevated px-2.5 py-2 shadow-popover"
            style={compact ? { left: Math.max(0, tooltipLeft), bottom: height + 6 } : { left: Math.max(0, tooltipLeft), top: geometry.plotTop }}
          >
            <div className="flex items-baseline gap-1.5">
              <span className="text-sm font-semibold tabular-nums text-fg">{hoverValue === null ? 'Not scored' : formatScore(hoverValue)}</span>
              {hoverValue === null ? null : <span className="truncate text-xs text-muted">{METRICS[metric].label}</span>}
            </div>
            <div className="mt-0.5 truncate text-xs text-fg">
              {hovered.resname} {hovered.resnum}
              {hovered.icode ? hovered.icode : ''}
              <span className="text-muted"> in chain {hoveredSegment.chain.id}</span>
            </div>
            {residueValue(hovered, other) !== null ? (
              <div className="mt-0.5 flex justify-between text-xs text-muted">
                <span>{METRICS[other].label}</span>
                <span className="tabular-nums text-fg">{formatScore(residueValue(hovered, other))}</span>
              </div>
            ) : null}
            {!hovered.scored && hoverValue !== null ? <div className="mt-0.5 text-xs text-muted">Not scored by DAQ</div> : null}
          </div>
        </>
      ) : null}
      <span id={liveId} className="sr-only" aria-live="polite">
        {hovered
          ? `${hovered.resname} ${hovered.resnum} in chain ${hoveredSegment.chain.id}: ${hoverValue === null ? 'not scored' : formatScore(hoverValue)}`
          : 'Use the arrow keys to read residues and Enter to show one in the viewer.'}
      </span>
    </div>
  )
}
