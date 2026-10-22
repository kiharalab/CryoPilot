// File explorer over the project's roots: the modeling workspace, uploads,
// reconstruction runs, and each bound CryoSPARC project (read-only). Models
// and maps open in the viewer; everything else opens in a preview tab.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Atom,
  ChevronRight,
  Copy,
  Download,
  Eye,
  FileText,
  Folder,
  FolderOpen,
  Lock,
  MoreHorizontal,
  RefreshCw,
  Search,
  SquareTerminal,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatBytes, formatDateTime } from '../../lib/format'
import { useApp } from '../../store/app'
import {
  Button,
  ConfirmDialog,
  EmptyState,
  IconButton,
  InlineError,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  Skeleton,
  Spinner,
  Tip,
} from '../../components/ui'
import { useTerminals } from '../terminal/terminals'
import { useProjectRoots, useRootsStore } from './roots'
import { absolutePath, downloadUrl, fileIcon, isModelingInput, isViewable, parentPath, viewerRequestForFile } from './fileTypes'

const PAGE = 300
const POLL_MS = 15000
const UPLOAD_TARGET = { root: 'modeling', path: 'data' }

const nodeKey = (root, path) => `${root}\u0000${path}`

/** Every folder from the root down to `path`, inclusive: 'a/b' -> ['', 'a', 'a/b']. */
function ancestors(path) {
  const parts = (path || '').split('/').filter(Boolean)
  return ['', ...parts.map((_, index) => parts.slice(0, index + 1).join('/'))]
}

export function FilesPanel({ active }) {
  const projectId = useApp((state) => state.projectId)
  const project = useApp((state) => state.project)
  const workbenchOpen = useApp((state) => state.workbenchOpen)
  if (!projectId || !project) {
    return (
      <EmptyState icon={Folder} title="No project open">
        Choose or create a project in the sidebar to browse its files.
      </EmptyState>
    )
  }
  return <Explorer key={projectId} projectId={projectId} shown={active && workbenchOpen} />
}

function Explorer({ projectId, shown }) {
  const toast = useApp((state) => state.toast)
  const openFile = useApp((state) => state.openFile)
  const closeFile = useApp((state) => state.closeFile)
  const showInViewer = useApp((state) => state.showInViewer)
  const navigate = useApp((state) => state.navigate)
  const setBottomTab = useApp((state) => state.setBottomTab)
  const workbenchTab = useApp((state) => state.workbenchTab)
  const { roots, error: rootsError, reload: reloadRoots } = useProjectRoots(projectId)

  const [started, setStarted] = useState(false)
  // nodeKey -> {root, path} of every open folder; roots count as folders with path ''.
  const [expanded, setExpanded] = useState(() => new Map([[nodeKey('modeling', ''), { root: 'modeling', path: '' }]]))
  // nodeKey -> {entries, truncated, loading, error}
  const [listings, setListings] = useState({})
  const [limits, setLimits] = useState({})
  const [filter, setFilter] = useState('')
  const [menu, setMenu] = useState(null) // {x, y, target}
  const [confirm, setConfirm] = useState(null) // {root, entry}
  const [deleting, setDeleting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const uploadTarget = useRef(UPLOAD_TARGET)
  const inputRef = useRef(null)
  const treeRef = useRef(null)
  const expandedRef = useRef(expanded)
  const listingsRef = useRef(listings)

  useEffect(() => {
    expandedRef.current = expanded
    listingsRef.current = listings
  })

  useEffect(() => {
    if (shown) setStarted(true)
  }, [shown])

  const loadDir = useCallback(
    async (root, path, { quiet = false } = {}) => {
      const key = nodeKey(root, path)
      if (!quiet) setListings((current) => ({ ...current, [key]: { ...(current[key] || {}), loading: true, error: null } }))
      try {
        const data = await api.listDir(projectId, root, path)
        setListings((current) => ({
          ...current,
          [key]: { entries: data.entries || [], truncated: Boolean(data.truncated), loading: false, error: null },
        }))
      } catch (err) {
        setListings((current) => ({ ...current, [key]: { ...(current[key] || {}), loading: false, error: err.message } }))
      }
    },
    [projectId],
  )

  // Load open folders the first time they are needed.
  useEffect(() => {
    if (!started || !roots) return
    for (const [key, node] of expanded) {
      if (!listings[key] && roots.some((root) => root.key === node.root)) loadDir(node.root, node.path)
    }
  }, [started, roots, expanded, listings, loadDir])

  /** Re-lists every open folder that was loaded before, keeping the current rows on screen meanwhile. */
  const refresh = useCallback(
    async ({ withRoots = true } = {}) => {
      let current = useRootsStore.getState().roots[projectId] || []
      if (withRoots) {
        try {
          current = await useRootsStore.getState().load(projectId, { force: true })
        } catch {
          // keep the roots we had; the listing below still refreshes
        }
      }
      const nodes = [...expandedRef.current.entries()].filter(
        ([key, node]) => listingsRef.current[key] && current.some((root) => root.key === node.root),
      )
      await Promise.all(nodes.map(([, node]) => loadDir(node.root, node.path, { quiet: true })))
    },
    [projectId, loadDir],
  )

  // New agent outputs show up on their own while the panel is visible.
  useEffect(() => {
    if (!shown) return undefined
    refresh({ withRoots: false })
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refresh()
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [shown, refresh])

  const toggle = (root, path) => {
    const key = nodeKey(root, path)
    if (expanded.has(key)) {
      setExpanded((current) => {
        const next = new Map(current)
        next.delete(key)
        return next
      })
      return
    }
    setExpanded((current) => new Map(current).set(key, { root, path }))
    // Re-list on every open so the folder is current; cached rows show meanwhile.
    loadDir(root, path, { quiet: Boolean(listings[key]?.entries) })
  }

  const reveal = (root, path) => {
    setExpanded((current) => {
      const next = new Map(current)
      for (const folder of ancestors(path)) next.set(nodeKey(root, folder), { root, path: folder })
      return next
    })
    for (const folder of ancestors(path)) loadDir(root, folder, { quiet: Boolean(listingsRef.current[nodeKey(root, folder)]?.entries) })
  }

  // ------------------------------------------------------------- actions
  const upload = async (files, target = UPLOAD_TARGET) => {
    const list = Array.from(files || [])
    if (!list.length) return
    setUploading(true)
    let done = 0
    try {
      for (const file of list) {
        await api.uploadFile(projectId, target.root, target.path, file)
        done += 1
      }
      toast({
        title: done === 1 ? `Uploaded ${list[0].name}` : `Uploaded ${done} files`,
        description: target.path ? `Saved in ${target.path}/` : undefined,
        tone: 'success',
      })
    } catch (err) {
      toast({
        title: done ? `Uploaded ${done} of ${list.length} files` : 'Upload failed',
        description: err.message,
        tone: 'error',
      })
    } finally {
      setUploading(false)
      reveal(target.root, target.path)
    }
  }

  const chooseUpload = (target) => {
    uploadTarget.current = target
    inputRef.current?.click()
  }

  const view = (root, entry) => {
    showInViewer(viewerRequestForFile(api.fileUrl(projectId, root.key, entry.path), root.key, entry.path, entry.name))
  }

  const preview = (root, entry) => openFile({ root: root.key, path: entry.path, name: entry.name })

  const activate = (root, entry) => {
    if (entry.type === 'dir') toggle(root.key, entry.path)
    else if (isViewable(entry.name)) view(root, entry)
    else preview(root, entry)
  }

  const copyPath = async (root, path) => {
    try {
      await navigator.clipboard.writeText(absolutePath(root.path, path))
      toast({ title: 'Path copied' })
    } catch {
      toast({ title: 'Could not copy the path', description: absolutePath(root.path, path), tone: 'error' })
    }
  }

  const attachToModeling = (root, entry) => {
    navigate({ name: 'new-modeling', attachments: [{ type: 'file', root: root.key, path: entry.path, label: entry.name }] })
  }

  const openTerminal = async (root) => {
    setBottomTab('terminal')
    try {
      await useTerminals.getState().createTerminal({ project_id: projectId, root: root.key })
    } catch (err) {
      toast({ title: 'Could not open a terminal', description: err.message, tone: 'error' })
    }
  }

  const remove = async () => {
    if (!confirm) return
    const { root, entry } = confirm
    setDeleting(true)
    try {
      await api.deleteFile(projectId, root.key, entry.path)
      const inside = (path) => path === entry.path || (entry.type === 'dir' && path.startsWith(`${entry.path}/`))
      for (const file of useApp.getState().openFiles) {
        if (file.root === root.key && inside(file.path)) closeFile(file)
      }
      if (isViewable(entry.name)) showInViewer({ kind: 'remove', key: `file:${root.key}:${entry.path}` })
      if (entry.type === 'dir') {
        setExpanded((current) => new Map([...current].filter(([, node]) => !(node.root === root.key && inside(node.path)))))
      }
      setConfirm(null)
      loadDir(root.key, parentPath(entry.path), { quiet: true })
    } catch (err) {
      toast({ title: `Could not delete ${entry.name}`, description: err.message, tone: 'error' })
    } finally {
      setDeleting(false)
    }
  }

  const openMenu = (event, target) => {
    event.preventDefault()
    let { clientX: x, clientY: y } = event
    // Keyboard (Shift+F10, the menu key) and the "…" button: anchor under the element.
    if (event.type !== 'contextmenu' || (x === 0 && y === 0)) {
      const rect = event.currentTarget.getBoundingClientRect()
      x = rect.left
      y = rect.bottom
    }
    setMenu({ x, y, target })
  }

  // ---------------------------------------------------------------- rows
  const query = filter.trim().toLowerCase()
  const rows = useMemo(() => {
    if (!roots) return []
    const out = []
    const matches = (rootKey, entry) => {
      if (entry.name.toLowerCase().includes(query)) return true
      if (entry.type !== 'dir') return false
      const key = nodeKey(rootKey, entry.path)
      return expanded.has(key) && (listings[key]?.entries || []).some((child) => matches(rootKey, child))
    }
    const walk = (root, path, depth) => {
      const key = nodeKey(root.key, path)
      const listing = listings[key]
      if (!listing?.entries) {
        if (listing?.error) out.push({ type: 'error', key: `${key}#error`, depth, root, path, message: listing.error })
        else out.push({ type: 'loading', key: `${key}#loading`, depth })
        return
      }
      const entries = query ? listing.entries.filter((entry) => matches(root.key, entry)) : listing.entries
      if (!entries.length) {
        if (!query) out.push({ type: 'note', key: `${key}#empty`, depth, text: 'Empty folder' })
        return
      }
      const limit = limits[key] || PAGE
      for (const entry of entries.slice(0, limit)) {
        const childKey = nodeKey(root.key, entry.path)
        const open = entry.type === 'dir' && expanded.has(childKey)
        out.push({ type: 'entry', key: childKey, root, entry, depth, open })
        if (open) walk(root, entry.path, depth + 1)
      }
      if (entries.length > limit) out.push({ type: 'more', key: `${key}#more`, depth, folder: key, remaining: entries.length - limit })
      if (listing.truncated && !query) out.push({ type: 'note', key: `${key}#truncated`, depth, text: 'Only the first entries of this folder are listed.' })
    }
    for (const root of roots) {
      const key = nodeKey(root.key, '')
      const open = expanded.has(key)
      out.push({ type: 'root', key, root, open })
      if (open) walk(root, '', 1)
    }
    return out
  }, [roots, expanded, listings, limits, query])

  const onTreeKeyDown = (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    const buttons = [...(treeRef.current?.querySelectorAll('[data-row]') || [])]
    const index = buttons.indexOf(document.activeElement)
    const next = buttons[index + (event.key === 'ArrowDown' ? 1 : -1)]
    if (next) {
      event.preventDefault()
      next.focus()
    }
  }

  const target = menu?.target
  const targetEntry = target?.entry
  const targetRoot = target?.root

  return (
    <div
      className="relative flex h-full min-h-0 flex-col"
      onDragEnter={(event) => {
        if (event.dataTransfer?.types?.includes('Files')) {
          event.preventDefault()
          setDragging(true)
        }
      }}
      onDragOver={(event) => {
        if (event.dataTransfer?.types?.includes('Files')) event.preventDefault()
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false)
      }}
      onDrop={(event) => {
        if (!event.dataTransfer?.files?.length) return
        event.preventDefault()
        setDragging(false)
        upload(event.dataTransfer.files)
      }}
    >
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-2">
        <label className="flex h-7 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-sm hover:bg-subtle focus-within:bg-subtle focus-within:ring-1 focus-within:ring-line-strong">
          <Search size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setFilter('')
            }}
            placeholder="Filter open folders"
            aria-label="Filter open folders"
            className="min-w-0 flex-1 bg-transparent text-fg outline-none placeholder:text-faint"
          />
          {filter ? (
            <button type="button" aria-label="Clear filter" onClick={() => setFilter('')} className="text-faint hover:text-fg">
              <X size={13} />
            </button>
          ) : null}
        </label>
        {uploading ? (
          <span className="flex h-7 w-7 items-center justify-center">
            <Spinner />
          </span>
        ) : (
          <IconButton icon={Upload} label="Upload to the modeling workspace" size="sm" onClick={() => chooseUpload(UPLOAD_TARGET)} />
        )}
        <IconButton icon={RefreshCw} label="Refresh" size="sm" onClick={() => refresh()} />
      </div>

      <div ref={treeRef} role="tree" aria-label="Project files" onKeyDown={onTreeKeyDown} className="min-h-0 flex-1 overflow-y-auto px-1.5 py-1.5">
        {!roots && rootsError ? (
          <div className="p-2">
            <InlineError>{rootsError}</InlineError>
            <Button size="sm" variant="ghost" className="mt-2" icon={RefreshCw} onClick={() => reloadRoots().catch(() => {})}>
              Try again
            </Button>
          </div>
        ) : !roots ? (
          <LoadingRows depth={0} count={4} />
        ) : (
          rows.map((row, index) => {
            if (row.type === 'root') {
              return (
                <RootRow
                  key={row.key}
                  row={row}
                  first={index === 0}
                  onToggle={() => toggle(row.root.key, '')}
                  onRefresh={() => loadDir(row.root.key, '', { quiet: true })}
                  onMenu={(event) => openMenu(event, { root: row.root })}
                />
              )
            }
            if (row.type === 'entry') {
              return (
                <EntryRow
                  key={row.key}
                  row={row}
                  selected={workbenchTab === `file:${row.root.key}:${row.entry.path}`}
                  onActivate={() => activate(row.root, row.entry)}
                  onToggle={(open) => {
                    if (open !== row.open) toggle(row.root.key, row.entry.path)
                  }}
                  onMenu={(event) => openMenu(event, { root: row.root, entry: row.entry })}
                />
              )
            }
            if (row.type === 'more') {
              return (
                <button
                  key={row.key}
                  type="button"
                  onClick={() => setLimits((current) => ({ ...current, [row.folder]: (current[row.folder] || PAGE) + PAGE }))}
                  className="flex h-7 w-full items-center rounded-md text-left text-sm text-accent-text hover:bg-hover"
                  style={{ paddingLeft: indent(row.depth) + 20 }}
                >
                  Show {Math.min(row.remaining, PAGE)} more
                </button>
              )
            }
            if (row.type === 'loading') return <LoadingRows key={row.key} depth={row.depth} count={1} />
            if (row.type === 'error') {
              return (
                <div key={row.key} className="flex min-h-7 items-center gap-2 py-1 pr-2 text-sm" style={{ paddingLeft: indent(row.depth) + 20 }}>
                  <span className="min-w-0 flex-1 truncate text-danger" title={row.message}>
                    {row.message}
                  </span>
                  <Button size="xs" variant="ghost" onClick={() => loadDir(row.root.key, row.path)}>
                    Retry
                  </Button>
                </div>
              )
            }
            return (
              <p key={row.key} className="flex h-7 items-center text-sm text-faint" style={{ paddingLeft: indent(row.depth) + 20 }}>
                {row.text}
              </p>
            )
          })
        )}
        {roots && query && !rows.some((row) => row.type === 'entry') ? (
          <p className="px-2 py-3 text-sm text-muted">No file in the open folders matches “{filter.trim()}”.</p>
        ) : null}
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          upload(event.target.files, uploadTarget.current)
          event.target.value = ''
        }}
      />

      {dragging ? (
        <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-accent bg-accent-soft p-6 text-center text-sm font-medium text-accent-text">
          Drop files to upload them to data/ in the modeling workspace
        </div>
      ) : null}

      <Menu open={Boolean(menu)} onOpenChange={(open) => (open ? null : setMenu(null))} modal={false}>
        <MenuTrigger asChild>
          <span
            aria-hidden
            tabIndex={-1}
            style={{ position: 'fixed', left: menu?.x ?? 0, top: menu?.y ?? 0, width: 1, height: 1, pointerEvents: 'none' }}
          />
        </MenuTrigger>
        <MenuContent className="min-w-[220px]" onCloseAutoFocus={(event) => event.preventDefault()}>
          {targetRoot && !targetEntry ? (
            <>
              {targetRoot.writable ? (
                <MenuItem icon={Upload} onSelect={() => chooseUpload({ root: targetRoot.key, path: '' })}>
                  Upload files here
                </MenuItem>
              ) : null}
              <MenuItem icon={SquareTerminal} onSelect={() => openTerminal(targetRoot)}>
                Open a terminal here
              </MenuItem>
              <MenuItem icon={Copy} onSelect={() => copyPath(targetRoot, '')}>
                Copy path
              </MenuItem>
              <MenuItem icon={RefreshCw} onSelect={() => loadDir(targetRoot.key, '', { quiet: true })}>
                Refresh
              </MenuItem>
            </>
          ) : null}
          {targetEntry?.type === 'dir' ? (
            <>
              {targetRoot.writable ? (
                <MenuItem icon={Upload} onSelect={() => chooseUpload({ root: targetRoot.key, path: targetEntry.path })}>
                  Upload files here
                </MenuItem>
              ) : null}
              <MenuItem icon={Copy} onSelect={() => copyPath(targetRoot, targetEntry.path)}>
                Copy path
              </MenuItem>
              <MenuItem icon={RefreshCw} onSelect={() => reveal(targetRoot.key, targetEntry.path)}>
                Refresh
              </MenuItem>
              {targetRoot.writable ? (
                <>
                  <MenuSeparator />
                  <MenuItem icon={Trash2} danger onSelect={() => setConfirm({ root: targetRoot, entry: targetEntry })}>
                    Delete
                  </MenuItem>
                </>
              ) : null}
            </>
          ) : null}
          {targetEntry && targetEntry.type !== 'dir' ? (
            <>
              {isViewable(targetEntry.name) ? (
                <MenuItem icon={Eye} onSelect={() => view(targetRoot, targetEntry)}>
                  Open in viewer
                </MenuItem>
              ) : null}
              <MenuItem icon={FileText} onSelect={() => preview(targetRoot, targetEntry)}>
                Preview
              </MenuItem>
              {isModelingInput(targetEntry.name) ? (
                <MenuItem icon={Atom} onSelect={() => attachToModeling(targetRoot, targetEntry)}>
                  Use for modeling
                </MenuItem>
              ) : null}
              <MenuSeparator />
              <MenuItem icon={Copy} onSelect={() => copyPath(targetRoot, targetEntry.path)}>
                Copy path
              </MenuItem>
              <MenuItem icon={Download} onSelect={() => downloadUrl(api.fileUrl(projectId, targetRoot.key, targetEntry.path), targetEntry.name)}>
                Download
              </MenuItem>
              {targetRoot.writable ? (
                <>
                  <MenuSeparator />
                  <MenuItem icon={Trash2} danger onSelect={() => setConfirm({ root: targetRoot, entry: targetEntry })}>
                    Delete
                  </MenuItem>
                </>
              ) : null}
            </>
          ) : null}
        </MenuContent>
      </Menu>

      <ConfirmDialog
        open={Boolean(confirm)}
        onOpenChange={(open) => (open ? null : setConfirm(null))}
        title={`Delete ${confirm?.entry.name || 'this file'}?`}
        description={
          confirm?.entry.type === 'dir'
            ? 'The folder and everything in it are removed from the server. This cannot be undone.'
            : 'The file is removed from the server. This cannot be undone.'
        }
        busy={deleting}
        onConfirm={remove}
      />
    </div>
  )
}

const indent = (depth) => 6 + depth * 12

function RootRow({ row, first, onToggle, onRefresh, onMenu }) {
  const { root, open } = row
  return (
    <div className={cx('group flex h-8 items-center gap-0.5 rounded-lg pr-1 hover:bg-hover', !first && 'mt-1.5')} onContextMenu={onMenu}>
      <button
        type="button"
        data-row
        role="treeitem"
        aria-expanded={open}
        aria-level={1}
        onClick={onToggle}
        onKeyDown={(event) => {
          if ((event.key === 'ArrowRight' && !open) || (event.key === 'ArrowLeft' && open)) {
            event.preventDefault()
            onToggle()
          }
        }}
        title={root.path}
        className="flex h-full min-w-0 flex-1 items-center gap-1.5 pl-1.5 text-left text-sm font-medium text-fg"
      >
        <ChevronRight size={14} strokeWidth={1.75} className={cx('shrink-0 text-faint transition-transform duration-100', open && 'rotate-90')} />
        <span className="truncate">{root.label}</span>
        {!root.writable ? (
          <Tip label="Read-only">
            <span className="inline-flex shrink-0 text-faint" aria-label="Read-only">
              <Lock size={12} strokeWidth={1.75} />
            </span>
          </Tip>
        ) : null}
      </button>
      <span className="flex items-center opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
        <IconButton icon={RefreshCw} label="Refresh" size="xs" onClick={onRefresh} />
        <IconButton icon={MoreHorizontal} label="More actions" size="xs" onClick={onMenu} />
      </span>
    </div>
  )
}

function EntryRow({ row, selected, onActivate, onToggle, onMenu }) {
  const { entry, depth, open } = row
  const isDir = entry.type === 'dir'
  const Icon = isDir ? (open ? FolderOpen : Folder) : fileIcon(entry.name)
  const viewable = !isDir && isViewable(entry.name)
  return (
    <div
      className={cx('group flex h-7 items-center rounded-md pr-1', selected ? 'bg-hover' : 'hover:bg-hover')}
      style={{ paddingLeft: indent(depth) }}
      onContextMenu={onMenu}
    >
      <button
        type="button"
        data-row
        role="treeitem"
        aria-expanded={isDir ? open : undefined}
        aria-level={depth + 1}
        onClick={onActivate}
        onKeyDown={(event) => {
          if (!isDir) return
          if (event.key === 'ArrowRight' && !open) onToggle(true)
          if (event.key === 'ArrowLeft' && open) onToggle(false)
        }}
        title={[entry.path, entry.modified ? `Modified ${formatDateTime(entry.modified)}` : ''].filter(Boolean).join('\n')}
        className="flex h-full min-w-0 flex-1 items-center gap-1.5 text-left text-sm text-fg"
      >
        {isDir ? (
          <ChevronRight size={13} strokeWidth={1.75} className={cx('shrink-0 text-faint transition-transform duration-100', open && 'rotate-90')} />
        ) : (
          <span className="w-[13px] shrink-0" />
        )}
        <Icon size={15} strokeWidth={1.75} className={cx('shrink-0', viewable ? 'text-accent-text' : 'text-muted')} />
        <span className="truncate">{entry.name}</span>
      </button>
      {!isDir && entry.size !== undefined && entry.size !== null ? (
        <span className="shrink-0 pl-2 pr-1 text-xs tabular-nums text-faint group-hover:hidden group-focus-within:hidden">{formatBytes(entry.size)}</span>
      ) : null}
      <button
        type="button"
        aria-label={`More actions for ${entry.name}`}
        onClick={onMenu}
        className="hidden h-6 w-6 shrink-0 items-center justify-center rounded-md text-faint hover:bg-line hover:text-fg group-hover:flex group-focus-within:flex"
      >
        <MoreHorizontal size={14} />
      </button>
    </div>
  )
}

function LoadingRows({ depth, count }) {
  return Array.from({ length: count }, (_, index) => (
    <div key={index} className="flex h-7 items-center gap-2" style={{ paddingLeft: indent(depth) + 20 }}>
      <Skeleton className="h-3.5 w-3.5" />
      <Skeleton className="h-3 w-32" />
    </div>
  ))
}
