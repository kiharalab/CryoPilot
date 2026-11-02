// The settings dialog: a section list on the left and the section on the
// right. Every control saves as soon as it changes. Open it with
// openSettings(section) from anywhere; ⌘, opens General.
import { Atom, ExternalLink, Info, KeyRound, Monitor, Moon, SlidersHorizontal, Sun, Waypoints } from 'lucide-react'
import { cx } from '../../lib/cx'
import { modKeyLabel } from '../../lib/platform'
import { useApp } from '../../store/app'
import { Dialog, DialogContent, Kbd, Segmented } from '../../components/ui'
import { BrandMark } from '../../layout/Brand'
import { systemName } from '../setup/services'
import { AccountsSection } from './AccountsSection'
import { ModelingSection, ReconstructionSection } from './AgentSections'
import { SettingsGroup, SettingsRow } from './controls'

const SECTIONS = [
  { id: 'general', label: 'General', icon: SlidersHorizontal, component: GeneralSection },
  { id: 'accounts', label: 'Accounts', icon: KeyRound, component: AccountsSection },
  { id: 'reconstruction', label: 'Reconstruction', icon: Waypoints, component: ReconstructionSection },
  { id: 'modeling', label: 'Modeling', icon: Atom, component: ModelingSection },
  { id: 'about', label: 'About', icon: Info, component: AboutSection },
]

export function SettingsDialog() {
  const open = useApp((state) => state.settingsOpen)
  const sectionId = useApp((state) => state.settingsSection)
  const closeSettings = useApp((state) => state.closeSettings)
  const setSettingsSection = useApp((state) => state.setSettingsSection)
  const hasSetup = useApp((state) => Boolean(state.setup))

  const section = SECTIONS.find((entry) => entry.id === sectionId) || SECTIONS[0]
  const Section = section.component

  return (
    <Dialog open={open} onOpenChange={(next) => !next && closeSettings()}>
      <DialogContent title="Settings" width="max-w-[880px]" className="h-[min(640px,calc(100vh-32px))]">
        <div className="mt-3 flex min-h-0 flex-1 flex-col border-t border-line sm:flex-row">
          <nav
            aria-label="Settings sections"
            className="flex shrink-0 gap-0.5 overflow-x-auto border-b border-line p-2 [scrollbar-width:none] sm:w-[200px] sm:flex-col sm:overflow-visible sm:border-b-0 sm:border-r"
          >
            {SECTIONS.map((entry) => {
              const Icon = entry.icon
              const selected = entry.id === section.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  aria-current={selected ? 'page' : undefined}
                  onClick={() => setSettingsSection(entry.id)}
                  className={cx(
                    'flex h-8 shrink-0 items-center gap-2.5 rounded-lg px-2.5 text-left text-sm transition-colors',
                    selected ? 'bg-hover font-medium text-fg' : 'text-muted hover:bg-hover hover:text-fg',
                  )}
                >
                  <Icon size={15} strokeWidth={1.75} className={selected ? 'text-fg' : 'text-muted'} />
                  {entry.label}
                </button>
              )
            })}
          </nav>
          <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-[620px] px-6 pb-8 pt-5">
              <h2 className="mb-5 text-base font-semibold text-fg">{section.label}</h2>
              {hasSetup ? <Section key={section.id} /> : null}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------------- general
const THEME_OPTIONS = [
  { value: 'system', label: 'System', icon: Monitor },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
]

const SHORTCUTS = [
  { label: 'New session', keys: [modKeyLabel, 'K'] },
  { label: 'Show or hide the sidebar', keys: [modKeyLabel, 'B'] },
  { label: 'Show or hide the terminal', keys: [modKeyLabel, 'J'] },
  { label: 'Show or hide the workbench', keys: [modKeyLabel, '\\'] },
  { label: 'Open settings', keys: [modKeyLabel, ','] },
  { label: 'Send a message', keys: ['Enter'] },
  { label: 'New line in a message', keys: ['Shift', 'Enter'] },
]

function GeneralSection() {
  const theme = useApp((state) => state.theme)
  const setTheme = useApp((state) => state.setTheme)
  return (
    <>
      <SettingsGroup>
        <SettingsRow title="Theme" description="System follows the appearance of your computer.">
          <Segmented value={theme} onChange={setTheme} options={THEME_OPTIONS} size="sm" ariaLabel="Theme" />
        </SettingsRow>
      </SettingsGroup>
      <SettingsGroup title="Keyboard shortcuts">
        <dl className="overflow-hidden rounded-xl border border-line">
          {SHORTCUTS.map((shortcut, index) => (
            <div key={shortcut.label} className={cx('flex h-10 items-center justify-between gap-4 px-3.5', index > 0 && 'border-t border-line')}>
              <dt className="text-sm text-fg">{shortcut.label}</dt>
              <dd className="flex items-center gap-1">
                {shortcut.keys.map((key) => (
                  <Kbd key={key}>{key}</Kbd>
                ))}
              </dd>
            </div>
          ))}
        </dl>
      </SettingsGroup>
    </>
  )
}

// ------------------------------------------------------------------- about
const VERSION = '0.3.2'

function ExternalAnchor({ href, children }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-sm text-accent-text hover:underline">
      {children}
      <ExternalLink size={12} strokeWidth={1.75} />
    </a>
  )
}

function AboutSection() {
  const system = useApp((state) => state.setup?.platform?.system)
  return (
    <>
      <div className="flex items-center gap-3.5">
        <BrandMark size={40} />
        <div>
          <p className="text-lg font-semibold tracking-[-0.01em] text-fg">CryoPilot</p>
          <p className="text-sm text-muted">Version {VERSION}</p>
        </div>
      </div>
      <p className="mt-4 text-sm text-muted">
        CryoPilot takes a cryo-EM dataset to a validated atomic model. The reconstruction agent processes the data in CryoSPARC, and the modeling agent
        builds, fits, refines, and validates models in the maps. Developed by the Kihara Lab at Purdue University.
      </p>
      <SettingsGroup className="mt-6">
        <SettingsRow title="Paper">
          <span className="text-sm text-muted">In submission</span>
        </SettingsRow>
        <SettingsRow title="License">
          <span className="text-sm text-muted">PolyForm Noncommercial 1.0.0</span>
        </SettingsRow>
        <SettingsRow title="Website">
          <ExternalAnchor href="https://cryopilot.vercel.app">cryopilot.vercel.app</ExternalAnchor>
        </SettingsRow>
        <SettingsRow title="Source code">
          <ExternalAnchor href="https://github.com/kiharalab/CryoPilot">github.com/kiharalab/CryoPilot</ExternalAnchor>
        </SettingsRow>
        {system ? (
          <SettingsRow title="Server">
            <span className="text-sm text-muted">{systemName(system)}</span>
          </SettingsRow>
        ) : null}
      </SettingsGroup>
    </>
  )
}
