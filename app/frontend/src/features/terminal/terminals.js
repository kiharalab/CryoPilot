// Integrated terminals. The server owns the shells, which survive dropped
// connections and page reloads; this module owns one xterm.js instance and
// WebSocket per shell, outside React, so switching tabs or collapsing the
// panel never loses what is on screen.
import { create } from 'zustand'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { api } from '../../lib/api'
import { terminalTheme } from './theme'

const FONT_FAMILY = '"Geist Mono Variable", ui-monospace, SFMono-Regular, Menlo, monospace'
const FONT_SIZE = 12.5
const MAX_RETRY_DELAY = 10000
// Close codes from app/server/routes_terminal.py.
const CLOSE_NOT_FOUND = 4404
const CLOSE_TOO_SLOW = 1013

// xterm measures the font when it opens, so wait for Geist Mono first.
const fontReady =
  typeof document !== 'undefined' && document.fonts?.load
    ? document.fonts.load(`${FONT_SIZE}px "Geist Mono Variable"`).catch(() => null)
    : Promise.resolve(null)

const connections = new Map()
let initRequest = null

function setEntry(id, patch) {
  useTerminals.getState().update(id, patch)
}

class Connection {
  constructor(id) {
    this.id = id
    this.element = document.createElement('div')
    this.element.className = 'terminal-host'
    this.element.style.cssText = 'position:absolute;inset:0;'
    this.fitAddon = new FitAddon()
    this.term = null
    this.socket = null
    this.opening = null
    this.connectedOnce = false
    this.retries = 0
    this.retryTimer = null
    this.exited = false
    this.disposed = false
    this.pendingFocus = false
  }

  /** Puts the terminal into `container` (once), opening xterm and the socket on first use. */
  mount(container) {
    if (this.disposed) return
    if (this.element.parentElement !== container) container.appendChild(this.element)
    if (!this.opening) this.opening = fontReady.then(() => this.open())
  }

  open() {
    if (this.disposed) return
    const term = new Terminal({
      fontFamily: FONT_FAMILY,
      fontSize: FONT_SIZE,
      lineHeight: 1.25,
      cursorBlink: true,
      cursorStyle: 'bar',
      cursorInactiveStyle: 'outline',
      scrollback: 10000,
      macOptionIsMeta: true,
      theme: terminalTheme(),
    })
    term.loadAddon(this.fitAddon)
    term.loadAddon(new WebLinksAddon((_event, uri) => window.open(uri, '_blank', 'noopener,noreferrer')))
    term.attachCustomKeyEventHandler((event) => {
      // ⌘K clears the screen, as in macOS terminals.
      if (event.type === 'keydown' && event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === 'k') {
        term.clear()
        return false
      }
      return true
    })
    term.onData((data) => this.send({ type: 'input', data }))
    term.onBinary((data) => this.send({ type: 'input', data }))
    term.onResize(({ cols, rows }) => this.send({ type: 'resize', cols, rows }))
    term.open(this.element)
    this.term = term
    this.fit()
    if (this.pendingFocus) term.focus()
    this.connect()
  }

  connect() {
    if (this.disposed || this.exited) return
    clearTimeout(this.retryTimer)
    let socket
    try {
      socket = new WebSocket(api.terminalSocketUrl(this.id))
    } catch {
      this.handleDrop()
      return
    }
    this.socket = socket
    setEntry(this.id, { status: this.connectedOnce ? 'reconnecting' : 'connecting' })

    socket.onopen = () => {
      if (this.socket !== socket) return
      // The server replays its scrollback on every connect; start from a
      // clean screen so a reconnect does not show everything twice.
      if (this.connectedOnce) this.term.reset()
      this.connectedOnce = true
      this.retries = 0
      setEntry(this.id, { status: 'open' })
      this.send({ type: 'resize', cols: this.term.cols, rows: this.term.rows })
    }
    socket.onmessage = (event) => {
      if (this.socket !== socket) return
      let message
      try {
        message = JSON.parse(event.data)
      } catch {
        return
      }
      if (message.type === 'output' && typeof message.data === 'string') this.term.write(message.data)
      else if (message.type === 'exit') this.markExited(message.code)
    }
    socket.onclose = (event) => {
      if (this.socket !== socket) return
      this.socket = null
      if (this.disposed || this.exited) return
      if (event.code === CLOSE_NOT_FOUND) {
        this.markExited(null)
      } else if (event.code === CLOSE_TOO_SLOW) {
        // The shell printed faster than the page could take it; reconnect and
        // pick up from the replayed scrollback.
        this.retryTimer = setTimeout(() => this.connect(), 250)
      } else {
        this.handleDrop()
      }
    }
  }

  /** The socket closed without an exit message: reconnect while the shell is still alive. */
  async handleDrop() {
    setEntry(this.id, { status: 'reconnecting' })
    let alive = true
    try {
      const { terminals = [] } = await api.terminals()
      const entry = terminals.find((terminal) => terminal.id === this.id)
      alive = Boolean(entry) && entry.alive !== false
    } catch {
      // The server is unreachable (restarting, network); keep trying.
    }
    if (this.disposed || this.exited) return
    if (!alive) {
      this.markExited(null)
      return
    }
    const delay = Math.min(1000 * 2 ** this.retries, MAX_RETRY_DELAY)
    this.retries += 1
    this.retryTimer = setTimeout(() => this.connect(), delay)
  }

  markExited(code) {
    if (this.exited) return
    this.exited = true
    clearTimeout(this.retryTimer)
    const suffix = code === null || code === undefined ? '' : ` with code ${code}`
    this.term?.write(`\r\n\x1b[2m[process exited${suffix}]\x1b[0m\r\n`)
    setEntry(this.id, { status: 'exited', alive: false, exitCode: code ?? null })
    const socket = this.socket
    this.socket = null
    try {
      socket?.close()
    } catch {
      // already closed
    }
  }

  send(message) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message))
  }

  /** Resizes to the host; skipped while hidden or collapsed so the shell keeps a sane size. */
  fit() {
    if (!this.term || this.disposed) return
    const { clientWidth, clientHeight } = this.element
    if (clientWidth < 40 || clientHeight < 24) return
    try {
      this.fitAddon.fit()
    } catch {
      // not rendered yet
    }
  }

  setVisible(visible) {
    this.element.style.display = visible ? '' : 'none'
    if (visible && this.term) {
      requestAnimationFrame(() => {
        this.fit()
        this.term?.refresh(0, this.term.rows - 1)
      })
    }
  }

  focus() {
    if (this.term) this.term.focus()
    else this.pendingFocus = true
  }

  setTheme(theme) {
    if (this.term) this.term.options.theme = theme
  }

  dispose() {
    this.disposed = true
    clearTimeout(this.retryTimer)
    const socket = this.socket
    this.socket = null
    try {
      socket?.close()
    } catch {
      // already closed
    }
    this.term?.dispose()
    this.element.remove()
  }
}

/** The xterm connection for a terminal id, created on first use. */
export function connectionFor(id) {
  let connection = connections.get(id)
  if (!connection) {
    connection = new Connection(id)
    connections.set(id, connection)
  }
  return connection
}

export function existingConnection(id) {
  return connections.get(id) || null
}

function dropConnection(id) {
  connections.get(id)?.dispose()
  connections.delete(id)
}

/** Re-reads the colour tokens; call after the app theme changes. */
export function applyTerminalTheme() {
  if (!connections.size) return
  const theme = terminalTheme()
  for (const connection of connections.values()) connection.setTheme(theme)
}

/** Where "+" opens a terminal: the project's modeling workspace, or the home folder without a project. */
export function defaultSpec(projectId) {
  return projectId ? { project_id: projectId, root: 'modeling' } : {}
}

function initialStatus(terminal) {
  return terminal.alive === false ? 'exited' : 'connecting'
}

/** The request that opens a shell like this one again (same project root, or the home folder). */
export function specOf(terminal) {
  if (terminal.spec) return terminal.spec
  if (terminal.project_id) return { project_id: terminal.project_id, root: terminal.root || 'modeling' }
  return {}
}

function entryFor(terminal, spec, previous) {
  return {
    ...terminal,
    spec: spec ?? previous?.spec,
    status: previous?.status || initialStatus(terminal),
    exitCode: previous?.exitCode ?? terminal.exit_code ?? null,
  }
}

export const useTerminals = create((set, get) => ({
  /** Server terminals ({id, title, cwd, project_id, root, alive, exit_code, ...}) plus spec, status, exitCode; status: connecting | open | reconnecting | exited */
  terminals: [],
  activeId: null,
  /** True once the server's list has been read (and a first terminal created if there was none). */
  ready: false,
  error: null,

  /** Attaches to the server's terminals; creates one when none is running. Safe to call repeatedly. */
  init(projectId) {
    if (get().ready) return Promise.resolve()
    if (!initRequest) {
      initRequest = (async () => {
        try {
          set({ error: null })
          const { terminals = [] } = await api.terminals()
          set((state) => {
            const local = new Map(state.terminals.map((terminal) => [terminal.id, terminal]))
            const merged = terminals.map((terminal) => entryFor(terminal, undefined, local.get(terminal.id)))
            // Terminals created here while the list was loading.
            for (const terminal of state.terminals) if (!merged.some((entry) => entry.id === terminal.id)) merged.push(terminal)
            const activeId = merged.some((entry) => entry.id === state.activeId) ? state.activeId : merged[merged.length - 1]?.id || null
            return { terminals: merged, activeId }
          })
          if (!get().terminals.length) await get().createTerminal(defaultSpec(projectId))
          set({ ready: true })
        } catch (err) {
          set({ error: err.message || 'Could not open a terminal.' })
        } finally {
          initRequest = null
        }
      })()
    }
    return initRequest
  },

  async createTerminal(spec = {}) {
    const terminal = await api.createTerminal(spec)
    set((state) => ({
      terminals: [...state.terminals.filter((entry) => entry.id !== terminal.id), entryFor(terminal, spec)],
      activeId: terminal.id,
    }))
    return terminal
  },

  /** Ends the shell and removes its tab. */
  async closeTerminal(id) {
    try {
      await api.closeTerminal(id)
    } catch (err) {
      if (err.status !== 404) throw err
    }
    dropConnection(id)
    set((state) => {
      const index = state.terminals.findIndex((terminal) => terminal.id === id)
      const terminals = state.terminals.filter((terminal) => terminal.id !== id)
      const activeId = state.activeId === id ? terminals[Math.min(index, terminals.length - 1)]?.id || null : state.activeId
      return { terminals, activeId }
    })
  },

  /** Replaces an exited terminal with a fresh shell in the same place. */
  async restartTerminal(id, spec) {
    const terminal = await api.createTerminal(spec)
    try {
      await api.closeTerminal(id)
    } catch {
      // the old shell is already gone
    }
    dropConnection(id)
    set((state) => ({
      terminals: state.terminals.map((entry) => (entry.id === id ? entryFor(terminal, spec) : entry)),
      activeId: terminal.id,
    }))
    return terminal
  },

  setActive(activeId) {
    set({ activeId })
  },

  update(id, patch) {
    set((state) => ({
      terminals: state.terminals.map((terminal) => (terminal.id === id ? { ...terminal, ...patch } : terminal)),
    }))
  },
}))
