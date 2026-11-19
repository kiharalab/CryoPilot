// A session's run report: what it produced and how, with its FSC curves, a
// Methods draft to copy, and downloads of the report and the transcript.
import { useEffect, useMemo, useState } from 'react'
import { Check, Copy, Download } from 'lucide-react'
import { api } from '../../lib/api'
import { Markdown } from '../../components/Markdown'
import { Button, Dialog, DialogContent, InlineError, Skeleton } from '../../components/ui'

const CURVE_COLORS = ['var(--faint)', 'var(--muted)', '#d9a441', '#3f8fd2', '#3aa675', '#b45fb5']

/** FSC against resolution, with the 0.143 and 0.5 lines. */
function FscPlot({ curve }) {
  const width = 640
  const height = 240
  const left = 36
  const right = 12
  const top = 10
  const bottom = 34
  const { frequency, curves } = curve
  const maxFrequency = frequency[frequency.length - 1] || 1
  const x = (value) => left + (value / maxFrequency) * (width - left - right)
  const y = (value) => top + (1 - Math.min(1.05, Math.max(-0.05, value)) / 1.05) * (height - top - bottom)
  const ticks = useMemo(() => {
    const steps = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5]
    return steps.filter((value) => value < maxFrequency)
  }, [maxFrequency])
  return (
    <figure className="mb-4">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Fourier shell correlation against resolution" className="w-full">
        {[0, 0.143, 0.5, 1].map((value) => (
          <g key={value}>
            <line x1={left} x2={width - right} y1={y(value)} y2={y(value)} stroke="var(--line)" strokeDasharray={value === 0.143 || value === 0.5 ? '4 3' : undefined} />
            <text x={left - 5} y={y(value) + 3.5} textAnchor="end" fontSize="10" fill="var(--muted)">
              {value}
            </text>
          </g>
        ))}
        {ticks.map((value) => (
          <text key={value} x={x(value)} y={height - bottom + 13} textAnchor="middle" fontSize="10" fill="var(--muted)">
            {(1 / value).toFixed(1)}
          </text>
        ))}
        <text x={(left + width - right) / 2} y={height - 4} textAnchor="middle" fontSize="10" fill="var(--muted)">
          Resolution (Å)
        </text>
        {curves.map((entry, index) => (
          <polyline
            key={entry.label}
            points={entry.values.map((value, point) => `${x(frequency[point]).toFixed(1)},${y(value).toFixed(1)}`).join(' ')}
            fill="none"
            stroke={CURVE_COLORS[index % CURVE_COLORS.length]}
            strokeWidth="1.4"
          />
        ))}
      </svg>
      <figcaption className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted">
        {curves.map((entry, index) => (
          <span key={entry.label} className="inline-flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-3" style={{ background: CURVE_COLORS[index % CURVE_COLORS.length] }} />
            {entry.label}
          </span>
        ))}
      </figcaption>
    </figure>
  )
}

export function ReportDialog({ session, open, onOpenChange }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setReport(null)
    setError('')
    api
      .sessionReport(session.id)
      .then((result) => {
        if (!cancelled) setReport(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [open, session.id])

  const copyMethods = async () => {
    try {
      await navigator.clipboard.writeText(report.methods)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('This browser did not allow copying. Select the text in the report instead.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Run report" width="max-w-[880px]" className="h-[min(88vh,880px)]">
        <div className="mt-3 flex flex-wrap items-center gap-2 border-b border-line px-5 pb-3">
          <Button size="sm" variant="secondary" icon={copied ? Check : Copy} disabled={!report?.methods} onClick={copyMethods}>
            {copied ? 'Copied' : 'Copy Methods draft'}
          </Button>
          <a href={api.sessionReportUrl(session.id)} download className="contents">
            <Button size="sm" variant="ghost" icon={Download}>
              Report
            </Button>
          </a>
          <a href={api.sessionTranscriptUrl(session.id)} download className="contents">
            <Button size="sm" variant="ghost" icon={Download}>
              Transcript
            </Button>
          </a>
          <a href={api.sessionTranscriptUrl(session.id, 'jsonl')} download className="contents">
            <Button size="sm" variant="ghost" icon={Download}>
              Transcript records
            </Button>
          </a>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {error ? <InlineError>{error}</InlineError> : null}
          {!report && !error ? (
            <div className="flex flex-col gap-3" aria-busy="true" aria-label="Building the report">
              <Skeleton className="h-6 w-1/2" />
              <Skeleton className="h-4 w-11/12" />
              <Skeleton className="h-4 w-4/5" />
              <Skeleton className="mt-3 h-40 w-full" />
            </div>
          ) : null}
          {report?.fsc_curve ? <FscPlot curve={report.fsc_curve} /> : null}
          {report ? <Markdown>{report.markdown}</Markdown> : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
