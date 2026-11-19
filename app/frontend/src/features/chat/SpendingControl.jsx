// What a session has cost so far, and its spending limit: a small figure beside
// the model name that opens the details, and the card shown while a run is
// paused at its limit.
import { useEffect, useState } from 'react'
import { CircleDollarSign } from 'lucide-react'
import { api } from '../../lib/api'
import { formatTokens, formatUsd } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, Input, Popover, PopoverContent, PopoverTrigger } from '../../components/ui'

function useLimitSaver(session) {
  const patchSession = useApp((state) => state.patchSession)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const save = async (text) => {
    setBusy(true)
    setError('')
    try {
      const value = String(text ?? '').trim()
      patchSession(await api.setSpendingLimit(session.id, value === '' ? null : value))
      return true
    } catch (err) {
      setError(err.message)
      return false
    } finally {
      setBusy(false)
    }
  }
  return { save, busy, error }
}

/** A limit a little past what has been spent, as the suggested next limit. */
function nextLimit(usage) {
  const spent = Number(usage?.cost_usd) || 0
  const limit = Number(usage?.limit_usd) || 0
  return (Math.ceil(Math.max(spent, limit) * 1.5 + 1)).toFixed(2)
}

function LimitField({ id, value, onChange, disabled, onEnter }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-sm text-muted">$</span>
      <Input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') onEnter?.()
        }}
        inputMode="decimal"
        placeholder="No limit"
        disabled={disabled}
        className="w-24"
      />
    </div>
  )
}

/** Shown under the transcript while the agent holds its next model request. */
export function PausedCard({ session, detail }) {
  const usage = session.usage
  const [value, setValue] = useState(() => nextLimit(usage))
  const { save, busy, error } = useLimitSaver(session)
  return (
    <div className="mt-4 rounded-xl border border-line bg-elevated px-4 py-3">
      <p className="text-sm font-medium text-fg">Paused at the spending limit</p>
      <p className="mt-0.5 text-sm text-muted">
        {detail || `This session has used about ${formatUsd(usage?.cost_usd)}.`} The agent has lost nothing and is waiting to send its next request. Raise the limit
        to continue, or stop the agent.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <LimitField id="paused-limit" value={value} onChange={setValue} disabled={busy} onEnter={() => save(value)} />
        <Button size="sm" variant="primary" loading={busy} onClick={() => save(value)}>
          Raise the limit and continue
        </Button>
      </div>
      {error ? <p className="mt-1.5 text-sm text-danger">{error}</p> : null}
    </div>
  )
}

/** The cost figure in the message box. `open`/`onOpenChange` let the message box open it after a refused message. */
export function SpendingControl({ session, open, onOpenChange }) {
  const usage = session.usage
  const [value, setValue] = useState('')
  const { save, busy, error } = useLimitSaver(session)
  const limit = usage?.limit_usd

  useEffect(() => {
    if (open) setValue(limit ?? '')
  }, [open, limit])

  if (!usage || (!usage.requests && limit === null)) return null
  const known = !usage.unpriced_models?.length
  const figure = usage.requests ? formatUsd(usage.cost_usd) : '$0.00'

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Cost and spending limit"
          className={`mr-1 inline-flex select-none items-center gap-1 rounded-md px-1.5 py-0.5 text-xs tabular-nums transition-colors hover:bg-hover ${
            usage.over_limit ? 'text-warning' : 'text-faint hover:text-fg'
          }`}
        >
          <CircleDollarSign size={12} strokeWidth={1.75} />
          {figure.replace('$', '')}
          {!known ? '+' : ''}
          {limit !== null && limit !== undefined ? <span> of {formatUsd(limit).replace('$', '')}</span> : null}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" side="top" className="w-[320px] p-4">
        <p className="text-sm font-medium text-fg">Estimated cost: {figure}</p>
        <p className="mt-0.5 text-xs text-muted">From list prices as of {usage.prices_as_of}. Your provider's invoice is the exact figure.</p>
        {!known ? (
          <p className="mt-2 text-xs text-warning">
            {usage.unpriced_models.join(', ')} has no listed price in this version of CryoPilot, so the estimate leaves it out and the spending limit cannot stop it.
          </p>
        ) : null}
        <dl className="mt-3 grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted">Model requests</dt>
          <dd className="text-right tabular-nums text-fg">{usage.requests.toLocaleString()}</dd>
          <dt className="text-muted">Input tokens</dt>
          <dd className="text-right tabular-nums text-fg">{formatTokens(usage.input_tokens)}</dd>
          <dt className="text-muted">Cache reads</dt>
          <dd className="text-right tabular-nums text-fg">{formatTokens(usage.cached_tokens)}</dd>
          <dt className="text-muted">Cache writes</dt>
          <dd className="text-right tabular-nums text-fg">{formatTokens(usage.cache_write_tokens)}</dd>
          <dt className="text-muted">Output tokens</dt>
          <dd className="text-right tabular-nums text-fg">{formatTokens(usage.output_tokens)}</dd>
        </dl>
        <div className="mt-3 border-t border-line pt-3">
          <label htmlFor="session-limit" className="text-xs font-medium text-fg">
            Spending limit for this session
          </label>
          <p className="mt-0.5 text-xs text-muted">The run pauses before its next model request once it reaches this. A CryoSPARC job already running finishes first.</p>
          <div className="mt-2 flex items-center gap-2">
            <LimitField id="session-limit" value={value} onChange={setValue} disabled={busy} onEnter={() => save(value)} />
            <Button size="sm" variant="secondary" loading={busy} onClick={() => save(value)}>
              Save
            </Button>
            {limit !== null && limit !== undefined ? (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => save('')}>
                No limit
              </Button>
            ) : null}
          </div>
          {error ? <p className="mt-1.5 text-xs text-danger">{error}</p> : null}
        </div>
      </PopoverContent>
    </Popover>
  )
}
