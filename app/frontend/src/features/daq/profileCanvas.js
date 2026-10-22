// Drawing for the DAQ profile, kept apart from React: layout of chains along
// x, the y scale, hit testing, and the canvas paint. DaqProfile.jsx owns the
// element, hover, and keyboard.
import { residueValue } from './store'

export const FULL_MARGIN = { left: 34, right: 10, top: 8, bottom: 22 }
export const COMPACT_MARGIN = { left: 10, right: 10, top: 16, bottom: 8 }
export const COMPACT_GAP = 14
export const RUG = 6

/** −0.42 / 0.42 with a typographic minus. */
export function formatScore(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–'
  const text = Math.abs(value).toFixed(digits)
  return value < 0 && Number(text) !== 0 ? `−${text}` : text
}

/** Symmetric y-range shared by every chain: at least ±1.5, rounded up to 0.5. */
export function profileDomain(profile, metric) {
  const extent = profile?.extent?.[metric] || 0
  return Math.max(1.5, Math.ceil(extent * 2) / 2)
}

export function readTokens() {
  const style = getComputedStyle(document.documentElement)
  const token = (name) => style.getPropertyValue(name).trim()
  return {
    neg: token('--daq-neg'),
    pos: token('--daq-pos'),
    strong: token('--line-strong'),
    faint: token('--faint'),
    muted: token('--muted'),
    font: getComputedStyle(document.body).fontFamily,
  }
}

export function niceStep(span, slots) {
  const raw = span / Math.max(1, slots)
  const power = 10 ** Math.floor(Math.log10(Math.max(raw, 1)))
  for (const factor of [1, 2, 5, 10]) if (factor * power >= raw) return factor * power
  return 10 * power
}

export function layoutSegments(chains, width, height, compact) {
  const margin = compact ? COMPACT_MARGIN : FULL_MARGIN
  const gap = compact ? COMPACT_GAP : 0
  const inner = Math.max(0, width - margin.left - margin.right - gap * Math.max(0, chains.length - 1))
  const spans = chains.map((chain) => Math.max(1, chain.end - chain.start + 1))
  const total = spans.reduce((sum, span) => sum + span, 0) || 1
  let x = margin.left
  const segments = chains.map((chain, index) => {
    const segment = { chain, x0: x, w: (inner * spans[index]) / total, span: spans[index] }
    x += segment.w + gap
    return segment
  })
  const plotTop = margin.top
  const plotHeight = Math.max(8, height - margin.top - margin.bottom - RUG)
  return { margin, gap, segments, plotTop, plotHeight, width, height }
}

export function xOf(segment, resnum) {
  return segment.x0 + ((resnum - segment.chain.start + 0.5) / segment.span) * segment.w
}

export function yScale(geometry, domain) {
  return (value) => {
    const clamped = Math.max(-domain, Math.min(domain, value))
    return geometry.plotTop + (1 - (clamped + domain) / (2 * domain)) * geometry.plotHeight
  }
}

/** Nearest residue (by number) to an x position, as {segment, index}. */
export function hitTest(geometry, x) {
  const { segments, gap } = geometry
  if (!segments.length) return null
  let segmentIndex = segments.findIndex((segment) => x >= segment.x0 - gap / 2 && x <= segment.x0 + segment.w + gap / 2)
  if (segmentIndex === -1) segmentIndex = x < segments[0].x0 ? 0 : segments.length - 1
  const segment = segments[segmentIndex]
  const residues = segment.chain.residues
  const target = segment.chain.start + ((x - segment.x0) / Math.max(1, segment.w)) * segment.span - 0.5
  let low = 0
  let high = residues.length - 1
  while (low < high) {
    const middle = (low + high) >> 1
    if (residues[middle].resnum < target) low = middle + 1
    else high = middle
  }
  if (low > 0 && Math.abs(residues[low - 1].resnum - target) <= Math.abs(residues[low].resnum - target)) low -= 1
  return { segment: segmentIndex, index: low }
}

export function drawProfile(canvas, geometry, { metric, domain, compact, tokens }) {
  const dpr = window.devicePixelRatio || 1
  const { width, height, segments, plotTop, plotHeight } = geometry
  canvas.width = Math.round(width * dpr)
  canvas.height = Math.round(height * dpr)
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)
  const y = yScale(geometry, domain)
  const zero = Math.round(y(0))
  const bottom = plotTop + plotHeight
  ctx.font = `11px ${tokens.font}`
  ctx.textBaseline = 'middle'

  for (const segment of segments) {
    const { chain } = segment
    const residues = chain.residues
    const residueWidth = segment.w / segment.span
    const area = new Path2D()
    const line = new Path2D()
    let run = []
    const flush = () => {
      if (!run.length) return
      if (run.length === 1) {
        const [px, py] = run[0]
        const half = Math.max(0.75, residueWidth / 2)
        area.rect(px - half, Math.min(py, zero), half * 2, Math.abs(py - zero))
        line.moveTo(px - half, py)
        line.lineTo(px + half, py)
      } else {
        area.moveTo(run[0][0], zero)
        for (const [px, py] of run) area.lineTo(px, py)
        area.lineTo(run[run.length - 1][0], zero)
        area.closePath()
        line.moveTo(run[0][0], run[0][1])
        for (let index = 1; index < run.length; index += 1) line.lineTo(run[index][0], run[index][1])
      }
      run = []
    }
    residues.forEach((residue, index) => {
      const value = residueValue(residue, metric)
      const previous = residues[index - 1]
      if (value === null || (previous && residue.resnum - previous.resnum > 1)) flush()
      if (value !== null) run.push([xOf(segment, residue.resnum), y(value)])
    })
    flush()

    // Above zero in blue, below in red: fill and stroke the same paths in two clips.
    const dense = residueWidth < 1.5
    for (const [top, bandBottom, color] of [
      [plotTop - 2, zero, tokens.pos],
      [zero, bottom + 2, tokens.neg],
    ]) {
      ctx.save()
      ctx.beginPath()
      ctx.rect(segment.x0 - 1, top, segment.w + 2, bandBottom - top)
      ctx.clip()
      ctx.globalAlpha = 0.16
      ctx.fillStyle = color
      ctx.fill(area)
      ctx.globalAlpha = 1
      ctx.strokeStyle = color
      ctx.lineWidth = dense ? 1.25 : 1.75
      ctx.lineJoin = 'round'
      ctx.lineCap = 'round'
      ctx.stroke(line)
      ctx.restore()
    }

    ctx.fillStyle = tokens.strong
    ctx.fillRect(segment.x0, zero, segment.w, 1)

    // Residues the model has but DAQ did not score.
    ctx.fillStyle = tokens.faint
    const tick = Math.max(1, Math.min(residueWidth, 3))
    for (const residue of residues) {
      if (!residue.scored) ctx.fillRect(xOf(segment, residue.resnum) - tick / 2, bottom + 2, tick, RUG - 2)
    }

    if (compact) {
      if (segment.w >= 14) {
        ctx.fillStyle = tokens.muted
        ctx.textAlign = 'left'
        ctx.fillText(chain.id || '–', segment.x0, geometry.margin.top / 2)
      }
    } else {
      ctx.fillStyle = tokens.muted
      ctx.textAlign = 'center'
      ctx.textBaseline = 'alphabetic'
      const step = niceStep(segment.span, segment.w / 72)
      for (let resnum = Math.ceil(chain.start / step) * step; resnum <= chain.end; resnum += step) {
        const px = xOf(segment, resnum)
        const text = String(resnum)
        // Keep end labels inside the canvas.
        const half = ctx.measureText(text).width / 2
        ctx.fillRect(Math.round(px), bottom + RUG, 1, 3)
        ctx.fillText(text, Math.min(width - half - 1, Math.max(half + 1, px)), height - 4)
      }
      ctx.textBaseline = 'middle'
    }
  }

  if (!compact) {
    ctx.fillStyle = tokens.muted
    ctx.textAlign = 'right'
    const label = geometry.margin.left - 6
    ctx.fillText(formatScore(domain, domain % 1 ? 1 : 0), label, plotTop + 4)
    ctx.fillText('0', label, zero)
    ctx.fillText(formatScore(-domain, domain % 1 ? 1 : 0), label, bottom - 4)
  }
}
