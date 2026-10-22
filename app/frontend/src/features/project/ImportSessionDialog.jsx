// Attach a reconstruction started from the terminal (python harness.py) to
// the current project, so its transcript, maps, and jobs show up here.
import { useEffect, useMemo, useState } from 'react'
import { Search, Terminal } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, Dialog, DialogContent, EmptyState, InlineError, Skeleton, Spinner, StatusDot } from '../../components/ui'

const PAGE = 50
const SETTLED_STATES = new Set(['completed', 'error', 'interrupted'])
const STATUS_NAMES = {
  collecting: 'Not started',
  initializing: 'Setting up',
  running: 'Last seen running',
  interrupted: 'Stopped',
  completed: 'Completed',
  error: 'Ended with an error',
}

export function ImportSessionDialog({ open, onOpenChange }) {
  const project = useApp((state) => state.project)
  const projects = useApp((state) => state.projects)
  const refreshProject = useApp((state) => state.refreshProject)
  const openSession = useApp((state) => state.openSession)
  const [sessions, setSessions] = useState(null)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [importing, setImporting] = useState(null)

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setSessions(null)
    setError('')
    setQuery('')
    setLimit(PAGE)
    setImporting(null)
    api
      .harnessSessions()
      .then((data) => !cancelled && setSessions(data.sessions || []))
      .catch((err) => {
        if (cancelled) return
        setSessions([])
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  const projectTitles = useMemo(() => Object.fromEntries(projects.map((entry) => [entry.id, entry.title])), [projects])

  const filtered = useMemo(() => {
    if (!sessions) return []
    const words = query.toLowerCase().split(/\s+/).filter(Boolean)
    if (!words.length) return sessions
    return sessions.filter((session) => {
      const haystack = [session.id, session.title, session.user_goal, session.project_uid, session.model].filter(Boolean).join(' ').toLowerCase()
      return words.every((word) => haystack.includes(word))
    })
  }, [sessions, query])

  const finish = async (sessionId) => {
    await refreshProject()
    openSession(sessionId)
    onOpenChange(false)
  }

  const choose = async (session) => {
    if (!project || importing) return
    setImporting(session.id)
    setError('')
    try {
      if (session.attached_session_id && session.attached_project_id === project.id) {
        await finish(session.attached_session_id)
        return
      }
      const created = await api.importReconstruction(project.id, { harness_session_id: session.id })
      await finish(created.id)
    } catch (err) {
      if (err.status === 409 && err.data?.session_id) {
        // Attached in the meantime; open it if it belongs here.
        const existing = err.data.session_id
        const refreshed = await refreshProject().catch(() => null)
        if (refreshed?.sessions?.some((entry) => entry.id === existing)) {
          openSession(existing)
          onOpenChange(false)
          return
        }
      }
      setError(err.message)
    } finally {
      setImporting(null)
    }
  }

  const unattached = sessions ? sessions.filter((session) => !session.attached_session_id).length : 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Import a terminal session"
        description={
          project ? `Reconstructions started with python harness.py on this computer. Importing one adds it to ${project.title}.` : undefined
        }
        width="max-w-2xl"
        className="h-[min(640px,84vh)]"
      >
        <div className="flex min-h-0 flex-1 flex-col pt-3">
          {sessions && sessions.length > 8 ? (
            <div className="mx-5 mb-2 flex h-9 shrink-0 items-center gap-2 rounded-lg border border-line bg-canvas px-3 focus-within:border-accent">
              <Search size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
              <input
                autoFocus
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value)
                  setLimit(PAGE)
                }}
                placeholder="Search by goal, title, or project"
                aria-label="Search terminal sessions"
                className="h-full min-w-0 flex-1 bg-transparent text-sm text-fg placeholder:text-faint focus:outline-none"
              />
              <span className="shrink-0 text-xs text-faint">{unattached} not imported</span>
            </div>
          ) : null}
          {error ? <InlineError className="mx-5 mb-2">{error}</InlineError> : null}
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
            {!sessions ? (
              <div className="flex flex-col gap-3 px-2 py-2">
                {[0, 1, 2, 3].map((row) => (
                  <div key={row} className="flex flex-col gap-1.5">
                    <Skeleton className="h-4 w-2/5" />
                    <Skeleton className="h-3.5 w-4/5" />
                  </div>
                ))}
              </div>
            ) : !sessions.length ? (
              <EmptyState icon={Terminal} title="No terminal sessions on this computer">
                Reconstructions you start with <code className="font-mono text-[12.5px]">python harness.py</code> appear here, and you can follow them in
                CryoPilot after importing.
              </EmptyState>
            ) : !filtered.length ? (
              <p className="px-2 py-6 text-center text-sm text-muted">No sessions match.</p>
            ) : (
              <ul className="flex flex-col">
                {filtered.slice(0, limit).map((session) => (
                  <SessionRow
                    key={session.id}
                    session={session}
                    projectId={project?.id}
                    projectTitles={projectTitles}
                    busy={importing === session.id}
                    disabled={Boolean(importing)}
                    onChoose={() => choose(session)}
                  />
                ))}
                {filtered.length > limit ? (
                  <li className="flex justify-center pt-2">
                    <Button size="sm" variant="ghost" onClick={() => setLimit((current) => current + PAGE)}>
                      Show {Math.min(PAGE, filtered.length - limit)} more
                    </Button>
                  </li>
                ) : null}
              </ul>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function SessionRow({ session, projectId, projectTitles, busy, disabled, onChoose }) {
  const here = session.attached_session_id && session.attached_project_id === projectId
  const elsewhere = session.attached_session_id && !here
  const title = session.title || session.user_goal || session.id
  const goal = session.title && session.user_goal ? session.user_goal : ''
  // The harness database may still say "running" for a process that ended, so
  // only settled states get a colour here; nothing pulses.
  const state = SETTLED_STATES.has(session.status) ? session.status : undefined

  let trailing
  if (busy) trailing = <Spinner size={14} />
  else if (here) trailing = <span className="text-xs font-medium text-accent-text">Open</span>
  else if (elsewhere) trailing = <span className="text-xs text-faint">In {projectTitles[session.attached_project_id] || 'another project'}</span>
  else trailing = <span className="text-xs font-medium text-muted opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100">Import</span>

  return (
    <li>
      <button
        type="button"
        disabled={elsewhere || disabled}
        onClick={onChoose}
        title={elsewhere ? 'Already attached to another project' : undefined}
        className={cx(
          'group flex w-full items-start gap-3 rounded-lg px-2.5 py-2 text-left transition-colors',
          elsewhere ? 'cursor-not-allowed opacity-60' : 'hover:bg-hover',
        )}
      >
        <span className="mt-[7px] flex" title={STATUS_NAMES[session.status] || session.status}>
          <StatusDot state={state} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="min-w-0 truncate text-sm font-medium text-fg">{title}</span>
            {session.project_uid ? <span className="shrink-0 font-mono text-[11.5px] text-faint">{session.project_uid}</span> : null}
          </span>
          {goal ? <span className="mt-0.5 line-clamp-2 block text-xs leading-relaxed text-muted">{goal}</span> : null}
        </span>
        <span className="flex shrink-0 flex-col items-end gap-1 pt-0.5">
          <span className="text-xs tabular-nums text-faint">{formatRelative(session.updated_at || session.created_at)}</span>
          {trailing}
        </span>
      </button>
    </li>
  )
}
