// Live view of one session: its transcript, agent status, and the modeling
// agent's streaming text. Loads history over REST, then follows the SSE feed
// from where the history ended. Status changes are merged into the store so
// the sidebar and the header follow along.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { isActiveState } from '../lib/format'
import { useApp } from '../store/app'

/** The trace endpoint returns at most this many reconstruction items per call. */
const TRACE_PAGE = 5000
/** Sub-agent metadata is not streamed; it is polled while the agent works. */
const RECURSIVE_POLL_MS = 8000
/** After this many dropped connections in a row, ask the server why over REST. */
const PROBE_AFTER_FAILURES = 3
const EMPTY_PENDING = { reasoning: '', answer: '' }

function mergeById(existing, incoming) {
  if (!incoming?.length) return existing
  const seen = new Set(existing.map((entry) => entry.id))
  const additions = incoming.filter((entry) => !seen.has(entry.id))
  if (!additions.length) return existing
  const merged = [...existing, ...additions]
  // A live event can arrive before the catch-up request that fetches the
  // records published just before it; keep the records in id order so tool
  // calls still precede their outputs.
  const last = existing[existing.length - 1]?.id
  if (additions.some((entry) => entry.id < last)) merged.sort((a, b) => a.id - b.id)
  return merged
}

function statusFromRuntime(runtime) {
  return {
    state: runtime?.state || 'stopped',
    detail: runtime?.detail || '',
    running: Boolean(runtime?.running),
  }
}

/**
 * Returns {status, items, events, recursiveCalls, pending, loading, error,
 * agentError, lastTurn, activeSince, refresh, reload}.
 *   items/recursiveCalls  reconstruction transcript (trace.py nodes)
 *   events                modeling events
 *   pending               streaming modeling text {reasoning, answer}
 *   error                 the history could not be loaded
 *   agentError            an error the agent reported on the live feed
 *   activeSince           ms timestamp when this view saw the agent start working
 */
export function useSessionStream(session) {
  const sessionId = session?.id
  const kind = session?.kind
  const [status, setStatus] = useState(() => statusFromRuntime(session?.runtime))
  const [items, setItems] = useState([])
  const [events, setEvents] = useState([])
  const [recursiveCalls, setRecursiveCalls] = useState([])
  const [pending, setPending] = useState(EMPTY_PENDING)
  const [loading, setLoading] = useState(Boolean(sessionId))
  const [error, setError] = useState('')
  const [agentError, setAgentError] = useState('')
  const [lastTurn, setLastTurn] = useState(null)
  const [activeSince, setActiveSince] = useState(null)
  const [attempt, setAttempt] = useState(0)

  const cursorRef = useRef(0)
  // null until the first status event: a snapshot of a turn already under
  // way does not tell when that turn started.
  const activeRef = useRef(null)
  // Streaming deltas arrive per token; they are buffered and flushed once per frame.
  const deltaRef = useRef({ reasoning: '', answer: '' })
  const frameRef = useRef(0)

  const applyTrace = useCallback((trace) => {
    if (trace.kind === 'modeling') {
      setEvents((current) => mergeById(current, trace.events))
    } else {
      setItems((current) => mergeById(current, trace.items))
      if (trace.recursive_calls) setRecursiveCalls(trace.recursive_calls)
    }
    if (trace.cursor) cursorRef.current = Math.max(cursorRef.current, trace.cursor)
  }, [])

  const refreshTail = useCallback(async () => {
    if (!sessionId) return
    try {
      applyTrace(await api.trace(sessionId, cursorRef.current))
    } catch {
      // transient; the live feed or the next refresh catches up
    }
  }, [sessionId, applyTrace])

  useEffect(() => {
    if (!sessionId) {
      setLoading(false)
      return undefined
    }
    let cancelled = false
    let source = null
    let retryTimer = 0
    let failures = 0
    cursorRef.current = 0
    activeRef.current = null
    deltaRef.current = { reasoning: '', answer: '' }
    setItems([])
    setEvents([])
    setRecursiveCalls([])
    setPending(EMPTY_PENDING)
    setError('')
    setAgentError('')
    setLoading(true)

    const flushDeltas = () => {
      frameRef.current = 0
      const buffered = deltaRef.current
      if (!buffered.reasoning && !buffered.answer) return
      deltaRef.current = { reasoning: '', answer: '' }
      setPending((current) => ({
        reasoning: current.reasoning + buffered.reasoning,
        answer: current.answer + buffered.answer,
      }))
    }

    const clearPending = (channel) => {
      if (channel) {
        deltaRef.current = { ...deltaRef.current, [channel]: '' }
        setPending((current) => (current[channel] ? { ...current, [channel]: '' } : current))
      } else {
        deltaRef.current = { reasoning: '', answer: '' }
        setPending(EMPTY_PENDING)
      }
    }

    const onStatus = (event) => {
      const next = { state: event.state || 'stopped', detail: event.detail || '', running: Boolean(event.running) }
      const active = next.running && isActiveState(next.state)
      if (active && activeRef.current === false) setActiveSince(Date.now())
      if (!active) {
        setActiveSince(null)
        clearPending()
      }
      if (active) setAgentError('')
      activeRef.current = active
      setStatus(next)
      // "stopped" only says no process is attached; keep the session's own
      // last state (completed, error, ...) as its status.
      useApp.getState().patchSession({
        id: sessionId,
        ...(next.state && next.state !== 'stopped' ? { status: next.state } : {}),
        runtime: { running: next.running, state: next.state, detail: next.detail },
      })
    }

    const handle = (message) => {
      let event
      try {
        event = JSON.parse(message.data)
      } catch {
        return
      }
      switch (event.type) {
        case 'status':
          onStatus(event)
          break
        case 'trace':
          setItems((current) => mergeById(current, event.items || []))
          if (event.cursor) cursorRef.current = Math.max(cursorRef.current, event.cursor)
          break
        case 'event': {
          const record = event.event
          if (!record) break
          setEvents((current) => mergeById(current, [record]))
          if (record.id) cursorRef.current = Math.max(cursorRef.current, record.id)
          if (record.kind === 'answer') clearPending('answer')
          if (record.kind === 'reasoning') clearPending('reasoning')
          break
        }
        case 'delta': {
          const channel = event.channel === 'answer' ? 'answer' : 'reasoning'
          deltaRef.current = { ...deltaRef.current, [channel]: deltaRef.current[channel] + (event.text || '') }
          if (!frameRef.current) frameRef.current = requestAnimationFrame(flushDeltas)
          break
        }
        case 'turn_end':
          clearPending()
          setLastTurn({ outcome: event.outcome, at: Date.now() })
          break
        case 'session':
          if (event.session) useApp.getState().patchSession(event.session)
          break
        case 'error':
          setAgentError(event.message || 'The agent reported an error.')
          break
        default:
          break
      }
    }

    // EventSource would reconnect with the cursor it was opened with and
    // replay everything since; reopen it from the current cursor instead.
    const connect = () => {
      if (cancelled) return
      source = new EventSource(api.eventsUrl(sessionId, cursorRef.current))
      source.onmessage = handle
      source.onopen = () => {
        failures = 0
        refreshTail()
      }
      source.onerror = () => {
        source?.close()
        source = null
        if (cancelled) return
        failures += 1
        const retry = () => {
          if (!cancelled) retryTimer = window.setTimeout(connect, Math.min(10_000, 1000 * 2 ** Math.min(failures - 1, 4)))
        }
        if (failures < PROBE_AFTER_FAILURES) {
          retry()
          return
        }
        // EventSource does not expose the status code. A REST request does:
        // a 401 there sends the user back to the access gate, and a 404 means
        // the session was deleted, so there is nothing left to follow.
        api.session(sessionId).then(retry, (err) => {
          if (cancelled) return
          if (err.status === 404) setError('This session no longer exists.')
          else retry()
        })
      }
    }

    const loadHistory = async () => {
      let after = 0
      for (;;) {
        const trace = await api.trace(sessionId, after)
        if (cancelled) return
        applyTrace(trace)
        const page = trace.kind === 'modeling' ? trace.events : trace.items
        if (trace.kind === 'modeling' || page.length < TRACE_PAGE || !trace.cursor || trace.cursor <= after) return
        after = trace.cursor
      }
    }

    loadHistory()
      .then(() => {
        if (cancelled) return
        setLoading(false)
        connect()
      })
      .catch((err) => {
        if (cancelled) return
        setError(err.message || 'The transcript could not be loaded.')
        setLoading(false)
      })

    return () => {
      cancelled = true
      window.clearTimeout(retryTimer)
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
      frameRef.current = 0
      source?.close()
    }
  }, [sessionId, attempt, applyTrace, refreshTail])

  // Sub-agent metadata is not streamed; poll it while the agent works. The
  // refresh on turn_end picks up the final state.
  const working = status.running && isActiveState(status.state)
  useEffect(() => {
    if (!sessionId || kind !== 'reconstruction' || !working) return undefined
    const timer = window.setInterval(refreshTail, RECURSIVE_POLL_MS)
    return () => window.clearInterval(timer)
  }, [sessionId, kind, working, refreshTail])

  useEffect(() => {
    if (lastTurn) refreshTail()
  }, [lastTurn, refreshTail])

  const reload = useCallback(() => setAttempt((value) => value + 1), [])

  return {
    status,
    items,
    events,
    recursiveCalls,
    pending,
    loading,
    error,
    agentError,
    lastTurn,
    activeSince,
    refresh: refreshTail,
    reload,
  }
}
