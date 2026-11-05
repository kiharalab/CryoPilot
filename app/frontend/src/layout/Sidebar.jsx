// Left sidebar: project switcher, new session, the project's sessions, and
// settings with the connection status of each service.
import { useMemo, useState } from 'react'
import {
  Atom,
  ChevronsUpDown,
  Download,
  FolderPlus,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  Plus,
  Settings,
  Trash2,
  Waypoints,
} from 'lucide-react'
import { api } from '../lib/api'
import { cx } from '../lib/cx'
import { toDate } from '../lib/format'
import { modKeyLabel } from '../lib/platform'
import { useApp } from '../store/app'
import { ConfirmDialog, IconButton, Kbd, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, StatusDot, Tip } from '../components/ui'
import { CreateProjectDialog } from '../features/project/CreateProjectDialog'
import { ImportSessionDialog } from '../features/project/ImportSessionDialog'
import { SERVICE_NAMES, defaultModelServices } from '../features/setup/services'
import { Wordmark } from './Brand'

export const SESSION_ICONS = { reconstruction: Waypoints, modeling: Atom }

function sessionState(session) {
  if (session.runtime?.running) return session.runtime.state || 'running'
  return session.status
}

/** Each service the agents use, and whether CryoPilot can use it now. */
function serviceStatus(setup) {
  return [
    ...defaultModelServices(setup.defaults).map((service) => ({ label: SERVICE_NAMES[service], ok: Boolean(setup[service]?.configured) })),
    ...(setup.platform?.reconstruction_supported ? [{ label: 'CryoSPARC', ok: Boolean(setup.cryosparc?.password_set) }] : []),
    { label: 'CryoZeta', ok: Boolean(setup.cryozeta?.password_set) },
  ]
}

function groupByDate(sessions) {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const day = 86400000
  const groups = [
    { label: 'Today', from: startOfToday, items: [] },
    { label: 'Yesterday', from: startOfToday - day, items: [] },
    { label: 'Previous 7 days', from: startOfToday - 7 * day, items: [] },
    { label: 'Previous 30 days', from: startOfToday - 30 * day, items: [] },
    { label: 'Older', from: -Infinity, items: [] },
  ]
  for (const session of sessions) {
    const time = toDate(session.updated_at || session.created_at)?.getTime() ?? 0
    const group = groups.find((entry) => time >= entry.from)
    group.items.push(session)
  }
  return groups.filter((group) => group.items.length)
}

export function Sidebar() {
  const project = useApp((state) => state.project)
  const projects = useApp((state) => state.projects)
  const selectProject = useApp((state) => state.selectProject)
  const view = useApp((state) => state.view)
  const navigate = useApp((state) => state.navigate)
  const openSession = useApp((state) => state.openSession)
  const toggleSidebar = useApp((state) => state.toggleSidebar)
  const openSettings = useApp((state) => state.openSettings)
  const setup = useApp((state) => state.setup)
  const toast = useApp((state) => state.toast)
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)

  const sessions = useMemo(
    () =>
      [...(project?.sessions || [])].sort(
        (a, b) => (toDate(b.updated_at || b.created_at)?.getTime() ?? 0) - (toDate(a.updated_at || a.created_at)?.getTime() ?? 0),
      ),
    [project?.sessions],
  )
  const groups = useMemo(() => groupByDate(sessions), [sessions])
  const services = setup ? serviceStatus(setup) : []

  const switchProject = (projectId) => {
    selectProject(projectId).catch((err) => toast({ title: 'Could not open the project', description: err.message, tone: 'error' }))
  }

  return (
    <nav className="flex h-full min-w-0 flex-col" aria-label="Sessions">
      <div className="flex h-12 shrink-0 items-center justify-between px-3">
        <Wordmark className="pl-1 text-[15px]" />
        <IconButton icon={PanelLeftClose} label="Hide sidebar" size="sm" onClick={() => toggleSidebar(false)} />
      </div>

      <div className="px-2">
        <Menu>
          <MenuTrigger asChild>
            <button
              type="button"
              className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm hover:bg-hover data-[state=open]:bg-hover"
            >
              <span className="min-w-0 flex-1 truncate font-medium">{project?.title || 'No project'}</span>
              <ChevronsUpDown size={14} className="shrink-0 text-faint" />
            </button>
          </MenuTrigger>
          <MenuContent className="w-[248px]">
            {projects.length ? <MenuLabel>Projects</MenuLabel> : null}
            <div className="max-h-[320px] overflow-y-auto">
              {projects.map((entry) => (
                <MenuItem key={entry.id} onSelect={() => switchProject(entry.id)} checked={entry.id === project?.id}>
                  {entry.title}
                </MenuItem>
              ))}
            </div>
            {projects.length ? <MenuSeparator /> : null}
            <MenuItem icon={FolderPlus} onSelect={() => setCreateOpen(true)}>
              New project
            </MenuItem>
            <MenuItem icon={Download} disabled={!project} onSelect={() => setImportOpen(true)}>
              Import a terminal session
            </MenuItem>
          </MenuContent>
        </Menu>

        <button
          type="button"
          disabled={!project}
          onClick={() => navigate({ name: 'home' })}
          className={cx(
            'mt-1 flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors disabled:opacity-40',
            view.name === 'home' || view.name.startsWith('new-') ? 'bg-hover text-fg' : 'text-fg hover:bg-hover',
          )}
        >
          <Plus size={16} strokeWidth={1.75} className="text-muted" />
          <span className="flex-1 text-left">New session</span>
          <span className="hidden items-center gap-0.5 sm:flex">
            <Kbd>{modKeyLabel}</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {project && !sessions.length ? (
          <p className="px-2.5 py-2 text-sm text-faint">Sessions you start appear here.</p>
        ) : null}
        {groups.map((group) => (
          <div key={group.label} className="mb-3">
            <p className="px-2.5 pb-1 text-xs font-medium text-faint">{group.label}</p>
            {group.items.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                selected={view.name === 'session' && view.sessionId === session.id}
                onOpen={() => openSession(session.id)}
              />
            ))}
          </div>
        ))}
      </div>

      <div className="shrink-0 border-t border-line p-2">
        {/* Opens on Accounts while a service is not signed in, which is what the dots show. */}
        <button
          type="button"
          onClick={() => openSettings(services.some((service) => !service.ok) ? 'accounts' : 'general')}
          className="flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm text-fg hover:bg-hover"
        >
          <Settings size={16} strokeWidth={1.75} className="text-muted" />
          <span className="flex-1 text-left">Settings</span>
          <ConnectionDots services={services} />
        </button>
      </div>

      <CreateProjectDialog open={createOpen} onOpenChange={setCreateOpen} />
      <ImportSessionDialog open={importOpen} onOpenChange={setImportOpen} />
    </nav>
  )
}

function ConnectionDots({ services }) {
  if (!services.length) return null
  const summary = services.map((service) => `${service.label}: ${service.ok ? 'signed in' : 'not signed in'}`).join(', ')
  return (
    <Tip label={summary} side="top">
      <span className="flex items-center gap-1">
        {services.map((service) => (
          <span key={service.label} aria-hidden className={cx('h-1.5 w-1.5 rounded-full', service.ok ? 'bg-success' : 'bg-line-strong')} />
        ))}
        <span className="sr-only">{summary}</span>
      </span>
    </Tip>
  )
}

function SessionRow({ session, selected, onOpen }) {
  const Icon = SESSION_ICONS[session.kind] || Waypoints
  const state = sessionState(session)
  const working = session.runtime?.running && ['running', 'starting', 'connecting', 'initializing'].includes(state)
  const patchSession = useApp((s) => s.patchSession)
  const removeSession = useApp((s) => s.removeSession)
  const toast = useApp((s) => s.toast)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(session.title)
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)

  const rename = async () => {
    setRenaming(false)
    const next = title.trim()
    if (!next || next === session.title) return setTitle(session.title)
    try {
      patchSession(await api.renameSession(session.id, next))
    } catch (err) {
      setTitle(session.title)
      toast({ title: 'Could not rename the session', description: err.message, tone: 'error' })
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      await api.deleteSession(session.id)
      removeSession(session.id)
      setConfirm(false)
    } catch (err) {
      toast({ title: 'Could not delete the session', description: err.message, tone: 'error' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={cx(
        'group relative flex h-8 items-center rounded-lg pr-1 text-sm transition-colors',
        selected ? 'bg-hover text-fg' : 'text-muted hover:bg-hover hover:text-fg',
      )}
    >
      {renaming ? (
        <input
          autoFocus
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={rename}
          onKeyDown={(event) => {
            if (event.key === 'Enter') rename()
            if (event.key === 'Escape') {
              setTitle(session.title)
              setRenaming(false)
            }
          }}
          className="mx-1 h-7 min-w-0 flex-1 rounded-md border border-accent bg-canvas px-2 text-sm text-fg outline-none"
        />
      ) : (
        <button type="button" onClick={onOpen} className="flex h-full min-w-0 flex-1 items-center gap-2.5 pl-2.5 text-left">
          <Icon size={15} strokeWidth={1.75} className="shrink-0 opacity-70" />
          <span className="min-w-0 flex-1 truncate">{session.title}</span>
          {working ? <StatusDot state="running" className="mr-1" /> : state === 'error' ? <StatusDot state="error" className="mr-1" /> : null}
        </button>
      )}
      {!renaming ? (
        <Menu>
          <MenuTrigger asChild>
            <button
              type="button"
              aria-label="Session options"
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-faint opacity-0 hover:bg-line hover:text-fg group-hover:opacity-100 data-[state=open]:opacity-100 focus-visible:opacity-100"
            >
              <MoreHorizontal size={15} />
            </button>
          </MenuTrigger>
          <MenuContent align="end" className="min-w-[160px]">
            <MenuItem
              icon={Pencil}
              onSelect={() => {
                setTitle(session.title)
                setRenaming(true)
              }}
            >
              Rename
            </MenuItem>
            <MenuItem icon={Trash2} danger onSelect={() => setConfirm(true)}>
              Delete
            </MenuItem>
          </MenuContent>
        </Menu>
      ) : null}
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Delete this session?"
        description={
          session.kind === 'reconstruction'
            ? 'The conversation is removed from CryoPilot. The CryoSPARC project and its jobs stay as they are.'
            : 'The conversation is removed from CryoPilot. Files in the modeling workspace stay as they are.'
        }
        busy={busy}
        onConfirm={remove}
      />
    </div>
  )
}

