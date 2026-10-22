// CryoSPARC jobs and maps of a reconstruction session, read live from
// CryoSPARC (or from the project folder when not signed in). A map opens in
// the viewer or starts a modeling session; a job's images (FSC curves, class
// averages) open in a lightbox.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Atom,
  ChevronRight,
  ChevronsUpDown,
  Copy,
  Cuboid,
  Download,
  ExternalLink,
  Eye,
  Info,
  Layers,
  Maximize2,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Star,
  X,
} from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatBytes, formatRelative, formatResolution, toDate } from '../../lib/format'
import { useApp, useCurrentSession } from '../../store/app'
import {
  Button,
  EmptyState,
  IconButton,
  InlineError,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  Segmented,
  Skeleton,
  Spinner,
  StatusDot,
  Tip,
} from '../../components/ui'
import { Lightbox } from '../chat/ImageGrid'
import { downloadUrl } from '../files/fileTypes'

const POLL_MS = 15000
const JOB_PAGE = 150
const IMAGE_PAGE = 24
const PRIMARY_KINDS = new Set(['map_sharp', 'map'])

// Last answer per session, so switching sessions shows the list at once.
const jobCache = new Map()
// Image assets per job, status, and CryoSPARC account; a job's images change only when it finishes.
const assetCache = new Map()

/** CryoSPARC job states that StatusDot does not know by name. */
const DOT_STATES = { started: 'running', building: 'queued' }
const STATUS_LABELS = {
  building: 'Building',
  queued: 'Queued',
  launched: 'Launched',
  started: 'Running',
  running: 'Running',
  waiting: 'Waiting',
  completed: 'Completed',
  failed: 'Failed',
  killed: 'Killed',
}

function statusLabel(status) {
  return STATUS_LABELS[status] || (status ? status.charAt(0).toUpperCase() + status.slice(1) : 'Unknown')
}

function jobNumber(uid) {
  return Number.parseInt(String(uid || '').replace(/\D/g, ''), 10) || 0
}

function jobType(job) {
  return job.type_title || job.type || ''
}

/** CryoSPARC titles new jobs "New Job"; the job type says more. */
function jobName(job) {
  const title = (job.title || '').trim()
  return title && !/^new job$/i.test(title) ? title : jobType(job) || job.uid
}

function sessionTime(session) {
  return toDate(session.updated_at || session.created_at)?.getTime() ?? 0
}

function boundProject(session) {
  return session.config?.project_uid || session.harness?.project_uid || null
}

function volumeLabel(volume) {
  return volume.label || volume.name
}

/** "320³ at 1.245 Å/px, B-factor −85.2 Å², 125 MB" */
function volumeDetail(volume) {
  const parts = []
  const shape = Array.isArray(volume.shape) ? volume.shape.filter(Boolean) : []
  const box = shape.length === 3 && shape.every((value) => value === shape[0]) ? `${shape[0]}³` : shape.length ? shape.join(' × ') : ''
  const psize = volume.psize_A ? `${Number(Number(volume.psize_A).toFixed(3))} Å/px` : ''
  if (box && psize) parts.push(`${box} at ${psize}`)
  else if (box || psize) parts.push(box || psize)
  if (volume.b_factor !== null && volume.b_factor !== undefined && Number.isFinite(Number(volume.b_factor))) {
    parts.push(`B-factor ${Number(volume.b_factor).toFixed(1).replace('-', '−')} Å²`)
  }
  if (volume.size_bytes) parts.push(formatBytes(volume.size_bytes))
  return parts.join(', ')
}

/** Paths from the server are relative to the CryoSPARC project folder. */
function volumePath(volume, projectDir) {
  if (!volume.path) return ''
  if (volume.path.startsWith('/') || !projectDir) return volume.path
  return `${projectDir.replace(/\/+$/, '')}/${volume.path}`
}

const KIND_NAMES = { half_map: 'half maps', mask: 'masks', map: 'maps', map_sharp: 'sharpened maps', other: 'other outputs' }

/** "Show 4 more: half maps and masks" */
function moreOutputsLabel(volumes) {
  const names = [...new Set(volumes.map((volume) => KIND_NAMES[volume.kind] || KIND_NAMES.other))]
  const list = names.length < 3 ? names.join(' and ') : `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`
  return `Show ${volumes.length} more: ${list}`
}

export function MapsPanel({ active }) {
  const project = useApp((state) => state.project)
  const workbenchOpen = useApp((state) => state.workbenchOpen)
  const setup = useApp((state) => state.setup)
  const navigate = useApp((state) => state.navigate)
  const current = useCurrentSession()
  const [pickedId, setPickedId] = useState(null)

  const sessions = useMemo(
    () => (project?.sessions || []).filter((session) => session.kind === 'reconstruction').sort((a, b) => sessionTime(b) - sessionTime(a)),
    [project?.sessions],
  )

  // Follow the conversation: opening a reconstruction session shows its maps.
  useEffect(() => {
    if (current?.kind === 'reconstruction') setPickedId(current.id)
  }, [current?.id, current?.kind])

  if (!project) {
    return (
      <EmptyState icon={Layers} title="No project open">
        Choose or create a project in the sidebar to see its maps.
      </EmptyState>
    )
  }

  if (!sessions.length) {
    return (
      <EmptyState
        icon={Layers}
        title="No reconstructions in this project"
        action={
          setup?.platform?.reconstruction_supported ? (
            <Button size="sm" icon={Plus} onClick={() => navigate({ name: 'new-reconstruction' })}>
              New reconstruction
            </Button>
          ) : null
        }
      >
        Maps from CryoSPARC appear here once a reconstruction session runs in this project. Sessions started from the terminal can be
        imported from the project menu in the sidebar.
      </EmptyState>
    )
  }

  const selected =
    sessions.find((session) => session.id === pickedId) ||
    (current?.kind === 'reconstruction' ? sessions.find((session) => session.id === current.id) : null) ||
    sessions[0]

  return <SessionMaps key={selected.id} session={selected} sessions={sessions} onPick={setPickedId} shown={active && workbenchOpen} />
}

function SessionMaps({ session, sessions, onPick, shown }) {
  const openSettings = useApp((state) => state.openSettings)
  const running = Boolean(session.runtime?.running)

  const [data, setData] = useState(() => jobCache.get(session.id)?.data || null)
  const [error, setError] = useState(null) // {message, setup}
  const [loading, setLoading] = useState(false)
  const [onlyMaps, setOnlyMaps] = useState(true)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [limit, setLimit] = useState(JOB_PAGE)
  const [gallery, setGallery] = useState([]) // [{src, caption, description}]
  const [galleryIndex, setGalleryIndex] = useState(-1)
  const requestRef = useRef(0)
  const inFlightRef = useRef(false)
  // The server knows the binding best; the session object fills in before the first answer.
  const projectUid = data ? data.project_uid : boundProject(session)
  const workspaceUid = data ? data.workspace_uid : session.config?.workspace_uid || session.harness?.workspace_uid || null
  const sessionProjectUid = boundProject(session)

  const load = useCallback(
    async ({ quiet = false } = {}) => {
      const request = ++requestRef.current
      inFlightRef.current = true
      if (!quiet) setLoading(true)
      try {
        const result = await api.cryosparcJobs(session.id)
        if (request !== requestRef.current) return
        jobCache.set(session.id, { data: result, at: Date.now() })
        setData(result)
        setError(null)
      } catch (err) {
        if (request !== requestRef.current) return
        setError({ message: err.message, setup: err.setup })
      } finally {
        if (request === requestRef.current) {
          setLoading(false)
          inFlightRef.current = false
        }
      }
    },
    [session.id],
  )

  // Load when first shown and when the session binds its project; poll while
  // the agent runs so new jobs and maps appear on their own.
  useEffect(() => {
    if (!shown) return undefined
    const cached = jobCache.get(session.id)
    const stale = !cached || Date.now() - cached.at > POLL_MS || (sessionProjectUid && !cached.data?.project_uid)
    if (stale) load({ quiet: Boolean(cached) })
    if (!running) return undefined
    const timer = setInterval(() => {
      // A slow CryoSPARC answer must not pile up requests behind it.
      if (document.visibilityState === 'visible' && !inFlightRef.current) load({ quiet: true })
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [shown, sessionProjectUid, running, session.id, load])

  const allJobs = useMemo(() => [...(data?.jobs || [])].sort((a, b) => jobNumber(b.uid) - jobNumber(a.uid)), [data])
  const withMaps = useMemo(() => allJobs.filter((job) => job.volumes?.length), [allJobs])
  const search = query.trim().toLowerCase()
  const jobs = useMemo(() => {
    const pool = onlyMaps ? withMaps : allJobs
    if (!search) return pool
    return pool.filter((job) => [job.uid, job.title, job.type, job.type_title].some((value) => String(value || '').toLowerCase().includes(search)))
  }, [allJobs, withMaps, onlyMaps, search])

  const toggle = (uid) =>
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })

  let body
  if (!data && !error) {
    body = <LoadingJobs />
  } else if (data && !data.project_uid) {
    body = (
      <EmptyState icon={Layers} title="No CryoSPARC project yet">
        The reconstruction agent creates its CryoSPARC project when it starts. Jobs and maps appear here as they run.
      </EmptyState>
    )
  } else if (!data && error) {
    body = (
      <div className="p-3">
        <InlineError>{error.message}</InlineError>
        <div className="mt-2 flex gap-2">
          <Button size="sm" variant="ghost" icon={RefreshCw} onClick={() => load()}>
            Try again
          </Button>
          {error.setup ? (
            <Button size="sm" onClick={() => openSettings('accounts')}>
              Open settings
            </Button>
          ) : null}
        </div>
      </div>
    )
  } else if (!allJobs.length) {
    body = (
      <EmptyState icon={Layers} title="No jobs yet">
        Jobs appear here as the reconstruction agent queues them in CryoSPARC.
      </EmptyState>
    )
  } else if (!jobs.length) {
    body = search ? (
      <p className="px-3 py-4 text-sm text-muted">No job matches “{query.trim()}”.</p>
    ) : (
      <EmptyState
        icon={Cuboid}
        title="No maps yet"
        action={
          <Button size="sm" onClick={() => setOnlyMaps(false)}>
            Show all jobs
          </Button>
        }
      >
        Refinements and reconstructions add their maps here when they finish.
      </EmptyState>
    )
  } else {
    body = (
      <div className="px-1.5 pb-3">
        {jobs.slice(0, limit).map((job) => (
          <JobRow key={job.uid} job={job} open={expanded.has(job.uid)} onToggle={() => toggle(job.uid)}>
            <JobDetail
              session={session}
              projectUid={projectUid}
              projectDir={data.project_dir}
              job={job}
              onOpenImages={(images, index) => {
                setGallery(images)
                setGalleryIndex(index)
              }}
            />
          </JobRow>
        ))}
        {jobs.length > limit ? (
          <button
            type="button"
            onClick={() => setLimit((value) => value + JOB_PAGE)}
            className="mt-1 flex h-8 w-full items-center rounded-lg px-3 text-left text-sm text-accent-text hover:bg-hover"
          >
            Show {Math.min(JOB_PAGE, jobs.length - limit)} more jobs
          </button>
        ) : null}
      </div>
    )
  }

  return (
    <div className="@container flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-2">
        <SessionPicker session={session} sessions={sessions} projectUid={projectUid} workspaceUid={workspaceUid} onPick={onPick} />
        {data?.cryosparc_url ? (
          <IconButton
            icon={ExternalLink}
            label="Open CryoSPARC"
            size="sm"
            onClick={() => window.open(data.cryosparc_url, '_blank', 'noopener,noreferrer')}
          />
        ) : null}
        {loading ? (
          <span className="flex h-7 w-7 items-center justify-center">
            <Spinner />
          </span>
        ) : (
          <IconButton icon={RefreshCw} label="Refresh" size="sm" onClick={() => load()} />
        )}
      </div>

      {projectUid && data ? (
        <div className="flex shrink-0 items-center gap-2 px-2 pb-1 pt-2">
          <label className="flex h-7 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-sm hover:bg-subtle focus-within:bg-subtle focus-within:ring-1 focus-within:ring-line-strong">
            <Search size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setQuery('')
              }}
              placeholder="Search jobs"
              aria-label="Search jobs"
              className="min-w-0 flex-1 bg-transparent text-fg outline-none placeholder:text-faint"
            />
            {query ? (
              <button type="button" aria-label="Clear search" onClick={() => setQuery('')} className="text-faint hover:text-fg">
                <X size={13} />
              </button>
            ) : null}
          </label>
          <Segmented
            size="sm"
            ariaLabel="Which jobs to list"
            value={onlyMaps ? 'maps' : 'all'}
            onChange={(value) => setOnlyMaps(value === 'maps')}
            options={[
              { value: 'maps', label: 'With maps' },
              { value: 'all', label: 'All jobs' },
            ]}
          />
        </div>
      ) : null}

      {projectUid && data?.source === 'filesystem' ? (
        <div className="mx-2 mt-1 flex shrink-0 items-start gap-2 rounded-lg bg-subtle px-3 py-2 text-xs text-muted">
          <Info size={13} strokeWidth={1.75} className="mt-px shrink-0" />
          <span className="min-w-0 flex-1">{data.warning || 'Read from the project folder. Sign in to CryoSPARC for live status.'}</span>
          {data.warning ? null : (
            <button type="button" onClick={() => openSettings('accounts')} className="shrink-0 font-medium text-accent-text hover:underline">
              Sign in
            </button>
          )}
        </div>
      ) : null}

      {data && error ? <p className="mx-3 mt-2 shrink-0 text-xs text-danger">Could not refresh: {error.message}</p> : null}

      <div className="min-h-0 flex-1 overflow-y-auto pt-1">{body}</div>

      <Lightbox images={gallery} index={galleryIndex} onIndexChange={setGalleryIndex} />
    </div>
  )
}

function SessionPicker({ session, sessions, projectUid, workspaceUid, onPick }) {
  const running = Boolean(session.runtime?.running)
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left text-sm hover:bg-hover data-[state=open]:bg-hover"
        >
          {running ? <StatusDot state="running" /> : null}
          <span className="min-w-0 truncate font-medium text-fg">{session.title}</span>
          {projectUid ? (
            <span className="shrink-0 font-mono text-xs text-faint">
              {projectUid}
              {workspaceUid ? ` ${workspaceUid}` : ''}
            </span>
          ) : null}
          <ChevronsUpDown size={14} strokeWidth={1.75} className="ml-auto shrink-0 text-faint" />
        </button>
      </MenuTrigger>
      <MenuContent className="w-[300px]">
        <div className="max-h-[340px] overflow-y-auto">
          {sessions.map((entry) => (
            <MenuItem key={entry.id} onSelect={() => onPick(entry.id)} checked={entry.id === session.id} hint={boundProject(entry) || undefined}>
              {entry.title}
            </MenuItem>
          ))}
        </div>
      </MenuContent>
    </Menu>
  )
}

function JobRow({ job, open, onToggle, children }) {
  const completed = job.status === 'completed'
  const failed = job.status === 'failed' || job.status === 'killed'
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        title={jobType(job) && jobType(job) !== jobName(job) ? jobType(job) : undefined}
        className="flex h-9 w-full items-center gap-2 rounded-lg px-2 text-left text-sm hover:bg-hover"
      >
        <ChevronRight size={14} strokeWidth={1.75} className={cx('shrink-0 text-faint transition-transform duration-100', open && 'rotate-90')} />
        <StatusDot state={DOT_STATES[job.status] || job.status} />
        <span className="w-10 shrink-0 font-mono text-xs text-muted">{job.uid}</span>
        <span className="min-w-0 flex-1 truncate text-fg">{jobName(job)}</span>
        {job.is_final_result ? (
          <span className="inline-flex shrink-0 text-warning" aria-label="Marked as a final result" title="Marked as a final result">
            <Star size={12} strokeWidth={1.75} className="fill-current" />
          </span>
        ) : null}
        {job.resolution_A ? <span className="shrink-0 text-xs tabular-nums text-fg">{formatResolution(job.resolution_A)}</span> : null}
        <span
          className={cx(
            'w-[72px] shrink-0 truncate text-right text-xs',
            completed ? 'text-faint' : failed ? 'text-danger' : 'text-accent-text',
          )}
        >
          {completed ? formatRelative(job.completed_at || job.created_at) : statusLabel(job.status)}
        </span>
      </button>
      {open ? <div className="mb-2 ml-[22px] mt-0.5 border-l border-line pl-3">{children}</div> : null}
    </div>
  )
}

function JobDetail({ session, projectUid, projectDir, job, onOpenImages }) {
  const showInViewer = useApp((state) => state.showInViewer)
  const navigate = useApp((state) => state.navigate)
  const toast = useApp((state) => state.toast)
  const [showRest, setShowRest] = useState(false)
  const volumes = job.volumes || []
  const primary = volumes.filter((volume) => PRIMARY_KINDS.has(volume.kind))
  const lead = primary.length ? primary : volumes
  const rest = primary.length ? volumes.filter((volume) => !PRIMARY_KINDS.has(volume.kind)) : []

  const view = (volume, { preview = true } = {}) =>
    showInViewer({
      kind: 'volume',
      url: api.cryosparcVolumeUrl(session.id, job.uid, volume.name, { preview }),
      key: `${session.id}:${job.uid}:${volume.name}`,
      label: `${job.uid} ${volumeLabel(volume)}`,
    })

  const attachToModeling = (volume) =>
    navigate({
      name: 'new-modeling',
      attachments: [{ type: 'cryosparc_map', session_id: session.id, job_uid: job.uid, name: volume.name, label: `${job.uid} ${volumeLabel(volume)}` }],
    })

  const copyPath = async (volume) => {
    const path = volumePath(volume, projectDir)
    try {
      await navigator.clipboard.writeText(path)
      toast({ title: 'Path copied' })
    } catch {
      toast({ title: 'Could not copy the path', description: path, tone: 'error' })
    }
  }

  const actions = { view, attachToModeling, copyPath, projectUid }

  return (
    <div className="flex flex-col gap-2 py-1">
      {volumes.length ? (
        <div>
          {lead.map((volume) => (
            <VolumeRow key={volume.name} job={job} volume={volume} session={session} actions={actions} />
          ))}
          {rest.length ? (
            showRest ? (
              rest.map((volume) => <VolumeRow key={volume.name} job={job} volume={volume} session={session} actions={actions} />)
            ) : (
              <button
                type="button"
                onClick={() => setShowRest(true)}
                className="flex h-7 items-center rounded-md px-2 text-xs text-muted hover:bg-hover hover:text-fg"
              >
                {moreOutputsLabel(rest)}
              </button>
            )
          ) : null}
        </div>
      ) : (
        <p className="px-2 text-xs text-faint">This job has no maps.</p>
      )}
      <JobImages sessionId={session.id} job={job} onOpen={onOpenImages} />
    </div>
  )
}

function VolumeRow({ job, volume, session, actions }) {
  const detail = volumeDetail(volume)
  return (
    <div className="group flex min-h-10 items-center gap-2.5 rounded-lg px-2 py-1 hover:bg-hover">
      <Cuboid size={15} strokeWidth={1.75} className="shrink-0 text-accent-text" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-fg" title={volume.path || undefined}>
          {volumeLabel(volume)}
          {volume.resolution_A && volume.resolution_A !== job.resolution_A ? (
            <span className="ml-2 text-xs tabular-nums text-muted">{formatResolution(volume.resolution_A)}</span>
          ) : null}
        </p>
        {volume.available === false ? (
          <p className="truncate text-xs text-warning">Not found in the project folder</p>
        ) : detail ? (
          <p className="truncate text-xs tabular-nums text-faint">{detail}</p>
        ) : null}
      </div>
      {/* The labels give way to icons in a narrow panel; the tooltips name them there. */}
      <Tip label="View">
        <Button size="xs" icon={Eye} aria-label={`View ${volumeLabel(volume)}`} disabled={volume.available === false} onClick={() => actions.view(volume)}>
          <span className="hidden @[360px]:inline">View</span>
        </Button>
      </Tip>
      <Tip label="Use for modeling">
        <Button
          size="xs"
          variant="ghost"
          icon={Atom}
          aria-label="Use for modeling"
          disabled={volume.available === false}
          onClick={() => actions.attachToModeling(volume)}
        >
          <span className="hidden @[460px]:inline">Use for modeling</span>
        </Button>
      </Tip>
      <Menu>
        <MenuTrigger asChild>
          <IconButton icon={MoreHorizontal} label="More actions" size="xs" disabled={volume.available === false} />
        </MenuTrigger>
        <MenuContent align="end" className="min-w-[220px]">
          <MenuItem icon={Maximize2} onSelect={() => actions.view(volume, { preview: false })}>
            View at full resolution
          </MenuItem>
          <MenuItem
            icon={Download}
            onSelect={() =>
              downloadUrl(
                api.cryosparcVolumeUrl(session.id, job.uid, volume.name, { preview: false }),
                `${actions.projectUid || 'cryosparc'}_${job.uid}_${volume.name}.mrc`,
              )
            }
          >
            Download
          </MenuItem>
          {volume.path ? (
            <>
              <MenuSeparator />
              <MenuItem icon={Copy} onSelect={() => actions.copyPath(volume)}>
                Copy path
              </MenuItem>
            </>
          ) : null}
        </MenuContent>
      </Menu>
    </div>
  )
}

function JobImages({ sessionId, job, onOpen }) {
  // Signing in to CryoSPARC (or changing the account) changes what the server can list.
  const account = useApp((state) => {
    const cryosparc = state.setup?.cryosparc
    return cryosparc ? [cryosparc.url, cryosparc.email, cryosparc.password_set, cryosparc.verified_at].join('|') : ''
  })
  const key = `${sessionId}:${job.uid}:${job.status}:${account}`
  const [state, setState] = useState(() => assetCache.get(key) || null)
  const [limit, setLimit] = useState(IMAGE_PAGE)

  useEffect(() => {
    if (assetCache.has(key)) {
      setState(assetCache.get(key))
      return undefined
    }
    let cancelled = false
    setState(null)
    api
      .cryosparcAssets(sessionId, job.uid)
      .then((data) => {
        const value = { assets: data.assets || [], reason: data.reason || null }
        // A reason (not signed in, CryoSPARC unreachable) may not hold next time, so only real lists are kept.
        if (!value.reason) assetCache.set(key, value)
        if (!cancelled) setState(value)
      })
      .catch((err) => {
        if (!cancelled) setState({ assets: [], reason: null, error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [key, sessionId, job.uid])

  const images = useMemo(
    () =>
      (state?.assets || []).map((asset) => ({
        src: api.cryosparcAssetUrl(sessionId, job.uid, asset.id),
        caption: asset.filename || asset.id,
        description: `${job.uid} ${jobName(job)}`,
      })),
    [state, sessionId, job],
  )

  if (!state) {
    return (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(88px,1fr))] gap-2 px-2">
        {[0, 1, 2].map((index) => (
          <Skeleton key={index} className="aspect-square w-full rounded-lg" />
        ))}
      </div>
    )
  }
  if (state.error) return <p className="px-2 text-xs text-danger">{state.error}</p>
  if (!images.length) return state.reason ? <p className="px-2 text-xs text-muted">{state.reason}</p> : null

  return (
    <div className="px-2">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(88px,1fr))] gap-2">
        {images.slice(0, limit).map((image, index) => (
          <button
            key={image.src}
            type="button"
            title={image.caption}
            onClick={() => onOpen(images, index)}
            className="overflow-hidden rounded-lg border border-line bg-inset transition-colors hover:border-line-strong"
          >
            <img src={image.src} alt={image.caption} loading="lazy" className="aspect-square w-full object-contain" />
          </button>
        ))}
      </div>
      {images.length > limit ? (
        <button
          type="button"
          onClick={() => setLimit(images.length)}
          className="mt-1.5 flex h-7 items-center rounded-md px-2 text-xs text-muted hover:bg-hover hover:text-fg"
        >
          Show all {images.length} images
        </button>
      ) : null}
    </div>
  )
}

function LoadingJobs() {
  return (
    <div className="flex flex-col gap-1 px-3 pt-1">
      {[0, 1, 2, 3, 4].map((index) => (
        <div key={index} className="flex h-9 items-center gap-3">
          <Skeleton className="h-1.5 w-1.5 rounded-full" />
          <Skeleton className="h-3 w-8" />
          <Skeleton className="h-3 flex-1" />
          <Skeleton className="h-3 w-12" />
        </div>
      ))}
    </div>
  )
}
