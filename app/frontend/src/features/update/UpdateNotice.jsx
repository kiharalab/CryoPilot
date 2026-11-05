// Tells people about a newer version of CryoPilot and installs it: a notice
// above the conversation, and the full story in Settings > About. The server
// does the work (app/server/updates.py); this polls it and shows where it is.
import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useApp } from '../../store/app'
import { Button, IconButton } from '../../components/ui'
import { SetupNotice } from '../new/SetupNotice'

const DISMISSED_KEY = 'cryopilot-update-dismissed'
const POLL_INSTALLING_MS = 1000
// The Restart button waits for running agents to finish.
const POLL_READY_MS = 10 * 1000
// The first check runs just after the server starts, so an early answer may not have it yet.
const POLL_MS = 10 * 60 * 1000

function dismissedVersion() {
  try {
    return localStorage.getItem(DISMISSED_KEY)
  } catch {
    return null
  }
}

function dismiss(version) {
  try {
    localStorage.setItem(DISMISSED_KEY, version)
  } catch {
    // The notice comes back on the next visit.
  }
}

function useUpdatePolling() {
  const refreshUpdate = useApp((state) => state.refreshUpdate)
  const installState = useApp((state) => state.update?.install?.state)
  const interval = installState === 'downloading' || installState === 'checking' ? POLL_INSTALLING_MS : installState === 'ready' ? POLL_READY_MS : POLL_MS
  useEffect(() => {
    const refresh = () => refreshUpdate().catch(() => {})
    refresh()
    const timer = setInterval(refresh, interval)
    return () => clearInterval(timer)
  }, [interval, refreshUpdate])
}

function progressText(update) {
  const { state, done, total, version } = update.install
  if (state === 'checking') return `Checking CryoPilot ${version}. This can take a minute.`
  const percent = total ? ` ${Math.min(100, Math.floor((done * 100) / total))}%` : ''
  return `Downloading CryoPilot ${version}.${percent}`
}

function restartBlockedText(running) {
  return `CryoPilot ${running === 1 ? 'has 1 agent' : `has ${running} agents`} running. Restart when ${running === 1 ? 'it is' : 'they are'} done, because a restart stops agents.`
}

/** The one thing to do next: update, follow the progress, or restart. */
function UpdateAction({ update }) {
  const installUpdate = useApp((state) => state.installUpdate)
  const restartForUpdate = useApp((state) => state.restartForUpdate)
  const restarting = useApp((state) => state.updateRestarting)
  const state = update.install.state
  if (restarting || state === 'ready') {
    return (
      <Button size="sm" variant="primary" loading={restarting} disabled={update.agents_running > 0} onClick={restartForUpdate}>
        Restart
      </Button>
    )
  }
  if (state === 'downloading' || state === 'checking') return null
  return (
    <Button size="sm" variant="primary" disabled={Boolean(update.blocker)} onClick={installUpdate}>
      {state === 'failed' ? 'Try again' : 'Update'}
    </Button>
  )
}

function statusText(update, restarting) {
  const { state, version, error } = update.install
  if (restarting) return `Restarting into CryoPilot ${version}.`
  if (state === 'downloading' || state === 'checking') return progressText(update)
  if (state === 'failed') return error
  if (state === 'ready') {
    return update.agents_running > 0
      ? `CryoPilot ${version} is installed. ${restartBlockedText(update.agents_running)}`
      : `CryoPilot ${version} is installed. Restart to use it.`
  }
  return `CryoPilot ${update.latest} is available. You have ${update.current}.`
}

/** Above the main column, until it is dismissed for this version. */
export function UpdateBanner() {
  useUpdatePolling()
  const update = useApp((state) => state.update)
  const restarting = useApp((state) => state.updateRestarting)
  const openSettings = useApp((state) => state.openSettings)
  const [dismissed, setDismissed] = useState(dismissedVersion)
  if (!update?.latest) return null
  const started = update.install.state !== 'idle'
  if (!started && dismissed === update.latest) return null
  return (
    <div className="shrink-0 px-3 pb-2">
      <SetupNotice
        tone={update.install.state === 'failed' ? 'danger' : 'neutral'}
        action={
          <div className="flex shrink-0 items-center gap-1.5">
            {started ? null : (
              <Button size="sm" variant="ghost" onClick={() => openSettings('about')}>
                What's new
              </Button>
            )}
            <UpdateAction update={update} />
            {started ? null : (
              <IconButton
                icon={X}
                label="Not now"
                size="sm"
                onClick={() => {
                  dismiss(update.latest)
                  setDismissed(update.latest)
                }}
              />
            )}
          </div>
        }
      >
        {statusText(update, restarting)}
      </SetupNotice>
    </div>
  )
}

function ReleaseNotes({ release }) {
  return (
    <div className="border-b border-line py-3.5 last:border-b-0">
      <p className="text-sm font-medium text-fg">
        {release.version} <span className="font-normal text-muted">{release.date}</span>
      </p>
      <ul className="mt-1.5 list-disc space-y-1 pl-4 text-sm text-muted">
        {(release.notes || []).map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </div>
  )
}

/** Settings > About: the state of the update, and what changed in each release. */
export function UpdateStatus() {
  const update = useApp((state) => state.update)
  const restarting = useApp((state) => state.updateRestarting)
  if (!update?.latest) return null
  return (
    <div className="mt-4 rounded-xl bg-subtle px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-[200px] flex-1 text-sm text-fg">
          {update.install.state === 'idle' && update.blocker ? update.blocker : statusText(update, restarting)}
        </p>
        <UpdateAction update={update} />
      </div>
      {update.new_releases.map((release) => (
        <ReleaseNotes key={release.version} release={release} />
      ))}
    </div>
  )
}

/** The releases up to the running version; UpdateStatus shows the newer ones. */
export function Changelog() {
  const update = useApp((state) => state.update)
  const newer = new Set((update?.new_releases || []).map((release) => release.version))
  return (update?.releases || [])
    .filter((release) => !newer.has(release.version))
    .map((release) => <ReleaseNotes key={release.version} release={release} />)
}
