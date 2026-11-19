// Global app state (zustand). Feature components read slices with
// useApp((s) => s.thing) and call the actions below; nothing else talks to
// the router or the layout directly.
import { create } from 'zustand'
import { api } from '../lib/api'

const THEME_KEY = 'cryopilot-theme'
const PROJECT_KEY = 'cryopilot-project'
const UPDATE_RESTART_TIMEOUT_MS = 2 * 60 * 1000

function readStorage(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function writeStorage(key, value) {
  try {
    if (value === null || value === undefined) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    // private window; the choice simply is not remembered
  }
}

export function applyTheme(choice) {
  const dark =
    choice === 'dark' || (choice === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  return dark ? 'dark' : 'light'
}

let viewerRequestId = 0
let toastId = 0

export const useApp = create((set, get) => ({
  // ------------------------------------------------------------- server data
  /** /api/setup payload; null until loaded. */
  setup: null,
  /** /api/update payload; null until loaded. */
  update: null,
  updateRestarting: false,
  projects: [],
  projectId: readStorage(PROJECT_KEY, null),
  /** The current project with .sessions and .directories, or null. */
  project: null,
  projectLoading: false,

  // -------------------------------------------------------------- navigation
  /**
   * What the main column shows:
   *   {name: 'home'}                         new-session chooser
   *   {name: 'new-reconstruction'}           dataset card + goal composer
   *   {name: 'new-modeling', attachments?}   free-text composer
   *   {name: 'session', sessionId}           a conversation
   */
  view: { name: 'home' },

  // ------------------------------------------------------------------ layout
  sidebarOpen: true,
  workbenchOpen: true,
  bottomOpen: false,
  /** 'viewer' | 'maps' | 'daq' | 'files' | 'file:<root>:<path>' */
  workbenchTab: 'viewer',
  /** File preview tabs in the workbench: [{root, path, name}] */
  openFiles: [],
  /** 'terminal' | 'logs' */
  bottomTab: 'terminal',
  /**
   * A DAQ result the DAQ panel should select next: the CSV path relative to the
   * modeling workspace (as in api.daqResults().results[].path). Set by openDaq(),
   * cleared by the panel with clearDaqFocus() once it has selected it.
   */
  daqFocusPath: null,
  settingsOpen: false,
  /** 'general' | 'accounts' | 'reconstruction' | 'modeling' | 'alerts' | 'spending' | 'about' */
  settingsSection: 'general',
  theme: readStorage(THEME_KEY, 'system'),
  resolvedTheme: document.documentElement.dataset.theme || 'light',

  // ------------------------------------------------------------------ viewer
  /**
   * Queue of things to show in Mol*. The viewer drains it with takeViewerRequests().
   *   {kind:'volume', url, label, key?, sigma?, level?, color?, opacity?, format?:'ccp4'|'dscif', entryId?}
   *       sigma: level in σ above the mean; level: an absolute level, used when sigma is absent.
   *       Without either: the map's previous level, the EMDB recommended contour, or the level enclosing 1% of voxels.
   *   {kind:'structure', url, label, key?, format?:'pdb'|'mmcif', color?:'chain'|'daq', daqDomain?:[min,max], daqColors?}
   *       format is guessed from the label or URL when absent
   *   {kind:'focus', key?, chain, resnum, icode?}   key defaults to the newest model
   *   {kind:'remove', key}
   * `key` identifies a layer so a later request replaces it instead of adding a copy.
   */
  viewerRequests: [],

  // ------------------------------------------------------------------ toasts
  toasts: [],

  // ================================================================ actions
  async refreshSetup() {
    const setup = await api.setup()
    set({ setup })
    return setup
  },
  setSetup(setup) {
    set({ setup })
  },

  async refreshUpdate() {
    set({ update: await api.update() })
  },
  async installUpdate() {
    try {
      set({ update: await api.installUpdate() })
    } catch (err) {
      get().toast({ tone: 'error', title: err.message })
    }
  },
  /** Restarts into the installed update, and loads the new app once the server answers again. */
  async restartForUpdate() {
    const expected = get().update?.install?.version
    set({ updateRestarting: true })
    try {
      await api.restartForUpdate()
    } catch (err) {
      set({ updateRestarting: false })
      get().toast({ tone: 'error', title: err.message })
      return
    }
    const deadline = Date.now() + UPDATE_RESTART_TIMEOUT_MS
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1000))
      try {
        const update = await api.update()
        if (update.current === expected) {
          window.location.reload()
          return
        }
      } catch {
        // The server is still restarting.
      }
    }
    set({ updateRestarting: false })
    get().toast({ tone: 'error', title: 'CryoPilot did not come back after the restart.', description: 'Check its terminal, or run: cryopilot log' })
  },

  async loadProjects() {
    const { projects } = await api.projects()
    set({ projects })
    const { projectId } = get()
    const exists = projects.some((project) => project.id === projectId)
    const next = exists ? projectId : projects[0]?.id || null
    if (next) await get().selectProject(next, { keepView: exists })
    else set({ projectId: null, project: null })
    return projects
  },

  async selectProject(projectId, { keepView = false } = {}) {
    const previous = { projectId: get().projectId, project: get().project }
    writeStorage(PROJECT_KEY, projectId)
    // Without keepView the old project goes at once, so nothing keeps showing
    // (or writing to) it while projectId already names the new one.
    set({
      projectId,
      projectLoading: true,
      ...(keepView ? {} : { project: null, view: { name: 'home' }, openFiles: [], workbenchTab: 'viewer' }),
    })
    try {
      const project = await api.project(projectId)
      if (get().projectId === projectId) set({ project, projectLoading: false })
      return project
    } catch (err) {
      // Go back to the project that was open, unless the user has moved on.
      if (get().projectId === projectId) {
        writeStorage(PROJECT_KEY, previous.projectId)
        set({ projectId: previous.projectId, project: previous.project, projectLoading: false })
      }
      throw err
    }
  },

  /**
   * Bring the sidebar up to date with sessions that are not open. Only the open
   * session has a live feed, so the others would otherwise keep the state they
   * had when the project loaded, and a finished run would look busy.
   */
  async refreshSessions() {
    const { projectId, view } = get()
    if (!projectId) return
    const { sessions } = await api.projectSessions(projectId)
    if (get().projectId !== projectId) return
    const open = get().view.name === 'session' ? get().view.sessionId : view.name === 'session' ? view.sessionId : null
    for (const session of sessions) {
      if (session.id !== open) get().patchSession(session)
    }
  },

  async refreshProject() {
    const { projectId } = get()
    if (!projectId) return null
    const project = await api.project(projectId)
    if (get().projectId === projectId) set({ project })
    return project
  },

  async createProject(body) {
    const project = await api.createProject(body)
    set((state) => ({ projects: [project, ...state.projects] }))
    await get().selectProject(project.id)
    return project
  },

  /**
   * Merge a partial session (from SSE or an API response) into the current project.
   * A session of another project is ignored; a session the list does not have yet
   * is added only when the patch is a whole session (it has `kind` and `project_id`).
   */
  patchSession(patch) {
    if (!patch?.id) return
    set((state) => {
      if (!state.project) return {}
      if (patch.project_id && patch.project_id !== state.project.id) return {}
      const sessions = state.project.sessions || []
      const index = sessions.findIndex((session) => session.id === patch.id)
      if (index === -1 && !(patch.kind && patch.project_id)) return {}
      const nextSessions =
        index === -1
          ? [patch, ...sessions]
          : sessions.map((session) =>
              session.id === patch.id
                ? { ...session, ...patch, runtime: { ...(session.runtime || {}), ...(patch.runtime || {}) } }
                : session,
            )
      return { project: { ...state.project, sessions: nextSessions } }
    })
  },

  removeSession(sessionId) {
    set((state) => ({
      project: state.project
        ? { ...state.project, sessions: (state.project.sessions || []).filter((session) => session.id !== sessionId) }
        : state.project,
      view: state.view.name === 'session' && state.view.sessionId === sessionId ? { name: 'home' } : state.view,
    }))
  },

  navigate(view) {
    set({ view })
  },
  openSession(sessionId) {
    set({ view: { name: 'session', sessionId } })
  },

  // ---------------------------------------------------------------- layout
  toggleSidebar(open) {
    set((state) => ({ sidebarOpen: open ?? !state.sidebarOpen }))
  },
  toggleWorkbench(open) {
    set((state) => ({ workbenchOpen: open ?? !state.workbenchOpen }))
  },
  toggleBottom(open, tab) {
    set((state) => ({ bottomOpen: open ?? !state.bottomOpen, ...(tab ? { bottomTab: tab } : {}) }))
  },
  setBottomTab(bottomTab) {
    set({ bottomTab, bottomOpen: true })
  },
  setWorkbenchTab(workbenchTab) {
    set({ workbenchTab, workbenchOpen: true })
  },
  /** Bring the DAQ tab forward and ask it to select this result (see daqFocusPath). */
  openDaq(path) {
    set({ daqFocusPath: path, workbenchTab: 'daq', workbenchOpen: true })
  },
  clearDaqFocus() {
    set({ daqFocusPath: null })
  },
  openFile(file) {
    const tab = `file:${file.root}:${file.path}`
    set((state) => ({
      openFiles: state.openFiles.some((entry) => entry.root === file.root && entry.path === file.path)
        ? state.openFiles
        : [...state.openFiles, file],
      workbenchTab: tab,
      workbenchOpen: true,
    }))
  },
  closeFile(file) {
    const tab = `file:${file.root}:${file.path}`
    set((state) => ({
      openFiles: state.openFiles.filter((entry) => !(entry.root === file.root && entry.path === file.path)),
      workbenchTab: state.workbenchTab === tab ? 'files' : state.workbenchTab,
    }))
  },
  openSettings(section = 'general') {
    set({ settingsOpen: true, settingsSection: section })
  },
  closeSettings() {
    set({ settingsOpen: false })
  },
  setSettingsSection(settingsSection) {
    set({ settingsSection })
  },
  setTheme(theme) {
    writeStorage(THEME_KEY, theme)
    set({ theme, resolvedTheme: applyTheme(theme) })
  },
  syncSystemTheme() {
    set({ resolvedTheme: applyTheme(get().theme) })
  },

  // ---------------------------------------------------------------- viewer
  /** Show something in Mol* and bring the viewer tab forward. */
  showInViewer(request) {
    viewerRequestId += 1
    set((state) => ({
      viewerRequests: [...state.viewerRequests, { ...request, id: viewerRequestId }],
      workbenchTab: request.kind === 'focus' || request.kind === 'remove' ? state.workbenchTab : 'viewer',
      workbenchOpen: true,
    }))
  },
  takeViewerRequests() {
    const requests = get().viewerRequests
    if (requests.length) set({ viewerRequests: [] })
    return requests
  },

  // ---------------------------------------------------------------- toasts
  /** toast('Saved') or toast({title, description, tone:'error'|'success'|'neutral'}) */
  toast(input) {
    toastId += 1
    const toast = typeof input === 'string' ? { title: input } : input
    const entry = { tone: 'neutral', ...toast, id: toastId }
    set((state) => ({ toasts: [...state.toasts.slice(-3), entry] }))
    setTimeout(() => get().dismissToast(entry.id), toast.duration || (entry.tone === 'error' ? 7000 : 3500))
    return entry.id
  },
  dismissToast(id) {
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }))
  },
}))

/** The session object for the current view, from the loaded project. */
export function useCurrentSession() {
  return useApp((state) =>
    state.view.name === 'session' ? (state.project?.sessions || []).find((session) => session.id === state.view.sessionId) || null : null,
  )
}
