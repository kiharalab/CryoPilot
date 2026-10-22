// The single quiet line under the transcript while an agent works: a pulsing
// dot, what it is doing, how long the turn has run, and for the modeling agent
// the reasoning summary as it streams in.
import { useEffect, useState } from 'react'
import { formatDuration, stateLabel } from '../../lib/format'
import { StatusDot } from '../../components/ui'

function useNow(interval) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!interval) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), interval)
    return () => window.clearInterval(timer)
  }, [interval])
  return now
}

/** The end of a streaming text, starting at a word, so the line shows what is being written now. */
function tail(text, limit = 360) {
  const value = String(text || '').trim()
  if (value.length <= limit) return value
  const cut = value.slice(value.length - limit)
  const space = cut.indexOf(' ')
  return `…${space > 0 && space < 40 ? cut.slice(space + 1) : cut}`
}

export function LiveLine({ state, since, reasoning = '', stopping = false }) {
  const now = useNow(since ? 1000 : 0)
  const elapsed = since ? Math.max(0, (now - since) / 1000) : null
  const label = stopping ? 'Stopping' : reasoning ? 'Thinking' : stateLabel(state)
  return (
    <div className="flex flex-col gap-1">
      <div className="flex h-6 items-center gap-2 text-sm text-muted">
        <StatusDot state={stopping ? 'interrupted' : state || 'running'} className="ml-[3px]" />
        <span role="status">{label}</span>
        {elapsed !== null ? <span className="tabular-nums text-faint">{formatDuration(elapsed)}</span> : null}
      </div>
      {reasoning ? <p className="ml-[15px] whitespace-pre-wrap break-words text-sm leading-relaxed text-faint">{tail(reasoning)}</p> : null}
    </div>
  )
}
