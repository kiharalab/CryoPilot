// The programs the modeling agent runs on this computer, as its own tools
// resolve them. The server checks them in the background, so the first look
// and every "Check again" poll /api/setup until a new result arrives.
import { useEffect, useRef, useState } from 'react'
import { CircleCheck, CircleMinus, CircleX, RefreshCw } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, Skeleton } from '../../components/ui'

const TOOLS = [
  { key: 'pymol', label: 'PyMOL', use: 'Renders maps and models' },
  { key: 'chimerax', label: 'ChimeraX', use: 'Simulates density and computes Q-scores' },
  { key: 'vesper', label: 'VESPER', use: 'Aligns models to maps' },
  { key: 'phenix', label: 'Phenix', use: 'Real-space refinement and validation', optional: true },
  { key: 'rosetta', label: 'Rosetta', use: 'Density-guided refinement', optional: true },
  { key: 'rosetta_database', label: 'Rosetta database', use: 'Needed by Rosetta', optional: true },
  { key: 'ripgrep', label: 'ripgrep', use: 'Fast search in the workspace', optional: true },
]

const POLL_MS = 2500
// The server gives one check up to two minutes.
const POLL_LIMIT_MS = 150000

export function ToolchainStatus({ className }) {
  const tools = useApp((state) => state.setup?.modeling_tools)
  const toast = useApp((state) => state.toast)
  const [checking, setChecking] = useState(false)
  const [stalled, setStalled] = useState(false)
  const previousCheck = useRef(null)

  const waiting = (!tools || checking) && !stalled
  useEffect(() => {
    if (!waiting) return undefined
    let cancelled = false
    const started = Date.now()
    const timer = setInterval(async () => {
      if (Date.now() - started > POLL_LIMIT_MS) {
        clearInterval(timer)
        setChecking(false)
        setStalled(true)
        return
      }
      try {
        const next = await api.setup()
        const result = next.modeling_tools
        if (cancelled || !result || (checking && result.checked_at === previousCheck.current)) return
        const current = useApp.getState().setup
        useApp.getState().setSetup({ ...(current || next), modeling_tools: result })
        setChecking(false)
      } catch {
        // the next poll tries again
      }
    }, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [waiting, checking])

  const refresh = async () => {
    previousCheck.current = tools?.checked_at ?? null
    setStalled(false)
    setChecking(true)
    try {
      await api.refreshModelingTools()
    } catch (err) {
      setChecking(false)
      toast({ title: 'Could not check the modeling toolchain', description: err.message, tone: 'error' })
    }
  }

  return (
    <div className={cx('flex flex-col gap-2', className)}>
      <div className="overflow-hidden rounded-xl border border-line">
        {TOOLS.map((tool, index) => (
          <ToolRow key={tool.key} tool={tool} status={tools?.[tool.key]} loading={!tools} first={index === 0} />
        ))}
      </div>
      <div className="flex min-h-7 items-center justify-between gap-3">
        <p className="text-xs text-muted">
          {waiting
            ? 'Checking...'
            : tools?.checked_at
              ? `Checked ${formatRelative(tools.checked_at)}`
              : 'The check has not finished yet. Try again in a moment.'}
        </p>
        <Button size="sm" variant="ghost" icon={RefreshCw} loading={waiting} onClick={refresh}>
          Check again
        </Button>
      </div>
    </div>
  )
}

function ToolRow({ tool, status, loading, first }) {
  const ok = Boolean(status?.ok)
  const Icon = ok ? CircleCheck : tool.optional ? CircleMinus : CircleX
  const detail = ok ? status.path : status?.error || 'Not found'
  return (
    <div className={cx('flex items-start gap-3 px-3.5 py-2.5', !first && 'border-t border-line')}>
      {loading ? (
        <Skeleton className="mt-0.5 h-4 w-4 rounded-full" />
      ) : (
        <Icon
          size={16}
          strokeWidth={1.75}
          className={cx('mt-0.5 shrink-0', ok ? 'text-success' : tool.optional ? 'text-faint' : 'text-danger')}
          aria-label={ok ? 'Found' : 'Missing'}
        />
      )}
      <div className="min-w-0 flex-1">
        <p className="flex items-baseline gap-2 text-sm">
          <span className="font-medium text-fg">{tool.label}</span>
          <span className="truncate text-muted">{tool.use}</span>
          {tool.optional ? <span className="ml-auto shrink-0 text-xs text-faint">Optional</span> : null}
        </p>
        {loading ? (
          <Skeleton className="mt-1.5 h-3 w-3/5" />
        ) : (
          <p
            title={detail}
            className={cx(
              'mt-0.5 line-clamp-2 break-all text-xs',
              ok ? 'font-mono text-muted' : tool.optional ? 'text-muted' : 'text-danger',
            )}
          >
            {detail}
          </p>
        )}
      </div>
    </div>
  )
}
