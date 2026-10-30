// A one-line notice above a new-session composer when something the agent
// needs is missing, with the button that fixes it.
import { CircleAlert, Info } from 'lucide-react'
import { cx } from '../../lib/cx'
import { useApp } from '../../store/app'
import { Button } from '../../components/ui'

const SETUP_MESSAGES = {
  openai: 'Add your OpenAI API key in Settings to start an agent.',
  anthropic: 'Add your Anthropic API key in Settings to run a Claude model.',
  cryosparc: 'Sign in to CryoSPARC in Settings to start a reconstruction.',
  cryozeta: 'Sign in to CryoZeta in Settings to build models from a sequence.',
}

/** Message for a missing credential named by ApiError.setup. */
export function setupMessage(setup, fallback) {
  return SETUP_MESSAGES[setup] || fallback || 'Finish setting up CryoPilot in Settings.'
}

/**
 * Props: tone 'warning' | 'neutral' | 'danger', children, and either
 * settingsSection (shows an "Open settings" button) or action (any node).
 */
export function SetupNotice({ tone = 'warning', children, settingsSection, action, className }) {
  const openSettings = useApp((state) => state.openSettings)
  const Icon = tone === 'neutral' ? Info : CircleAlert
  return (
    <div
      role={tone === 'danger' ? 'alert' : 'status'}
      className={cx(
        'flex items-center gap-3 rounded-xl px-3.5 py-2.5 text-sm',
        tone === 'danger' ? 'bg-danger-soft text-danger' : tone === 'neutral' ? 'bg-subtle text-muted' : 'bg-warning-soft text-fg',
        className,
      )}
    >
      <Icon size={15} strokeWidth={1.75} className={cx('shrink-0', tone === 'warning' ? 'text-warning' : '')} />
      <p className="min-w-0 flex-1">{children}</p>
      {action ||
        (settingsSection ? (
          <Button size="sm" variant="secondary" onClick={() => openSettings(settingsSection)}>
            <span className="hidden sm:inline">Open settings</span>
            <span className="sm:hidden">Settings</span>
          </Button>
        ) : null)}
    </div>
  )
}
