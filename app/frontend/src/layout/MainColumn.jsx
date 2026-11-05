// Centre column: a slim header (title, state, panel toggles) over whatever
// the current view is: the new-session screens or a conversation.
import { useState } from 'react'
import { ExternalLink, FolderPlus, MoreHorizontal, PanelLeftOpen, PanelRight, Pencil, RotateCcw, SquareTerminal, Trash2 } from 'lucide-react'
import { api } from '../lib/api'
import { cx } from '../lib/cx'
import { stateLabel } from '../lib/format'
import { useApp, useCurrentSession } from '../store/app'
import { Button, ConfirmDialog, EmptyState, IconButton, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, StatusDot } from '../components/ui'
import { Home } from '../features/home/Home'
import { NewReconstruction } from '../features/new/NewReconstruction'
import { NewModeling } from '../features/new/NewModeling'
import { SessionView } from '../features/chat/SessionView'
import { CreateProjectDialog } from '../features/project/CreateProjectDialog'
import { UpdateBanner } from '../features/update/UpdateNotice'
import { SESSION_ICONS } from './Sidebar'
import { BrandMark } from './Brand'

const VIEW_TITLES = {
  home: 'New session',
  'new-reconstruction': 'New reconstruction',
  'new-modeling': 'New modeling session',
}

export function MainColumn() {
  const view = useApp((state) => state.view)
  const project = useApp((state) => state.project)
  const projects = useApp((state) => state.projects)
  const projectLoading = useApp((state) => state.projectLoading)

  let body
  if (!project) {
    body = projectLoading ? null : <NoProject hasProjects={projects.length > 0} />
  } else if (view.name === 'session') {
    body = <SessionView key={view.sessionId} sessionId={view.sessionId} />
  } else if (view.name === 'new-reconstruction') {
    body = <NewReconstruction key={`recon-${project.id}`} />
  } else if (view.name === 'new-modeling') {
    body = <NewModeling key={`model-${project.id}`} initialAttachments={view.attachments} />
  } else {
    body = <Home key={`home-${project.id}`} />
  }

  return (
    <main className="flex h-full min-w-0 flex-col bg-canvas">
      <MainHeader />
      <UpdateBanner />
      <div className="relative min-h-0 flex-1">{body}</div>
    </main>
  )
}

function MainHeader() {
  const view = useApp((state) => state.view)
  const sidebarOpen = useApp((state) => state.sidebarOpen)
  const toggleSidebar = useApp((state) => state.toggleSidebar)
  const workbenchOpen = useApp((state) => state.workbenchOpen)
  const toggleWorkbench = useApp((state) => state.toggleWorkbench)
  const bottomOpen = useApp((state) => state.bottomOpen)
  const toggleBottom = useApp((state) => state.toggleBottom)
  const session = useCurrentSession()

  return (
    <header className="flex h-12 shrink-0 items-center gap-1 px-3">
      {!sidebarOpen ? <IconButton icon={PanelLeftOpen} label="Show sidebar" onClick={() => toggleSidebar(true)} /> : null}
      <div className="flex min-w-0 flex-1 items-center gap-2 px-1">
        {view.name === 'session' && session ? (
          <SessionTitle key={session.id} session={session} />
        ) : (
          <span className="truncate text-sm font-medium text-muted">{VIEW_TITLES[view.name] || ''}</span>
        )}
      </div>
      <IconButton icon={SquareTerminal} label="Terminal" active={bottomOpen} onClick={() => toggleBottom()} />
      <IconButton icon={PanelRight} label="Workbench" active={workbenchOpen} onClick={() => toggleWorkbench()} />
    </header>
  )
}

function SessionTitle({ session }) {
  const Icon = SESSION_ICONS[session.kind]
  const state = session.runtime?.running ? session.runtime.state || 'running' : session.status
  const working = session.runtime?.running && ['running', 'starting', 'connecting', 'initializing'].includes(state)
  // A live agent process that is between turns; stopping it lets the next message start it with the current settings.
  const restartable = session.runtime?.running && ['idle', 'completed', 'interrupted', 'error'].includes(state)
  const patchSession = useApp((s) => s.patchSession)
  const removeSession = useApp((s) => s.removeSession)
  const setup = useApp((s) => s.setup)
  const toast = useApp((s) => s.toast)
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(session.title)
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)

  // The CryoSPARC this session ran on, which may not be the one in Settings (imported sessions, a changed URL).
  const cryosparcUrl = session.config?.agent?.cryosparc_url || session.harness?.cryosparc_url || setup?.cryosparc?.url
  const cryosparcLink = session.kind === 'reconstruction' && cryosparcUrl ? cryosparcUrl : null

  const startEditing = () => {
    setTitle(session.title)
    setEditing(true)
  }

  const restart = async () => {
    try {
      patchSession(await api.stopSession(session.id))
      toast({ title: 'Agent stopped', description: 'It starts again with your current settings when you send the next message.' })
    } catch (err) {
      toast({ title: 'Could not stop the agent', description: err.message, tone: 'error' })
    }
  }

  const save = async () => {
    setEditing(false)
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
    } catch (err) {
      toast({ title: 'Could not delete the session', description: err.message, tone: 'error' })
      setBusy(false)
    }
  }

  return (
    <>
      {Icon ? <Icon size={15} strokeWidth={1.75} className="shrink-0 text-muted" /> : null}
      {editing ? (
        <input
          autoFocus
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={save}
          onKeyDown={(event) => {
            if (event.key === 'Enter') save()
            if (event.key === 'Escape') {
              setTitle(session.title)
              setEditing(false)
            }
          }}
          className="h-7 min-w-0 max-w-[420px] flex-1 rounded-md border border-accent bg-canvas px-2 text-sm font-medium outline-none"
        />
      ) : (
        <button type="button" onDoubleClick={startEditing} className="min-w-0 truncate text-sm font-medium text-fg" title={session.title}>
          {session.title}
        </button>
      )}
      {/* Gives way before the title when the column is narrow. */}
      <span
        className={cx('flex min-w-0 shrink-[4] items-center gap-1.5 text-xs', working ? 'text-accent-text' : 'text-faint')}
        title={stateLabel(state)}
      >
        <StatusDot state={state} />
        <span className="truncate">{stateLabel(state)}</span>
      </span>
      <Menu>
        <MenuTrigger asChild>
          <IconButton icon={MoreHorizontal} label="Session options" size="sm" tooltip={false} />
        </MenuTrigger>
        <MenuContent align="start" className="min-w-[200px]">
          <MenuItem icon={Pencil} onSelect={startEditing}>
            Rename
          </MenuItem>
          {cryosparcLink ? (
            <MenuItem icon={ExternalLink} onSelect={() => window.open(cryosparcLink, '_blank', 'noopener')}>
              Open CryoSPARC
            </MenuItem>
          ) : null}
          {restartable ? (
            <MenuItem icon={RotateCcw} onSelect={restart}>
              Restart agent
            </MenuItem>
          ) : null}
          <MenuSeparator />
          <MenuItem icon={Trash2} danger onSelect={() => setConfirm(true)}>
            Delete
          </MenuItem>
        </MenuContent>
      </Menu>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Delete this session?"
        description="The conversation is removed from CryoPilot. Files and CryoSPARC jobs stay as they are."
        busy={busy}
        onConfirm={remove}
      />
    </>
  )
}

function NoProject({ hasProjects }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="flex h-full items-center justify-center">
      <EmptyState
        icon={null}
        title={null}
        action={
          <Button variant="primary" icon={FolderPlus} onClick={() => setOpen(true)}>
            New project
          </Button>
        }
      >
        <div className="flex flex-col items-center gap-3">
          <BrandMark size={36} />
          <p className="text-lg font-semibold text-fg">{hasProjects ? 'Choose a project' : 'Start with a project'}</p>
          <p>
            A project keeps the reconstruction and modeling sessions for one sample together, with the files they share.
          </p>
        </div>
      </EmptyState>
      <CreateProjectDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}
