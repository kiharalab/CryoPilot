// One conversation: the transcript of a reconstruction or modeling session,
// live while its agent works, with the message box underneath.
import { useEffect, useMemo, useRef, useState } from 'react'
import { MessageSquare, Play, SearchX } from 'lucide-react'
import { api } from '../../lib/api'
import { isActiveState, toDate } from '../../lib/format'
import { useApp, useCurrentSession } from '../../store/app'
import { Button, EmptyState, Skeleton } from '../../components/ui'
import { useSessionStream } from '../../hooks/useSessionStream'
import { CallsContext, ChatContext } from './context'
import { ChatComposer, SETUP_ACTIONS } from './ChatComposer'
import { LiveLine } from './LiveLine'
import { Notice, OutboxMessage, Prose } from './messages'
import { Transcript } from './Transcript'
import { buildModelingBlocks, buildReconstructionBlocks, stabilize } from './transcriptModel'

const COLUMN = 'mx-auto w-full max-w-[792px] px-4 sm:px-6'
/** Harness statuses of a reconstruction whose last turn did not finish ('completed' waits for a message). */
const UNFINISHED = new Set(['initializing', 'running', 'interrupted', 'error'])

export function SessionView({ sessionId }) {
  const stored = useCurrentSession()
  const [fetched, setFetched] = useState(null)
  const [missing, setMissing] = useState(null)
  const session = stored || fetched

  // Opened before the project list knew about it (a link, or a session
  // created elsewhere): fetch it and add it to the store.
  useEffect(() => {
    if (stored || fetched) return undefined
    let cancelled = false
    api
      .session(sessionId)
      .then((result) => {
        if (cancelled) return
        setFetched(result)
        const { projectId, patchSession, selectProject } = useApp.getState()
        if (result.project_id && result.project_id !== projectId) selectProject(result.project_id, { keepView: true }).catch(() => {})
        else patchSession(result)
      })
      .catch((err) => {
        if (!cancelled) setMissing(err.status === 404 ? 'This session no longer exists.' : err.message)
      })
    return () => {
      cancelled = true
    }
  }, [sessionId, stored, fetched])

  if (!session) {
    if (missing) {
      return (
        <EmptyState
          icon={SearchX}
          title="Session not found"
          action={
            <Button variant="secondary" onClick={() => useApp.getState().navigate({ name: 'home' })}>
              Start a new session
            </Button>
          }
        >
          {missing}
        </EmptyState>
      )
    }
    return <TranscriptSkeleton />
  }
  return <Conversation key={session.id} session={session} />
}

function TranscriptSkeleton() {
  return (
    <div className={`${COLUMN} flex flex-col gap-3 pt-10`} aria-busy="true" aria-label="Loading the conversation">
      <Skeleton className="ml-auto h-16 w-3/5 rounded-2xl" />
      <Skeleton className="mt-6 h-4 w-11/12" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-4 w-2/3" />
      <Skeleton className="mt-3 h-6 w-1/2" />
      <Skeleton className="h-6 w-2/5" />
    </div>
  )
}

/** A reconstruction stopped in the middle of a turn: start its agent again to finish that turn, no message needed. */
function ResumeLine({ sessionId }) {
  const openSettings = useApp((state) => state.openSettings)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const resume = async () => {
    setBusy(true)
    setError(null)
    try {
      useApp.getState().patchSession(await api.startSession(sessionId))
      // Stays busy: this line goes away once the agent reports that it runs.
    } catch (err) {
      setError({ message: err.message, setup: err.setup })
      setBusy(false)
    }
  }

  return (
    <div className="mt-3 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <p className="text-sm text-muted">The agent stopped before it finished its turn.</p>
        <Button size="sm" variant="secondary" icon={Play} loading={busy} onClick={resume}>
          Resume
        </Button>
      </div>
      {error ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <p className="text-sm text-danger">{error.message}</p>
          {error.setup ? (
            <Button size="sm" variant="ghost" onClick={() => openSettings('accounts')}>
              {SETUP_ACTIONS[error.setup] || 'Open settings'}
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function Conversation({ session }) {
  const stream = useSessionStream(session)
  const projectSlug = useApp((state) => state.project?.slug)
  const toast = useApp((state) => state.toast)
  const reconstructionSupported = useApp((state) => state.setup?.platform?.reconstruction_supported)
  const transcriptRef = useRef(null)
  const cacheRef = useRef(new Map())
  const [outbox, setOutbox] = useState(null)
  const [stopping, setStopping] = useState(false)
  const [resumable, setResumable] = useState(false)

  const kind = session.kind
  const harnessId = session.harness_session_id
  const workspace = session.config?.workspace || (projectSlug ? `workspaces/${projectSlug}` : null)
  const { state, running } = stream.status
  const active = running && isActiveState(state)

  // A reconstruction whose agent process went away mid-turn (a server restart,
  // a crash, or a restart for new credentials) finishes that turn when it
  // starts again. The stored session may predate the last turn, so ask the
  // server for the harness status each time the process stops.
  useEffect(() => {
    if (kind !== 'reconstruction' || !harnessId || running || reconstructionSupported === false) {
      setResumable(false)
      return undefined
    }
    let cancelled = false
    api
      .session(session.id)
      .then((fresh) => {
        if (cancelled) return
        useApp.getState().patchSession(fresh)
        setResumable(!fresh.runtime?.running && UNFINISHED.has(fresh.harness?.status))
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [kind, session.id, harnessId, running, state, reconstructionSupported])

  const built = useMemo(
    () =>
      kind === 'modeling'
        ? buildModelingBlocks(stream.events)
        : buildReconstructionBlocks(stream.items, { sessionId: session.id, calls: stream.recursiveCalls }),
    [kind, session.id, stream.events, stream.items, stream.recursiveCalls],
  )
  const blocks = useMemo(() => {
    const result = stabilize(built.blocks, cacheRef.current)
    cacheRef.current = result.cache
    return result.blocks
  }, [built])

  const chat = useMemo(
    () => ({ sessionId: session.id, projectId: session.project_id, kind, workspace }),
    [session.id, session.project_id, kind, workspace],
  )

  // The optimistic copy of a sent message leaves once the transcript has it,
  // or once the turn it started is over without recording it (it failed).
  useEffect(() => {
    if (!outbox) return
    if (built.userCount > outbox.baseline) setOutbox(null)
    else if (active && !outbox.started) setOutbox({ ...outbox, started: true })
    else if (outbox.started && !active) setOutbox(null)
  }, [outbox, built.userCount, active])

  // A message whose turn never visibly started (the agent failed at once) does not linger.
  useEffect(() => {
    if (!outbox || outbox.started) return undefined
    const timer = window.setTimeout(() => setOutbox((current) => (current && !current.started ? null : current)), 20_000)
    return () => window.clearTimeout(timer)
  }, [outbox])

  useEffect(() => {
    if (!active) setStopping(false)
  }, [active])

  const onSent = ({ text, attachments, baseline }) => {
    setOutbox({ text, attachments, baseline, started: false })
    transcriptRef.current?.scrollToBottom()
  }

  const onStop = async () => {
    setStopping(true)
    try {
      await api.interrupt(session.id)
    } catch (err) {
      setStopping(false)
      toast({ title: 'Could not stop the agent', description: err.message, tone: 'error' })
    }
  }

  // When the current turn started: seen live, else the last message (or the
  // agent process starting, for a resumed turn nobody typed).
  const turnStart = useMemo(() => {
    if (stream.activeSince) return stream.activeSince
    const lastUser = toDate(built.lastUserAt)?.getTime() || 0
    const started = toDate(session.runtime?.started_at)?.getTime() || 0
    return Math.max(lastUser, started) || null
  }, [stream.activeSince, built.lastUserAt, session.runtime?.started_at])

  const lastBlock = blocks[blocks.length - 1]
  // The live status carries the error text; the stored session has it after a reload.
  const failure = active
    ? ''
    : stream.agentError || (state === 'error' && stream.status.detail) || (session.status === 'error' && session.last_error) || ''
  const showFailure = Boolean(failure) && !(lastBlock?.type === 'notice' && lastBlock.text === failure)
  const stopped = !active && !failure && (stream.lastTurn?.outcome === 'interrupted' || (!stream.lastTurn && session.status === 'interrupted'))

  const empty = !blocks.length && !outbox && !active && !stream.pending.answer
  const tail = (
    <>
      {outbox ? (
        <div className={blocks.length ? 'mt-8' : ''}>
          <OutboxMessage text={outbox.text} attachments={outbox.attachments} />
        </div>
      ) : null}
      {stream.pending.answer ? (
        <div className="mt-4">
          <Prose block={{ text: stream.pending.answer }} streaming />
        </div>
      ) : null}
      {active ? (
        <div className={blocks.length || outbox ? 'mt-4' : ''}>
          <LiveLine state={state} since={turnStart} reasoning={stream.pending.reasoning} stopping={stopping} />
        </div>
      ) : null}
      {showFailure ? (
        <div className="mt-4">
          <Notice text={failure} />
        </div>
      ) : null}
      {resumable && !active ? (
        <ResumeLine sessionId={session.id} />
      ) : stopped && lastBlock?.type !== 'notice' ? (
        <p className="mt-3 text-sm text-faint">Stopped</p>
      ) : null}
      {empty && !resumable ? (
        kind === 'reconstruction' && !harnessId ? (
          <EmptyState
            icon={MessageSquare}
            title="This session did not start"
            className="min-h-60"
            action={
              <Button variant="secondary" onClick={() => useApp.getState().navigate({ name: 'new-reconstruction' })}>
                Start a new reconstruction
              </Button>
            }
          >
            Its agent never started, so there is nothing to resume.
          </EmptyState>
        ) : (
          <EmptyState icon={MessageSquare} title="No messages yet" className="min-h-60">
            Send a message to start the agent.
          </EmptyState>
        )
      ) : null}
    </>
  )

  let body
  if (stream.loading) {
    body = (
      <div className="min-h-0 flex-1 overflow-hidden">
        <TranscriptSkeleton />
      </div>
    )
  } else if (stream.error) {
    body = (
      <div className="min-h-0 flex-1">
        <EmptyState
          icon={SearchX}
          title="The transcript could not be loaded"
          action={
            <Button variant="secondary" onClick={stream.reload}>
              Try again
            </Button>
          }
        >
          {stream.error}
        </EmptyState>
      </div>
    )
  } else {
    body = <Transcript ref={transcriptRef} blocks={blocks} session={session} active={active} tail={tail} />
  }

  return (
    <ChatContext.Provider value={chat}>
      <CallsContext.Provider value={stream.recursiveCalls}>
        <div className="flex h-full flex-col">
          {body}
          <div className="shrink-0 pb-4 pt-1">
            <div className={COLUMN}>
              <ChatComposer
                session={session}
                active={active}
                alive={running}
                resumable={resumable && !active}
                stopping={stopping}
                userCount={built.userCount}
                onSent={onSent}
                onStop={onStop}
              />
            </div>
          </div>
        </div>
      </CallsContext.Provider>
    </ChatContext.Provider>
  )
}
