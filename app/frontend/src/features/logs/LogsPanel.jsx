// The agent process's own output for the open session: what the driver and
// the agent print while they work. Follows the end of the log while the
// session runs, like `tail -f`.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, Check, Copy, ScrollText, Search, WrapText, X } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { toDate } from '../../lib/format'
import { useApp, useCurrentSession } from '../../store/app'
import { useCopy } from '../../components/CodeBlock'
import { Button, EmptyState, IconButton, InlineError, Spinner } from '../../components/ui'

const POLL_MS = 2000
const PAGE_SIZE = 2000 // the server returns at most this many lines per request
const KEEP_LINES = 50000
const RENDER_CHUNK = 1500

// Drivers send everything they print to stderr, so the stream says little;
// colour lines by what they report instead.
const ERROR_PATTERN = /\b(error|errors|exception|traceback|failed|failure|fatal|critical)\b/i
const WARNING_PATTERN = /\b(warn|warning|warnings|deprecated)\b/i
// eslint-disable-next-line no-control-regex
const ANSI_PATTERN = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)/g

function cleanText(text) {
  const plain = String(text ?? '').replace(ANSI_PATTERN, '')
  // Progress bars rewrite their line with \r; keep what was shown last.
  const index = plain.lastIndexOf('\r', plain.length - 2)
  return index === -1 ? plain.replace(/\r$/, '') : plain.slice(index + 1).replace(/\r$/, '')
}

function lineTone(text) {
  if (ERROR_PATTERN.test(text)) return 'error'
  if (WARNING_PATTERN.test(text)) return 'warning'
  return 'plain'
}

function clock(value) {
  const date = toDate(value)
  if (!date) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

export function LogsPanel({ active }) {
  const session = useCurrentSession()
  const bottomOpen = useApp((state) => state.bottomOpen)
  if (!session) {
    return (
      <EmptyState icon={ScrollText} title="No session open">
        Open a session to follow what its agent process prints while it works.
      </EmptyState>
    )
  }
  return <SessionLog key={session.id} session={session} shown={active && bottomOpen} />
}

function SessionLog({ session, shown }) {
  const running = Boolean(session.runtime?.running)
  const [lines, setLines] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')
  const [wrap, setWrap] = useState(true)
  const [limit, setLimit] = useState(RENDER_CHUNK)
  const [pinned, setPinned] = useState(true)
  const [copied, copy] = useCopy()
  const cursorRef = useRef(0)
  const fetchingRef = useRef(false)
  const againRef = useRef(false)
  const scrollRef = useRef(null)

  /** Reads every line after the cursor, a page at a time. A call during a read runs once more after it. */
  const fetchNew = useCallback(async () => {
    if (fetchingRef.current) {
      againRef.current = true
      return
    }
    fetchingRef.current = true
    const added = []
    let cursor = cursorRef.current
    try {
      do {
        againRef.current = false
        for (let page = 0; page < 25; page += 1) {
          const data = await api.console(session.id, cursor)
          const batch = data.lines || []
          added.push(...batch)
          cursor = data.cursor ?? batch[batch.length - 1]?.id ?? cursor
          if (batch.length < PAGE_SIZE) break
        }
      } while (againRef.current)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      // Pages read before a failure are kept, and the cursor moves past exactly those lines.
      if (added.length) {
        const prepared = added.map((line) => {
          const text = cleanText(line.text)
          return { id: line.id, text, time: clock(line.created_at), stream: line.stream, tone: lineTone(text) }
        })
        setLines((current) => {
          const next = current.concat(prepared)
          return next.length > KEEP_LINES ? next.slice(next.length - KEEP_LINES) : next
        })
      }
      cursorRef.current = cursor
      fetchingRef.current = false
      setLoaded(true)
    }
  }, [session.id])

  // Catch up when the panel is shown; follow the log while the agent runs.
  // A last read after it stops picks up the final lines.
  useEffect(() => {
    if (!shown) return undefined
    fetchNew()
    if (!running) return undefined
    const timer = setInterval(fetchNew, POLL_MS)
    return () => clearInterval(timer)
  }, [shown, running, fetchNew])

  const query = filter.trim().toLowerCase()
  const matching = useMemo(() => (query ? lines.filter((line) => line.text.toLowerCase().includes(query)) : lines), [lines, query])
  const hidden = Math.max(0, matching.length - limit)
  const visible = hidden ? matching.slice(hidden) : matching

  // Stay at the end while the reader is there; otherwise leave the scroll alone.
  useLayoutEffect(() => {
    const element = scrollRef.current
    if (element && pinned) element.scrollTop = element.scrollHeight
  })

  const onScroll = () => {
    const element = scrollRef.current
    if (!element) return
    setPinned(element.scrollHeight - element.scrollTop - element.clientHeight < 32)
  }

  const jumpToLatest = () => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
    setPinned(true)
  }

  const copyAll = () => copy(matching.map((line) => line.text).join('\n'))

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-1 border-b border-line px-2">
        <label className="flex h-7 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-sm hover:bg-subtle focus-within:bg-subtle focus-within:ring-1 focus-within:ring-line-strong">
          <Search size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setFilter('')
            }}
            placeholder="Filter output"
            aria-label="Filter output"
            className="min-w-0 flex-1 bg-transparent text-fg outline-none placeholder:text-faint"
          />
          {filter ? (
            <>
              <span className="shrink-0 text-xs tabular-nums text-faint">{matching.length.toLocaleString()}</span>
              <button type="button" aria-label="Clear filter" onClick={() => setFilter('')} className="text-faint hover:text-fg">
                <X size={13} />
              </button>
            </>
          ) : null}
        </label>
        <IconButton icon={WrapText} label={wrap ? 'Do not wrap lines' : 'Wrap lines'} size="sm" active={wrap} onClick={() => setWrap((value) => !value)} />
        <IconButton icon={copied ? Check : Copy} label={query ? 'Copy matching lines' : 'Copy all'} size="sm" disabled={!matching.length} onClick={copyAll} />
      </div>

      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} onScroll={onScroll} className="absolute inset-0 overflow-auto py-2 font-mono text-[12px] leading-[1.6]">
          {!loaded ? (
            <div className="flex h-full items-center justify-center">{shown ? <Spinner /> : null}</div>
          ) : !lines.length ? (
            error ? (
              <div className="p-3">
                <InlineError>{error}</InlineError>
              </div>
            ) : (
              <EmptyState icon={ScrollText} title="No output yet" className="font-sans">
                {running
                  ? 'The agent process has not printed anything yet.'
                  : 'Output appears here while the agent process runs in this app.'}
              </EmptyState>
            )
          ) : (
            <>
              {hidden ? (
                <div className="px-3 pb-2 font-sans">
                  <Button size="xs" variant="ghost" onClick={() => setLimit((value) => value + RENDER_CHUNK)}>
                    Show {Math.min(hidden, RENDER_CHUNK).toLocaleString()} earlier lines
                  </Button>
                </div>
              ) : null}
              {query && !matching.length ? <p className="px-3 font-sans text-sm text-muted">No line matches “{filter.trim()}”.</p> : null}
              <div className={cx(!wrap && 'w-max min-w-full')}>
                {visible.map((line) => (
                  <LogLine key={line.id} line={line} wrap={wrap} />
                ))}
              </div>
              {error ? <p className="px-3 pt-2 font-sans text-xs text-danger">{error}</p> : null}
            </>
          )}
        </div>
        {!pinned && lines.length ? (
          <button
            type="button"
            onClick={jumpToLatest}
            className="absolute bottom-3 left-1/2 flex h-7 -translate-x-1/2 items-center gap-1.5 rounded-full border border-line bg-elevated px-3 text-xs font-medium text-fg shadow-popover hover:bg-hover"
          >
            <ArrowDown size={13} strokeWidth={1.75} />
            Jump to latest
          </button>
        ) : null}
      </div>
    </div>
  )
}

const LogLine = memo(function LogLine({ line, wrap }) {
  return (
    <div className="flex gap-3 px-3 hover:bg-hover">
      <span className="shrink-0 select-none tabular-nums text-faint">{line.time}</span>
      <span
        className={cx(
          'min-w-0',
          wrap ? 'flex-1 whitespace-pre-wrap break-words' : 'whitespace-pre',
          line.tone === 'error' ? 'text-danger' : line.tone === 'warning' ? 'text-warning' : 'text-fg',
        )}
      >
        {line.text || ' '}
      </span>
    </div>
  )
})
