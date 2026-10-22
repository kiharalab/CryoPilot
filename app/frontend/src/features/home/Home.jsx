// The new-session screen: start a reconstruction or a modeling session in
// the current project, or pick up one of its recent sessions.
import { useEffect, useMemo, useState } from 'react'
import { Atom, Download, Waypoints } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative, isActiveState, pluralize, stateLabel, toDate } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, StatusDot } from '../../components/ui'
import { ImportSessionDialog } from '../project/ImportSessionDialog'

const KIND_ICONS = { reconstruction: Waypoints, modeling: Atom }
const RECENT_COUNT = 6

function sessionState(session) {
  if (session.runtime?.running) return session.runtime.state || 'running'
  return session.status
}

export function Home() {
  const project = useApp((state) => state.project)
  const setup = useApp((state) => state.setup)
  const navigate = useApp((state) => state.navigate)
  const openSession = useApp((state) => state.openSession)
  const [importOpen, setImportOpen] = useState(false)
  const [importable, setImportable] = useState(0)

  const reconstructionSupported = Boolean(setup?.platform?.reconstruction_supported)

  const recent = useMemo(
    () =>
      [...(project?.sessions || [])]
        .sort(
          (a, b) =>
            (toDate(b.updated_at || b.created_at)?.getTime() ?? 0) - (toDate(a.updated_at || a.created_at)?.getTime() ?? 0),
        )
        .slice(0, RECENT_COUNT),
    [project?.sessions],
  )

  // Offer the import only when the terminal has run sessions that are not in any project yet.
  useEffect(() => {
    let cancelled = false
    api
      .harnessSessions()
      .then((data) => !cancelled && setImportable((data.sessions || []).filter((session) => !session.attached_session_id).length))
      .catch(() => !cancelled && setImportable(0))
    return () => {
      cancelled = true
    }
  }, [importOpen])

  return (
    <div className="h-full overflow-y-auto">
      <div className="@container mx-auto flex min-h-full w-full max-w-[720px] flex-col justify-center px-6 pb-[10vh] pt-10">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] text-fg">{project.title}</h1>
        {project.description ? <p className="mt-1.5 line-clamp-3 text-base text-muted">{project.description}</p> : null}

        <div className="mt-7 grid grid-cols-1 gap-3 @lg:grid-cols-2">
          <Choice
            icon={Waypoints}
            title="Reconstruction"
            description="Process movies, micrographs, or particles in CryoSPARC, from import to a refined map."
            disabled={!reconstructionSupported}
            note={reconstructionSupported ? null : 'Runs on the Linux computer where CryoSPARC is installed. Start CryoPilot there to process data.'}
            onClick={() => navigate({ name: 'new-reconstruction' })}
          />
          <Choice
            icon={Atom}
            title="Modeling"
            description="Build, fit, validate, or refine an atomic model against a map."
            onClick={() => navigate({ name: 'new-modeling' })}
          />
        </div>

        {importable ? (
          <div className="mt-3">
            <Button size="sm" variant="ghost" icon={Download} onClick={() => setImportOpen(true)} className="-ml-2.5">
              Import {pluralize(importable, 'reconstruction')} started in the terminal
            </Button>
          </div>
        ) : null}

        {recent.length ? (
          <section className="mt-9" aria-labelledby="recent-sessions">
            <h2 id="recent-sessions" className="mb-1.5 px-2.5 text-sm font-medium text-muted">
              Recent
            </h2>
            <ul className="flex flex-col">
              {recent.map((session) => (
                <RecentRow key={session.id} session={session} onOpen={() => openSession(session.id)} />
              ))}
            </ul>
          </section>
        ) : null}
      </div>
      <ImportSessionDialog open={importOpen} onOpenChange={setImportOpen} />
    </div>
  )
}

function Choice({ icon: Icon, title, description, note, disabled = false, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cx(
        'flex min-h-[132px] flex-col items-start gap-3 rounded-xl border border-line bg-elevated p-4 text-left transition-colors',
        disabled ? 'cursor-not-allowed' : 'hover:border-line-strong hover:bg-hover',
      )}
    >
      <span className={cx('flex h-8 w-8 items-center justify-center rounded-lg bg-subtle', disabled ? 'text-faint' : 'text-accent-text')}>
        <Icon size={17} strokeWidth={1.75} />
      </span>
      <span className="flex flex-col gap-1">
        <span className={cx('text-base font-medium', disabled ? 'text-muted' : 'text-fg')}>{title}</span>
        <span className={cx('text-sm', disabled ? 'text-faint' : 'text-muted')}>{description}</span>
        {note ? <span className="mt-1 text-xs text-muted">{note}</span> : null}
      </span>
    </button>
  )
}

// States worth a word in the list; anything else (stopped, done) needs none.
const STATE_TONES = { idle: 'text-muted', error: 'text-danger', interrupted: 'text-warning' }

function RecentRow({ session, onOpen }) {
  const Icon = KIND_ICONS[session.kind] || Waypoints
  const state = sessionState(session)
  const working = Boolean(session.runtime?.running) && isActiveState(state)
  const tone = working ? 'text-accent-text' : STATE_TONES[state]
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="flex h-9 w-full items-center gap-3 rounded-lg px-2.5 text-left text-sm transition-colors hover:bg-hover"
      >
        <Icon size={15} strokeWidth={1.75} className="shrink-0 text-muted" />
        <span className="min-w-0 flex-1 truncate text-fg">{session.title}</span>
        {tone ? (
          <span className={cx('flex shrink-0 items-center gap-1.5 text-xs', tone)}>
            <StatusDot state={state} />
            {stateLabel(state)}
          </span>
        ) : null}
        <span className="w-16 shrink-0 text-right text-xs tabular-nums text-faint">{formatRelative(session.updated_at || session.created_at)}</span>
      </button>
    </li>
  )
}
