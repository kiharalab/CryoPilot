// The "+" menu in modeling composers: maps the project's reconstruction
// sessions produced in CryoSPARC, models, maps, and sequences already in the
// project, and uploads from this computer. Attachments are plain objects the
// server understands ({type:'cryosparc_map', session_id, job_uid, name} or
// {type:'file', root, path}) plus a `label` for the composer chip.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Atom, Check, Dna, FileText, Layers, LoaderCircle, Plus, Search, Upload } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { basename, formatBytes, formatResolution, pluralize } from '../../lib/format'
import { useApp } from '../../store/app'
import { IconButton, Popover, PopoverContent, PopoverTrigger, Skeleton, Switch } from '../../components/ui'

const STRUCTURE_SUFFIXES = ['.pdb', '.cif', '.mmcif', '.ent', '.pdb.gz', '.cif.gz']
const VOLUME_SUFFIXES = ['.mrc', '.map', '.ccp4', '.mrc.gz', '.map.gz']
const SEQUENCE_SUFFIXES = ['.fasta', '.fa', '.faa', '.fas', '.fna', '.seq']

// Walking data/ stops here so a workspace full of agent outputs stays quick.
const WALK_MAX_DEPTH = 3
const WALK_MAX_DIRS = 40
const WALK_MAX_FILES = 400
const RENDER_CAP = 100

/** 'structure' | 'volume' | 'sequence' | 'other', from the file name. */
export function fileKind(path = '') {
  const lower = String(path).toLowerCase()
  if (STRUCTURE_SUFFIXES.some((suffix) => lower.endsWith(suffix))) return 'structure'
  if (VOLUME_SUFFIXES.some((suffix) => lower.endsWith(suffix))) return 'volume'
  if (SEQUENCE_SUFFIXES.some((suffix) => lower.endsWith(suffix))) return 'sequence'
  return 'other'
}

const KIND_ICONS = { structure: Atom, volume: Layers, sequence: Dna, other: FileText }
const KIND_NAMES = { structure: 'Model', volume: 'Density map', sequence: 'Sequence', other: 'File' }
const ROOT_NAMES = { modeling: 'Modeling workspace', files: 'Uploads', reconstruction: 'Reconstruction runs' }

/** Stable identity of an attachment; also the composer chip key. */
export function attachmentKey(attachment) {
  if (attachment.type === 'cryosparc_map') return `map:${attachment.session_id}:${attachment.job_uid}:${attachment.name}`
  return `file:${attachment.root}:${attachment.path}`
}

/** Composer chip for an attachment: {key, label, icon, title}. */
export function attachmentChip(attachment) {
  if (attachment.type === 'cryosparc_map') {
    const label = attachment.label || `${attachment.job_uid} ${attachment.name}`
    return { key: attachmentKey(attachment), label, icon: Layers, title: `${label}, from CryoSPARC` }
  }
  const kind = fileKind(attachment.path)
  return {
    key: attachmentKey(attachment),
    label: attachment.label || basename(attachment.path),
    icon: KIND_ICONS[kind],
    title: `${KIND_NAMES[kind]}: ${attachment.path} (${ROOT_NAMES[attachment.root] || attachment.root})`,
  }
}

/** Adds attachments that are not there yet, keeping the order; returns `current` when nothing is new. */
export function mergeAttachments(current, added) {
  const seen = new Set(current.map(attachmentKey))
  const fresh = []
  for (const attachment of added || []) {
    const key = attachmentKey(attachment)
    if (!seen.has(key)) {
      seen.add(key)
      fresh.push(attachment)
    }
  }
  return fresh.length ? [...current, ...fresh] : current
}

/** Uploads one file into the modeling workspace's data/ folder and returns its attachment. */
export async function uploadToModeling(projectId, file) {
  const uploaded = await api.uploadFile(projectId, 'modeling', 'data', file, { onConflict: 'rename' })
  return { type: 'file', root: 'modeling', path: uploaded.path, label: uploaded.name || basename(uploaded.path) }
}

/** The attachment for a map from api.projectMaps or api.cryosparcJobs. */
export function mapAttachment(volume) {
  return {
    type: 'cryosparc_map',
    session_id: volume.session_id,
    job_uid: volume.job_uid,
    name: volume.name,
    label: `${volume.job_uid} ${volume.label || volume.name}`,
  }
}

async function walkModelingData(projectId) {
  const found = []
  const queue = [{ path: 'data', depth: 0 }]
  let visited = 0
  while (queue.length && visited < WALK_MAX_DIRS && found.length < WALK_MAX_FILES) {
    const { path, depth } = queue.shift()
    visited += 1
    let listing
    try {
      listing = await api.listDir(projectId, 'modeling', path)
    } catch (err) {
      if (depth === 0 && err.status !== 404) throw err
      continue
    }
    for (const entry of listing.entries || []) {
      if (entry.type === 'dir') {
        if (depth + 1 <= WALK_MAX_DEPTH) queue.push({ path: entry.path, depth: depth + 1 })
      } else if (fileKind(entry.name) !== 'other') {
        found.push({ ...entry, root: 'modeling' })
      }
    }
  }
  return found
}

async function loadProjectFiles(projectId) {
  const [modeling, uploads] = await Promise.allSettled([
    walkModelingData(projectId),
    api.listDir(projectId, 'files', '').then((listing) =>
      (listing.entries || []).filter((entry) => entry.type !== 'dir' && fileKind(entry.name) !== 'other').map((entry) => ({ ...entry, root: 'files' })),
    ),
  ])
  if (modeling.status === 'rejected' && uploads.status === 'rejected') throw modeling.reason
  const files = [...(modeling.value || []), ...(uploads.value || [])]
  files.sort((a, b) => (b.modified || 0) - (a.modified || 0))
  return files
}

function matches(query, ...parts) {
  if (!query) return true
  const haystack = parts.filter(Boolean).join(' ').toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word))
}

/**
 * Props:
 *   projectId
 *   onAttach(attachment)     called once per chosen map, file, or finished upload
 *   disabled
 *   attachedKeys             optional Set or array of attachmentKey()s already in the composer
 */
export function AttachMenu({ projectId, onAttach, disabled = false, attachedKeys }) {
  const toast = useApp((state) => state.toast)
  const openSettings = useApp((state) => state.openSettings)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [showAll, setShowAll] = useState(false)
  // `errors` lists the sessions whose jobs could not be read; the request itself still succeeds.
  const [maps, setMaps] = useState({ loading: false, items: [], errors: [], error: '' })
  const [files, setFiles] = useState({ loading: false, items: [], error: '' })
  const [active, setActive] = useState(0)
  const [uploading, setUploading] = useState(0)
  const fileInputRef = useRef(null)
  const listRef = useRef(null)
  const listId = useId()

  const attached = useMemo(() => new Set(attachedKeys || []), [attachedKeys])

  // Maps are fetched on every open, so a refinement that just finished shows up.
  useEffect(() => {
    if (!open || !projectId) return undefined
    let cancelled = false
    setMaps((current) => ({ ...current, loading: true, error: '' }))
    api
      .projectMaps(projectId, { all: showAll })
      .then((data) => !cancelled && setMaps({ loading: false, items: data.maps || [], errors: data.errors || [], error: '' }))
      .catch((err) => !cancelled && setMaps({ loading: false, items: [], errors: [], error: err.message }))
    return () => {
      cancelled = true
    }
  }, [open, projectId, showAll])

  useEffect(() => {
    if (!open || !projectId) return undefined
    let cancelled = false
    setFiles((current) => ({ ...current, loading: true, error: '' }))
    loadProjectFiles(projectId)
      .then((items) => !cancelled && setFiles({ loading: false, items, error: '' }))
      .catch((err) => !cancelled && setFiles({ loading: false, items: [], error: err.message }))
    return () => {
      cancelled = true
    }
  }, [open, projectId])

  useEffect(() => {
    if (!open) setQuery('')
    setActive(0)
  }, [open])

  // A map whose file is gone from the project folder cannot be attached.
  const availableMaps = useMemo(() => maps.items.filter((volume) => volume.available !== false), [maps.items])
  const visibleMaps = useMemo(
    () => availableMaps.filter((volume) => matches(query, volume.job_uid, volume.job_title, volume.job_type, volume.label, volume.session_title)),
    [availableMaps, query],
  )
  const visibleFiles = useMemo(() => files.items.filter((file) => matches(query, file.path, KIND_NAMES[fileKind(file.name)])), [files.items, query])
  const sessionCount = useMemo(() => new Set(availableMaps.map((volume) => volume.session_id)).size, [availableMaps])

  // One flat list for arrow keys: upload, then maps, then files.
  const options = useMemo(() => {
    const list = [{ id: 'upload', type: 'upload' }]
    for (const volume of visibleMaps.slice(0, RENDER_CAP)) {
      const attachment = mapAttachment(volume)
      list.push({ id: attachmentKey(attachment), type: 'map', volume, attachment })
    }
    for (const file of visibleFiles.slice(0, RENDER_CAP)) {
      const attachment = { type: 'file', root: file.root, path: file.path, label: file.name }
      list.push({ id: attachmentKey(attachment), type: 'file', file, attachment })
    }
    return list
  }, [visibleMaps, visibleFiles])

  // While searching, Enter picks the first match rather than the upload row.
  useEffect(() => {
    setActive(query && options.length > 1 ? 1 : 0)
  }, [query, options.length])

  useEffect(() => {
    if (!open) return
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  const upload = useCallback(
    async (fileList) => {
      const chosen = Array.from(fileList || [])
      for (const file of chosen) {
        setUploading((count) => count + 1)
        try {
          onAttach?.(await uploadToModeling(projectId, file))
        } catch (err) {
          toast({ title: `Could not upload ${file.name}`, description: err.message, tone: 'error' })
        } finally {
          setUploading((count) => count - 1)
        }
      }
    },
    [onAttach, projectId, toast],
  )

  const choose = (option) => {
    if (!option) return
    if (option.type === 'upload') {
      fileInputRef.current?.click()
      setOpen(false)
      return
    }
    if (!attached.has(option.id)) onAttach?.(option.attachment)
    setOpen(false)
  }

  const onKeyDown = (event) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((current) => (current + 1) % options.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((current) => (current - 1 + options.length) % options.length)
    } else if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
      event.preventDefault()
      choose(options[active])
    }
  }

  const openAccounts = () => {
    setOpen(false)
    openSettings('accounts')
  }

  const optionId = (index) => `${listId}-option-${index}`
  // Row positions in `options`: the upload row is 0, maps follow, then files.
  const mapsShown = Math.min(visibleMaps.length, RENDER_CAP)
  const mapOffset = 1
  const fileOffset = 1 + mapsShown

  return (
    <>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <IconButton
            icon={uploading ? Uploading : Plus}
            label={uploading ? 'Uploading' : 'Attach maps or files'}
            tooltip={false}
            disabled={disabled || !projectId}
            active={open}
          />
        </PopoverTrigger>
        <PopoverContent
          align="start"
          side="top"
          sideOffset={8}
          className="flex max-h-[min(480px,70vh)] w-[400px] max-w-[calc(100vw-24px)] flex-col overflow-hidden"
          onOpenAutoFocus={(event) => {
            event.preventDefault()
            event.currentTarget.querySelector('input')?.focus()
          }}
        >
          <div className="flex h-10 shrink-0 items-center gap-2 border-b border-line px-3">
            <Search size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Search maps and files"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={options[active] ? optionId(active) : undefined}
              aria-label="Search maps and files"
              className="h-full min-w-0 flex-1 bg-transparent text-sm text-fg placeholder:text-faint focus:outline-none"
            />
          </div>
          <div ref={listRef} id={listId} role="listbox" aria-label="Attachments" className="min-h-0 flex-1 overflow-y-auto p-1">
            <Option id={optionId(0)} index={0} active={active === 0} onHover={setActive} onChoose={() => choose(options[0])}>
              <Upload size={15} strokeWidth={1.75} className="shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate text-sm text-fg">Upload from this computer</span>
            </Option>

            <GroupHeader
              trailing={
                <label className="flex items-center gap-2 text-xs text-muted">
                  All outputs
                  <Switch checked={showAll} onCheckedChange={setShowAll} label="Show half maps and masks too" />
                </label>
              }
            >
              Maps from reconstruction
            </GroupHeader>
            {maps.loading && !maps.items.length ? (
              <LoadingRows />
            ) : maps.error ? (
              <GroupNote>Maps could not be loaded. {maps.error}</GroupNote>
            ) : !availableMaps.length ? (
              maps.errors.length ? (
                <MapsErrorNote errors={maps.errors} onOpenSettings={openAccounts} />
              ) : (
                <GroupNote>Maps appear here when a reconstruction session finishes a refinement in CryoSPARC.</GroupNote>
              )
            ) : !visibleMaps.length ? (
              <GroupNote>No maps match.</GroupNote>
            ) : (
              <>
                {visibleMaps.slice(0, RENDER_CAP).map((volume, position) => {
                  const optionIndex = mapOffset + position
                  const option = options[optionIndex]
                  return (
                    <Option
                      key={option.id}
                      id={optionId(optionIndex)}
                      index={optionIndex}
                      active={active === optionIndex}
                      selected={attached.has(option.id)}
                      onHover={setActive}
                      onChoose={() => choose(option)}
                      tall
                    >
                      <Layers size={15} strokeWidth={1.75} className="mt-0.5 shrink-0 self-start text-muted" />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-baseline gap-2">
                          <span className="min-w-0 flex-1 truncate text-sm text-fg">{volume.label || volume.name}</span>
                          {volume.resolution_A ? (
                            <span className="shrink-0 text-xs tabular-nums text-muted">{formatResolution(volume.resolution_A)}</span>
                          ) : null}
                        </span>
                        <span className="flex items-baseline gap-1.5 text-xs text-muted">
                          <span className="shrink-0 font-mono text-[11.5px] text-faint">{volume.job_uid}</span>
                          <span className="min-w-0 truncate">{volume.job_title || volume.job_type}</span>
                          {sessionCount > 1 && volume.session_title ? (
                            <span className="ml-auto max-w-[45%] shrink-0 truncate text-faint" title={volume.session_title}>
                              {volume.session_title}
                            </span>
                          ) : null}
                        </span>
                      </span>
                      {attached.has(option.id) ? <Check size={14} className="shrink-0 text-accent" /> : null}
                    </Option>
                  )
                })}
                {visibleMaps.length > RENDER_CAP ? <GroupNote>{pluralize(visibleMaps.length - RENDER_CAP, 'more map')}; search to narrow the list.</GroupNote> : null}
                {maps.errors.length ? <MapsErrorNote errors={maps.errors} onOpenSettings={openAccounts} /> : null}
              </>
            )}

            <GroupHeader>Project files</GroupHeader>
            {files.loading && !files.items.length ? (
              <LoadingRows />
            ) : files.error ? (
              <GroupNote>Files could not be listed. {files.error}</GroupNote>
            ) : !files.items.length ? (
              <GroupNote>Models, maps, and sequences in the modeling workspace data folder appear here.</GroupNote>
            ) : !visibleFiles.length ? (
              <GroupNote>No files match.</GroupNote>
            ) : (
              <>
                {visibleFiles.slice(0, RENDER_CAP).map((file, position) => {
                  const optionIndex = fileOffset + position
                  const option = options[optionIndex]
                  const Icon = KIND_ICONS[fileKind(file.name)]
                  const folder = file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : ''
                  return (
                    <Option
                      key={option.id}
                      id={optionId(optionIndex)}
                      index={optionIndex}
                      active={active === optionIndex}
                      selected={attached.has(option.id)}
                      onHover={setActive}
                      onChoose={() => choose(option)}
                      title={`${ROOT_NAMES[file.root] || file.root}: ${file.path}`}
                    >
                      <Icon size={15} strokeWidth={1.75} className="shrink-0 text-muted" />
                      <span className="min-w-0 flex-1 truncate text-sm text-fg">{file.name}</span>
                      <span className="min-w-0 max-w-[40%] shrink truncate text-xs text-faint">{file.root === 'files' ? 'Uploads' : folder}</span>
                      <span className="shrink-0 text-xs tabular-nums text-faint">{formatBytes(file.size)}</span>
                      {attached.has(option.id) ? <Check size={14} className="shrink-0 text-accent" /> : null}
                    </Option>
                  )
                })}
                {visibleFiles.length > RENDER_CAP ? <GroupNote>{pluralize(visibleFiles.length - RENDER_CAP, 'more file')}; search to narrow the list.</GroupNote> : null}
              </>
            )}
          </div>
        </PopoverContent>
      </Popover>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        tabIndex={-1}
        aria-hidden
        className="hidden"
        onChange={(event) => {
          const chosen = Array.from(event.target.files || [])
          event.target.value = ''
          upload(chosen)
        }}
      />
    </>
  )
}

function Uploading(props) {
  return <LoaderCircle {...props} className={cx(props.className, 'animate-spin')} />
}

function Option({ id, index, active, selected, onHover, onChoose, tall = false, title, children }) {
  return (
    <div
      id={id}
      role="option"
      aria-selected={active}
      aria-checked={selected || undefined}
      data-index={index}
      title={title}
      onMouseMove={() => !active && onHover(index)}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onChoose}
      className={cx(
        'flex cursor-default select-none items-center gap-2.5 rounded-lg px-2.5',
        tall ? 'py-1.5' : 'h-8',
        active ? 'bg-hover' : '',
      )}
    >
      {children}
    </div>
  )
}

function GroupHeader({ children, trailing }) {
  return (
    <div className="mt-1 flex h-8 items-center gap-2 border-t border-line px-2.5 pt-1">
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-faint">{children}</span>
      {trailing}
    </div>
  )
}

function GroupNote({ children }) {
  return <p className="px-2.5 pb-2 pt-1 text-xs leading-relaxed text-muted">{children}</p>
}

/** Why some sessions' maps are missing, with a way to sign in when that would help. */
function MapsErrorNote({ errors, onOpenSettings }) {
  const signedIn = useApp((state) => Boolean(state.setup?.cryosparc?.email && state.setup?.cryosparc?.password_set))
  const signInHelps = errors.some((error) => error.setup === 'cryosparc') || !signedIn
  const others = errors.length - 1
  return (
    <GroupNote>
      {errors[0].message}
      {others ? ` ${pluralize(others, 'other session')} could not be read either.` : ''}
      {signInHelps ? (
        <button type="button" onClick={onOpenSettings} className="ml-1.5 font-medium text-accent-text hover:underline">
          Open settings
        </button>
      ) : null}
    </GroupNote>
  )
}

function LoadingRows() {
  return (
    <div className="flex flex-col gap-2 px-2.5 pb-2 pt-1">
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
    </div>
  )
}
