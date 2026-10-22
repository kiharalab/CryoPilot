// New modeling session: one free-text box, like a chat. Maps from the
// project's reconstructions, models, and sequences are attached with "+",
// dropped onto the page, or picked from the maps CryoPilot found.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ClipboardCheck, Gauge, Hammer, Layers, LoaderCircle, Plus, Upload, Wrench } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative, formatResolution } from '../../lib/format'
import { useApp } from '../../store/app'
import { Composer } from '../../components/Composer'
import { InlineError } from '../../components/ui'
import { AttachMenu, attachmentChip, attachmentKey, mapAttachment, mergeAttachments, uploadToModeling } from '../attachments/AttachMenu'
import { ModelChip } from './AgentChips'
import { SetupNotice, setupMessage } from './SetupNotice'

const SUGGESTIONS = [
  {
    id: 'build',
    label: 'Build a model',
    icon: Hammer,
    needsCryozeta: true,
    text: 'Build an atomic model into the attached map from its sequence with CryoZeta, fit it to the map, and validate it with DAQ.',
  },
  {
    id: 'validate',
    label: 'Validate with DAQ',
    icon: ClipboardCheck,
    text: 'Validate the attached model against its map: compute residue-wise DAQ scores, point out the regions that fit poorly, and summarize the overall quality.',
  },
  {
    id: 'fix',
    label: 'Fix poorly fitting regions',
    icon: Wrench,
    text: 'Find the regions of the attached model that fit the map poorly (19-residue mean AA DAQ below 0), rebuild or refine them, and compare DAQ before and after.',
  },
  {
    id: 'refine',
    label: 'Refine and report statistics',
    icon: Gauge,
    text: 'Refine the attached model against the map, then report the model and map statistics: DAQ, Q-score, and the Phenix validation summary.',
  },
]

const REQUIRED_TOOLS = [
  ['pymol', 'PyMOL'],
  ['chimerax', 'ChimeraX'],
  ['vesper', 'VESPER'],
]

const FOUND_MAPS_SHOWN = 3

/** "PyMOL", "PyMOL and VESPER", "PyMOL, ChimeraX, and VESPER". */
function joinNames(names) {
  if (names.length < 3) return names.join(' and ')
  return `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`
}

function Spinning(props) {
  return <LoaderCircle {...props} className={cx(props.className, 'animate-spin')} />
}

export function NewModeling({ initialAttachments }) {
  const project = useApp((state) => state.project)
  const setup = useApp((state) => state.setup)
  const patchSession = useApp((state) => state.patchSession)
  const openSession = useApp((state) => state.openSession)
  const toast = useApp((state) => state.toast)

  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState(() => mergeAttachments([], initialAttachments))
  const [pending, setPending] = useState([]) // uploads in flight: [{key, name}]
  const [model, setModel] = useState(setup?.defaults?.modeling_model || 'gpt-5.5')
  const [effort, setEffort] = useState(setup?.defaults?.modeling_effort || 'high')
  const [suggestion, setSuggestion] = useState(null)
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  const [found, setFound] = useState({ maps: [], errors: [] }) // api.projectMaps
  const [dragging, setDragging] = useState(false)
  const composerRef = useRef(null)
  const dragDepth = useRef(0)
  const uploadId = useRef(0)

  // "Use for modeling" from the Maps or Files tab while this screen is open.
  useEffect(() => {
    if (initialAttachments?.length) setAttachments((current) => mergeAttachments(current, initialAttachments))
  }, [initialAttachments])

  useEffect(() => {
    let cancelled = false
    api
      .projectMaps(project.id)
      .then((data) => {
        if (cancelled) return
        // A map whose file is gone from the project folder cannot be attached.
        setFound({ maps: (data.maps || []).filter((volume) => volume.available !== false), errors: data.errors || [] })
      })
      .catch(() => !cancelled && setFound({ maps: [], errors: [] }))
    return () => {
      cancelled = true
    }
  }, [project.id])

  const attachedKeys = useMemo(() => attachments.map(attachmentKey), [attachments])
  const chips = useMemo(
    () => [
      ...attachments.map(attachmentChip),
      ...pending.map((upload) => ({ key: upload.key, label: `Uploading ${upload.name}`, icon: Spinning })),
    ],
    [attachments, pending],
  )
  const hasMap = attachments.some((attachment) => attachment.type === 'cryosparc_map' || /\.(mrc|map|ccp4)(\.gz)?$/i.test(attachment.path || ''))
  const mapSuggestions = hasMap ? [] : found.maps.slice(0, FOUND_MAPS_SHOWN)
  // No maps because a session could not be read: say why instead of showing nothing.
  const mapsError = !hasMap && !found.maps.length ? found.errors[0] : null

  const attach = useCallback((attachment) => setAttachments((current) => mergeAttachments(current, [attachment])), [])

  const uploadFiles = useCallback(
    async (files) => {
      for (const file of files) {
        uploadId.current += 1
        const key = `upload:${uploadId.current}`
        setPending((current) => [...current, { key, name: file.name }])
        try {
          attach(await uploadToModeling(project.id, file))
        } catch (err) {
          toast({ title: `Could not upload ${file.name}`, description: err.message, tone: 'error' })
        } finally {
          setPending((current) => current.filter((upload) => upload.key !== key))
        }
      }
    },
    [attach, project.id, toast],
  )

  const openaiReady = Boolean(setup?.openai?.configured)
  const cryozetaReady = Boolean(setup?.cryozeta?.username && setup?.cryozeta?.password_set)
  const cryosparcSignedIn = Boolean(setup?.cryosparc?.email && setup?.cryosparc?.password_set)
  const tools = setup?.modeling_tools
  const missingTools = tools ? REQUIRED_TOOLS.filter(([key]) => tools[key] && !tools[key].ok).map(([, label]) => label) : []
  const activeSuggestion = SUGGESTIONS.find((entry) => entry.id === suggestion && entry.text === text.trim())

  const submit = async () => {
    if (busy || pending.length) return
    setBusy(true)
    setSubmitError(null)
    try {
      const session = await api.createModeling(project.id, {
        prompt: text.trim(),
        attachments,
        model,
        reasoning_effort: effort,
      })
      // The user may have switched projects while the agent was starting.
      if (useApp.getState().projectId !== project.id) {
        useApp.getState().toast({ title: `Session started in ${project.title}` })
        return
      }
      patchSession(session)
      openSession(session.id)
    } catch (err) {
      setSubmitError({ message: err.fields?.prompt ? 'Describe what the modeling agent should do.' : err.message, setup: err.setup })
      setBusy(false)
    }
  }

  const applySuggestion = (entry) => {
    setText(entry.text)
    setSuggestion(entry.id)
    composerRef.current?.focus()
  }

  const dropHandlers = {
    onDragEnter: (event) => {
      if (!event.dataTransfer?.types?.includes('Files')) return
      event.preventDefault()
      dragDepth.current += 1
      setDragging(true)
    },
    onDragOver: (event) => {
      if (!event.dataTransfer?.types?.includes('Files')) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
    },
    onDragLeave: () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1)
      if (!dragDepth.current) setDragging(false)
    },
    onDrop: (event) => {
      if (!event.dataTransfer?.files?.length) return
      event.preventDefault()
      dragDepth.current = 0
      setDragging(false)
      uploadFiles(Array.from(event.dataTransfer.files))
    },
  }

  return (
    <div className="relative flex h-full flex-col" {...dropHandlers}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="@container mx-auto flex min-h-full w-full max-w-[760px] flex-col justify-center px-6 pb-[12vh] pt-10">
          <h1 className="mb-6 text-center text-2xl font-semibold tracking-[-0.02em] text-fg">What should the modeling agent do?</h1>

          <div className="mb-3 flex flex-col gap-2 empty:hidden">
            {!openaiReady ? <SetupNotice settingsSection="accounts">{setupMessage('openai')}</SetupNotice> : null}
            {missingTools.length ? (
              <SetupNotice settingsSection="modeling">
                {joinNames(missingTools)} {missingTools.length > 1 ? 'were' : 'was'} not found on this computer. Steps that need{' '}
                {missingTools.length > 1 ? 'them' : 'it'} will fail.
              </SetupNotice>
            ) : null}
            {submitError?.setup ? (
              <SetupNotice settingsSection="accounts">{setupMessage(submitError.setup, submitError.message)}</SetupNotice>
            ) : submitError ? (
              <InlineError>{submitError.message}</InlineError>
            ) : null}
          </div>

          <Composer
            ref={composerRef}
            size="lg"
            minRows={3}
            autoFocus
            value={text}
            onChange={setText}
            onSubmit={submit}
            canSubmit={Boolean(text.trim()) && openaiReady && !pending.length}
            busy={busy}
            placeholder="Describe the model you need, or ask about one"
            attachments={chips}
            onRemoveAttachment={(key) => setAttachments((current) => current.filter((attachment) => attachmentKey(attachment) !== key))}
            toolbarLeft={
              <>
                <AttachMenu projectId={project.id} onAttach={attach} attachedKeys={attachedKeys} disabled={busy} />
                <ModelChip
                  model={model}
                  effort={effort}
                  onModelChange={setModel}
                  onEffortChange={setEffort}
                  defaultModel={setup?.defaults?.modeling_model}
                />
              </>
            }
          />

          {activeSuggestion?.needsCryozeta && !cryozetaReady ? (
            <SetupNotice className="mt-3" settingsSection="accounts">
              {setupMessage('cryozeta')}
            </SetupNotice>
          ) : null}

          <div className="mt-4 flex flex-wrap justify-center gap-2">
            {SUGGESTIONS.map((entry) => {
              const Icon = entry.icon
              const selected = activeSuggestion?.id === entry.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => applySuggestion(entry)}
                  aria-pressed={selected}
                  className={cx(
                    'inline-flex h-8 items-center gap-2 rounded-full border px-3 text-sm transition-colors',
                    selected ? 'border-line-strong bg-hover text-fg' : 'border-line text-muted hover:bg-hover hover:text-fg',
                  )}
                >
                  <Icon size={14} strokeWidth={1.75} className="shrink-0" />
                  {entry.label}
                </button>
              )
            })}
          </div>

          {mapSuggestions.length ? (
            <div className="mt-8">
              <p className="mb-1.5 px-1 text-xs font-medium text-faint">Maps from this project's reconstructions</p>
              <ul className="overflow-hidden rounded-xl border border-line">
                {mapSuggestions.map((volume) => {
                  const attachment = mapAttachment(volume)
                  return (
                    <li key={attachmentKey(attachment)} className="border-t border-line first:border-t-0">
                      <button
                        type="button"
                        onClick={() => attach(attachment)}
                        className="group flex h-11 w-full items-center gap-3 px-3.5 text-left transition-colors hover:bg-hover"
                        title={`Attach ${attachment.label}${volume.session_title ? ` from ${volume.session_title}` : ''}`}
                      >
                        <Layers size={15} strokeWidth={1.75} className="shrink-0 text-muted" />
                        <span className="shrink-0 font-mono text-[12px] text-faint">{volume.job_uid}</span>
                        <span className="min-w-0 truncate text-sm text-fg">{volume.label || volume.name}</span>
                        <span className="min-w-0 flex-1 truncate text-sm text-muted">{volume.job_title || volume.job_type}</span>
                        {volume.resolution_A ? (
                          <span className="shrink-0 text-xs tabular-nums text-muted">{formatResolution(volume.resolution_A)}</span>
                        ) : null}
                        <span className="hidden w-16 shrink-0 text-right text-xs text-faint @md:inline">{formatRelative(volume.completed_at)}</span>
                        <Plus size={14} strokeWidth={1.75} className="shrink-0 text-faint group-hover:text-fg" />
                      </button>
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : mapsError ? (
            <div className="mt-8">
              <p className="mb-1.5 px-1 text-xs font-medium text-faint">Maps from this project's reconstructions</p>
              <SetupNotice
                tone="neutral"
                settingsSection={found.errors.some((error) => error.setup === 'cryosparc') || !cryosparcSignedIn ? 'accounts' : undefined}
              >
                {mapsError.message}
              </SetupNotice>
            </div>
          ) : null}
        </div>
      </div>

      {dragging ? (
        <div className="pointer-events-none absolute inset-3 z-10 flex items-center justify-center rounded-2xl border-2 border-dashed border-accent bg-accent-soft/60">
          <p className="flex items-center gap-2 rounded-lg bg-elevated px-3 py-2 text-sm font-medium text-fg shadow-popover">
            <Upload size={15} strokeWidth={1.75} className="text-accent-text" />
            Drop to upload into the modeling workspace
          </p>
        </div>
      ) : null}
    </div>
  )
}
