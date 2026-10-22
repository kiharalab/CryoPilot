// Terminals in the bottom panel: one shell per tab, opened in the project's
// modeling workspace by default. Nothing starts until the panel is first
// shown; shells keep running when the panel closes or the page reloads.
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Folder, House, Plus, RotateCcw, SquareTerminal, Trash2, X } from 'lucide-react'
import { cx } from '../../lib/cx'
import { basename } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, EmptyState, IconButton, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Spinner } from '../../components/ui'
import { useProjectRoots } from '../files/roots'
import { applyTerminalTheme, connectionFor, defaultSpec, existingConnection, specOf, useTerminals } from './terminals'

export function TerminalPanel({ active }) {
  const bottomOpen = useApp((state) => state.bottomOpen)
  const projectId = useApp((state) => state.projectId)
  const resolvedTheme = useApp((state) => state.resolvedTheme)
  const toast = useApp((state) => state.toast)
  const terminals = useTerminals((state) => state.terminals)
  const activeId = useTerminals((state) => state.activeId)
  const ready = useTerminals((state) => state.ready)
  const error = useTerminals((state) => state.error)
  const { roots } = useProjectRoots(projectId)
  const containerRef = useRef(null)
  const [creating, setCreating] = useState(false)
  const shown = active && bottomOpen

  useEffect(() => {
    if (shown && !ready) useTerminals.getState().init(projectId)
  }, [shown, ready, projectId])

  // Place every terminal in the host; only the selected one is visible.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    for (const terminal of terminals) {
      const connection = connectionFor(terminal.id)
      connection.mount(container)
      connection.setVisible(terminal.id === activeId)
    }
  }, [terminals, activeId])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    let frame = 0
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const id = useTerminals.getState().activeId
        if (id) existingConnection(id)?.fit()
      })
    })
    observer.observe(container)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [])

  useEffect(() => {
    applyTerminalTheme()
  }, [resolvedTheme])

  // Focus the shell when the user opens the panel or picks a terminal, but not
  // while the window first lays itself out with the panel already open.
  const mountedAt = useRef(performance.now())
  useEffect(() => {
    if (!shown || !activeId || performance.now() - mountedAt.current < 1500) return undefined
    const frame = requestAnimationFrame(() => existingConnection(activeId)?.focus())
    return () => cancelAnimationFrame(frame)
  }, [shown, activeId])

  const labelled = useMemo(() => {
    const seen = new Map()
    return terminals.map((terminal) => {
      const base = terminal.title || basename(terminal.cwd || '') || 'Terminal'
      const count = (seen.get(base) || 0) + 1
      seen.set(base, count)
      return { terminal, label: count > 1 ? `${base} ${count}` : base }
    })
  }, [terminals])

  const create = async (spec) => {
    setCreating(true)
    try {
      await useTerminals.getState().createTerminal(spec)
    } catch (err) {
      toast({ title: 'Could not open a terminal', description: err.message, tone: 'error' })
    } finally {
      setCreating(false)
    }
  }

  const close = async (id) => {
    try {
      await useTerminals.getState().closeTerminal(id)
    } catch (err) {
      toast({ title: 'Could not close the terminal', description: err.message, tone: 'error' })
    }
  }

  const restart = async (terminal) => {
    setCreating(true)
    try {
      await useTerminals.getState().restartTerminal(terminal.id, specOf(terminal))
    } catch (err) {
      toast({ title: 'Could not restart the terminal', description: err.message, tone: 'error' })
    } finally {
      setCreating(false)
    }
  }

  const current = terminals.find((terminal) => terminal.id === activeId) || null

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-1 pl-2 pr-1.5">
        <div role="tablist" aria-label="Terminals" className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
          {labelled.map(({ terminal, label }) => {
            const selected = terminal.id === activeId
            return (
              <div
                key={terminal.id}
                role="tab"
                aria-selected={selected}
                tabIndex={0}
                title={terminal.cwd || undefined}
                onClick={() => useTerminals.getState().setActive(terminal.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') useTerminals.getState().setActive(terminal.id)
                }}
                onAuxClick={(event) => {
                  if (event.button === 1) close(terminal.id)
                }}
                className={cx(
                  'group flex h-6 max-w-[180px] shrink-0 cursor-default items-center gap-1.5 rounded-md pl-2 pr-1 text-xs transition-colors',
                  selected ? 'bg-hover text-fg' : 'text-muted hover:text-fg',
                )}
              >
                <SquareTerminal size={12} strokeWidth={1.75} className={cx('shrink-0', terminal.status === 'exited' && 'text-faint')} />
                <span className={cx('truncate', terminal.status === 'exited' && 'text-faint')}>{label}</span>
                <button
                  type="button"
                  aria-label={`Close ${label}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    close(terminal.id)
                  }}
                  className={cx(
                    'flex h-4 w-4 shrink-0 items-center justify-center rounded hover:bg-line',
                    selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
                  )}
                >
                  <X size={11} />
                </button>
              </div>
            )
          })}
        </div>
        {creating ? (
          <span className="flex h-6 w-6 items-center justify-center">
            <Spinner size={12} />
          </span>
        ) : (
          <IconButton icon={Plus} label="New terminal" size="xs" onClick={() => create(defaultSpec(projectId))} />
        )}
        <Menu>
          <MenuTrigger asChild>
            <IconButton icon={ChevronDown} label="New terminal in another folder" size="xs" />
          </MenuTrigger>
          <MenuContent align="end" className="min-w-[230px]">
            <MenuLabel>New terminal in</MenuLabel>
            {projectId
              ? (roots || []).map((root) => (
                  <MenuItem key={root.key} icon={Folder} onSelect={() => create({ project_id: projectId, root: root.key })}>
                    {root.label}
                  </MenuItem>
                ))
              : null}
            <MenuItem icon={House} onSelect={() => create({})}>
              Home folder
            </MenuItem>
            {current ? (
              <>
                <MenuSeparator />
                <MenuItem icon={Trash2} danger onSelect={() => close(current.id)}>
                  Close this terminal
                </MenuItem>
              </>
            ) : null}
          </MenuContent>
        </Menu>
      </div>

      <div className="relative min-h-0 flex-1">
        <div ref={containerRef} className="absolute inset-0" />

        {!terminals.length ? (
          <div className="absolute inset-0 bg-canvas">
            {error ? (
              <EmptyState
                icon={SquareTerminal}
                title="The terminal could not start"
                action={
                  <Button size="sm" icon={RotateCcw} onClick={() => useTerminals.getState().init(projectId)}>
                    Try again
                  </Button>
                }
              >
                {error}
              </EmptyState>
            ) : ready ? (
              <EmptyState
                icon={SquareTerminal}
                title="No terminal open"
                action={
                  <Button size="sm" icon={Plus} loading={creating} onClick={() => create(defaultSpec(projectId))}>
                    New terminal
                  </Button>
                }
              >
                Terminals run on the server, in the project's modeling workspace unless you choose another folder.
              </EmptyState>
            ) : shown ? (
              <div className="flex h-full items-center justify-center">
                <Spinner />
              </div>
            ) : null}
          </div>
        ) : null}

        {current?.status === 'reconnecting' ? (
          <div className="pointer-events-none absolute right-3 top-2 flex items-center gap-2 rounded-lg border border-line bg-elevated px-2.5 py-1 text-xs text-muted shadow-popover">
            <Spinner size={12} />
            Reconnecting
          </div>
        ) : null}

        {current?.status === 'exited' ? (
          <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-line bg-elevated py-1.5 pl-3.5 pr-1.5 text-sm shadow-popover">
            <span className="whitespace-nowrap text-muted">
              Process exited{current.exitCode !== null && current.exitCode !== undefined ? ` with code ${current.exitCode}` : ''}
            </span>
            <Button size="sm" variant="primary" icon={RotateCcw} loading={creating} onClick={() => restart(current)}>
              Restart
            </Button>
            <Button size="sm" variant="ghost" onClick={() => close(current.id)}>
              Close
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  )
}
