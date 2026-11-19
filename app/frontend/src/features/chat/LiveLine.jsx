// The line under the transcript while an agent works. It says what the agent is
// actually waiting for right now (the model, a tool, a CryoSPARC job, the
// server), and the times beside each thing belong to that thing, so "Waiting
// for the model" never shows how long the whole turn has run.
import { useEffect, useState } from 'react'
import { api } from '../../lib/api'
import { formatDuration, stateLabel, toDate } from '../../lib/format'
import { useNow } from '../../hooks/useNow'
import { Button, StatusDot } from '../../components/ui'

const JOB_POLL_MS = 15000
const ACTIVE_JOB_STATES = new Set(['queued', 'launched', 'started', 'running', 'waiting'])
/** A job queued this long deserves an explanation. */
const LONG_QUEUE_S = 10 * 60
/** A model request this long deserves an explanation. */
const LONG_MODEL_WAIT_S = 10 * 60
/** A stop request this long unanswered: offer to end the process instead. */
const FORCE_STOP_AFTER_S = 20
const JOB_DOTS = { started: 'running' }
const JOB_VERBS = { queued: 'queued', launched: 'launching', started: 'running', running: 'running', waiting: 'waiting for input' }

/** The end of a streaming text, starting at a word, so the line shows what is being written now. */
function tail(text, limit = 360) {
  const value = String(text || '').trim()
  if (value.length <= limit) return value
  const cut = value.slice(value.length - limit)
  const space = cut.indexOf(' ')
  return `…${space > 0 && space < 40 ? cut.slice(space + 1) : cut}`
}

function secondsSince(value, now) {
  const at = toDate(value)?.getTime()
  return at ? Math.max(0, (now - at) / 1000) : null
}

/** CryoSPARC jobs of this session's workspace that are queued or running; polled while the agent works. */
function useActiveJobs(sessionId, enabled) {
  const [jobs, setJobs] = useState([])
  useEffect(() => {
    if (!enabled) {
      setJobs([])
      return undefined
    }
    let cancelled = false
    const load = () =>
      api
        .cryosparcJobs(sessionId)
        .then((data) => {
          if (!cancelled) setJobs((data.jobs || []).filter((job) => ACTIVE_JOB_STATES.has(job.status)))
        })
        .catch(() => {
          // CryoSPARC not answering is the Maps tab's to report; this line just shows no jobs.
          if (!cancelled) setJobs([])
        })
    load()
    const timer = window.setInterval(load, JOB_POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [sessionId, enabled])
  return jobs
}

function jobSince(job) {
  if (job.status === 'queued' || job.status === 'launched') return job.queued_at
  if (job.status === 'waiting') return job.waiting_at || job.running_at
  return job.running_at || job.started_at || job.queued_at
}

function JobLine({ job, now }) {
  const seconds = secondsSince(jobSince(job), now)
  const queued = job.status === 'queued'
  return (
    <div className="flex min-w-0 flex-col">
      <div className="flex min-w-0 items-center gap-2 text-sm text-faint">
        <StatusDot state={JOB_DOTS[job.status] || job.status} />
        <span className="min-w-0 truncate">
          {job.uid} {job.type_title}
        </span>
        <span className="shrink-0 tabular-nums">
          {JOB_VERBS[job.status] || job.status}
          {seconds !== null ? ` ${formatDuration(seconds)}` : ''}
        </span>
      </div>
      {queued && job.queue_message ? <p className="ml-3.5 text-xs text-faint">{job.queue_message}</p> : null}
      {queued && !job.queue_message && seconds !== null && seconds >= LONG_QUEUE_S ? (
        <p className="ml-3.5 text-xs text-warning">
          CryoSPARC has not started this job yet, probably because its resources are busy. It shows 0 GPU, CPU, and RAM for a job until it starts it.
        </p>
      ) : null}
    </div>
  )
}

/**
 * sessionId, kind, state, detail, since (turn start, ms), stepPending, modelSince (when the
 * last thing in the transcript happened), reasoning and answer (streaming text), stopping,
 * connected, limitReached, onForceStop.
 */
export function LiveLine({
  sessionId,
  kind,
  state,
  detail,
  since,
  stepPending = false,
  modelSince = null,
  reasoning = '',
  answer = '',
  stopping = false,
  connected = true,
  limitReached = false,
  onForceStop,
}) {
  const now = useNow(1000)
  const jobs = useActiveJobs(sessionId, kind === 'reconstruction' && state === 'running' && connected && !stopping)
  const [stoppingSince, setStoppingSince] = useState(null)
  const [ending, setEnding] = useState(false)
  useEffect(() => setStoppingSince(stopping ? Date.now() : null), [stopping])

  // Ending the process waits for it to exit, which can take the server 20 seconds.
  const endProcess = async () => {
    setEnding(true)
    try {
      await onForceStop()
    } finally {
      setEnding(false)
    }
  }

  const turn = since ? Math.max(0, (now - since) / 1000) : null
  const waitingForModel = state === 'running' && !stepPending && !reasoning && !answer && !stopping && connected
  const modelWait = waitingForModel ? secondsSince(modelSince, now) : null

  let label
  let dot = state || 'running'
  if (!connected) {
    label = 'Lost the connection to the server. Reconnecting'
    dot = 'interrupted'
  } else if (stopping) {
    label = 'Stopping'
    dot = 'interrupted'
  } else if (state !== 'running') {
    // Starting up: the agent says exactly what it is doing.
    label = detail || stateLabel(state)
  } else if (reasoning) {
    label = 'Thinking'
  } else if (answer) {
    label = 'Writing'
  } else if (stepPending) {
    label = jobs.length ? 'Waiting for CryoSPARC' : 'Running this step'
  } else {
    label = 'Waiting for the model'
  }

  const stuckStopping = stoppingSince && (now - stoppingSince) / 1000 >= FORCE_STOP_AFTER_S

  return (
    <div className="flex flex-col gap-1">
      <div className="flex min-h-6 flex-wrap items-center gap-x-2 text-sm text-muted">
        <StatusDot state={dot} className="ml-[3px]" />
        <span role="status">{label}</span>
        {modelWait !== null && modelWait >= 5 ? <span className="tabular-nums text-faint">{formatDuration(modelWait)}</span> : null}
        {turn !== null && connected ? <span className="tabular-nums text-faint">· turn {formatDuration(turn)}</span> : null}
        {stuckStopping && onForceStop ? (
          <Button size="xs" variant="secondary" loading={ending} onClick={endProcess} className="ml-1">
            End the agent process
          </Button>
        ) : null}
      </div>
      {modelWait !== null && modelWait >= LONG_MODEL_WAIT_S ? (
        <p className="ml-[15px] text-xs text-faint">Models can take many minutes at high reasoning effort, and retry by themselves when the provider is busy.</p>
      ) : null}
      {limitReached && connected && !stopping ? (
        <p className="ml-[15px] text-xs text-warning">This session has reached its spending limit. It pauses before its next model request.</p>
      ) : null}
      {!connected ? <p className="ml-[15px] text-xs text-faint">The agent runs on the server, so it keeps working while this page is disconnected.</p> : null}
      {jobs.length ? (
        <div className="ml-[3px] flex flex-col gap-0.5">
          {jobs.slice(0, 4).map((job) => (
            <JobLine key={job.uid} job={job} now={now} />
          ))}
        </div>
      ) : null}
      {reasoning ? <p className="ml-[15px] whitespace-pre-wrap break-words text-sm leading-relaxed text-faint">{tail(reasoning)}</p> : null}
    </div>
  )
}
