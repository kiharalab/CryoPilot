// The main window: sidebar | conversation | workbench (viewer, maps, DAQ,
// files) over the terminal. Every divider can be dragged; the sidebar, the
// workbench, and the terminal also collapse from the keyboard.
import { useEffect, useMemo, useRef } from 'react'
import { Group, Panel, Separator, useDefaultLayout, useGroupRef, usePanelRef } from 'react-resizable-panels'
import { useApp } from '../store/app'
import { hasModKey } from '../lib/platform'
import { SettingsDialog } from '../features/settings/SettingsDialog'
import { Sidebar } from './Sidebar'
import { MainColumn } from './MainColumn'
import { RightColumn } from './RightColumn'

const safeStorage = {
  getItem(key) {
    try {
      return localStorage.getItem(key)
    } catch {
      return null
    }
  },
  setItem(key, value) {
    try {
      localStorage.setItem(key, value)
    } catch {
      // layout is simply not remembered
    }
  },
}

/** Workbench over terminal when both are open, until the user drags the divider. */
const DEFAULT_SPLIT = { workbench: 64, bottom: 36 }

/** Shortcuts that also work while typing; the others leave text fields alone. */
const TYPING_SHORTCUTS = new Set(['k', ','])

function isTextEntry(target) {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable || target instanceof HTMLTextAreaElement) return true
  return target instanceof HTMLInputElement && !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(target.type)
}

/**
 * Keeps a collapsible panel of the column group in step with an open/closed
 * flag in the store. `preferred` (a Panel size: pixels as a number, percent as
 * a string) is the size it reopens at when it would otherwise come back much
 * smaller. Sizes reported before the first sync are ignored, so a restored
 * layout or the first measurement cannot override the store.
 */
function useCollapsible(panelRef, groupElementRef, open, setOpen, preferred) {
  const synced = useRef(false)
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    if (open && panel.isCollapsed()) {
      panel.expand()
      // The panel still measures 0 px until it re-renders, so compare percentages of the group.
      const groupWidth = groupElementRef.current?.offsetWidth || 0
      const percent = typeof preferred === 'number' ? (groupWidth ? (preferred / groupWidth) * 100 : 0) : parseFloat(preferred)
      if (panel.getSize().asPercentage < percent * 0.6) panel.resize(preferred)
    }
    if (!open && !panel.isCollapsed()) panel.collapse()
    synced.current = true
  }, [open, panelRef, groupElementRef, preferred])
  return (size) => {
    if (!synced.current) return
    const collapsed = size.inPixels < 1
    if (collapsed && open) setOpen(false)
    if (!collapsed && !open) setOpen(true)
  }
}

export function Workspace() {
  const loadProjects = useApp((state) => state.loadProjects)
  const toast = useApp((state) => state.toast)
  const sidebarOpen = useApp((state) => state.sidebarOpen)
  const workbenchOpen = useApp((state) => state.workbenchOpen)
  const bottomOpen = useApp((state) => state.bottomOpen)
  const toggleSidebar = useApp((state) => state.toggleSidebar)
  const toggleWorkbench = useApp((state) => state.toggleWorkbench)
  const toggleBottom = useApp((state) => state.toggleBottom)
  const openSettings = useApp((state) => state.openSettings)
  const navigate = useApp((state) => state.navigate)

  const columnsRef = useRef(null)
  const sidebarRef = usePanelRef()
  const rightRef = usePanelRef()
  const innerRef = useGroupRef()

  const outer = useDefaultLayout({ id: 'cryopilot-columns', storage: safeStorage })
  // Only the user's own split is saved; the full-height layouts below come from the store.
  const inner = useDefaultLayout({ id: 'cryopilot-right', storage: safeStorage, onlySaveAfterUserInteractions: true })
  const split = useRef(null)
  if (split.current === null) {
    const saved = inner.defaultLayout
    split.current = saved?.workbench >= 1 && saved?.bottom >= 1 ? { workbench: saved.workbench, bottom: saved.bottom } : DEFAULT_SPLIT
  }

  useEffect(() => {
    loadProjects().catch((err) => toast({ title: 'Could not load projects', description: err.message, tone: 'error' }))
  }, [loadProjects, toast])

  // The right column collapses as a whole when neither the workbench nor the
  // terminal is open. Inside it the two always fill the height, so they are
  // laid out from the store instead of collapsing one by one.
  const rightOpen = workbenchOpen || bottomOpen
  const onSidebarResize = useCollapsible(sidebarRef, columnsRef, sidebarOpen, toggleSidebar, 264)
  const onRightResize = useCollapsible(
    rightRef,
    columnsRef,
    rightOpen,
    (open) => {
      if (!open) {
        toggleWorkbench(false)
        toggleBottom(false)
      } else if (!workbenchOpen && !bottomOpen) {
        toggleWorkbench(true)
      }
    },
    '42%',
  )

  useEffect(() => {
    const group = innerRef.current
    if (!group || !rightOpen) return
    const current = group.getLayout()
    let target
    if (workbenchOpen && bottomOpen) {
      if (current.workbench >= 1 && current.bottom >= 1) return
      target = split.current
    } else {
      target = workbenchOpen ? { workbench: 100, bottom: 0 } : { workbench: 0, bottom: 100 }
    }
    if (current.workbench === target.workbench && current.bottom === target.bottom) return
    group.setLayout(target)
  }, [innerRef, rightOpen, workbenchOpen, bottomOpen])

  // Only a drag or a key press on the divider changes what is open.
  const onInnerLayoutChanged = (layout, meta) => {
    inner.onLayoutChanged(layout, meta)
    if (!meta.isUserInteraction) return
    const workbench = layout.workbench >= 1
    const bottom = layout.bottom >= 1
    if (workbench && bottom) split.current = { workbench: layout.workbench, bottom: layout.bottom }
    toggleWorkbench(workbench)
    toggleBottom(bottom)
  }

  const shortcuts = useMemo(
    () => ({
      b: () => toggleSidebar(),
      j: () => toggleBottom(),
      '\\': () => toggleWorkbench(),
      ',': () => openSettings(),
      k: () => navigate({ name: 'home' }),
    }),
    [toggleSidebar, toggleBottom, toggleWorkbench, openSettings, navigate],
  )

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.defaultPrevented || !hasModKey(event) || event.altKey || event.shiftKey) return
      const key = event.key.toLowerCase()
      const action = shortcuts[key]
      if (!action) return
      const target = event.target instanceof Element ? event.target : null
      // Leave keys to the terminal while it has focus, and to open dialogs.
      if (target?.closest('.xterm, [role="dialog"]')) return
      if (isTextEntry(target) && !TYPING_SHORTCUTS.has(key)) return
      event.preventDefault()
      action()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [shortcuts])

  return (
    <div className="flex h-full bg-canvas text-fg">
      <Group
        orientation="horizontal"
        id="cryopilot-columns"
        elementRef={columnsRef}
        defaultLayout={outer.defaultLayout}
        onLayoutChanged={outer.onLayoutChanged}
      >
        <Panel
          id="sidebar"
          panelRef={sidebarRef}
          defaultSize={264}
          minSize={216}
          maxSize={420}
          collapsible
          collapsedSize={0}
          groupResizeBehavior="preserve-pixel-size"
          onResize={onSidebarResize}
          className="bg-sidebar"
        >
          <Sidebar />
        </Panel>
        <Separator className="resize-handle" />
        <Panel id="main" minSize={380}>
          <MainColumn />
        </Panel>
        <Separator className="resize-handle" />
        <Panel id="right" panelRef={rightRef} defaultSize="42%" minSize={320} collapsible collapsedSize={0} onResize={onRightResize}>
          <Group
            orientation="vertical"
            id="cryopilot-right"
            groupRef={innerRef}
            defaultLayout={inner.defaultLayout}
            onLayoutChanged={onInnerLayoutChanged}
          >
            <Panel id="workbench" defaultSize="64%" minSize={160} collapsible collapsedSize={0}>
              <RightColumn.Workbench />
            </Panel>
            <Separator className="resize-handle" />
            <Panel id="bottom" defaultSize="36%" minSize={120} collapsible collapsedSize={0}>
              <RightColumn.Bottom />
            </Panel>
          </Group>
        </Panel>
      </Group>
      <SettingsDialog />
    </div>
  )
}
