// Message-like transcript blocks: the user's messages, the reconstruction
// dataset prompt, agent prose, dividers, and notices.
import { memo, useEffect, useMemo, useState } from 'react'
import { Atom, Check, ChevronRight, CircleAlert, Copy, Database, Dna, FileText, History, Layers, Minimize2 } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { basename, formatDateTime, formatTime } from '../../lib/format'
import { CodeBlock, useCopy } from '../../components/CodeBlock'
import { Markdown } from '../../components/Markdown'
import { IconButton } from '../../components/ui'
import { attachmentChip } from '../attachments/AttachMenu'
import { useChat } from './context'
import { showFileInViewer, viewerKind, workspaceRelative } from './tools'

// ------------------------------------------------------------ long text
// Literal class names so Tailwind generates them.
const CLAMP = { 5: 'line-clamp-5', 14: 'line-clamp-14' }

/** Plain text that folds after `lines` (5 or 14) lines with a toggle. */
function FoldedText({ text, lines = 14, className }) {
  const [open, setOpen] = useState(false)
  const long = useMemo(() => text.length > lines * 90 || text.split('\n').length > lines, [text, lines])
  return (
    <>
      <p className={cx('whitespace-pre-wrap break-words', long && !open && CLAMP[lines], className)}>{text}</p>
      {long ? (
        <button type="button" onClick={() => setOpen((value) => !value)} className="mt-1 text-sm font-medium text-muted hover:text-fg">
          {open ? 'Show less' : 'Show more'}
        </button>
      ) : null}
    </>
  )
}

// ------------------------------------------------------------------ user
const INPUT_ICONS = { 'Density map': Layers, Model: Atom, 'Sequence (FASTA)': Dna }

function InputChip({ input }) {
  const { projectId, workspace } = useChat()
  const Icon = INPUT_ICONS[input.label] || FileText
  const rel = workspaceRelative(input.path, workspace)
  const viewable = rel && viewerKind(rel)
  const title = [input.label, input.detail].filter(Boolean).join(': ')
  const content = (
    <>
      <Icon size={13} strokeWidth={1.75} className="shrink-0 text-muted" />
      <span className="truncate">{basename(input.path)}</span>
    </>
  )
  const chip = 'inline-flex h-7 max-w-[300px] items-center gap-1.5 rounded-lg border border-line bg-canvas px-2 text-xs text-fg'
  if (!viewable) {
    return (
      <span className={chip} title={title}>
        {content}
      </span>
    )
  }
  return (
    <button
      type="button"
      title={`${title}\nShow in viewer`}
      onClick={() => showFileInViewer(projectId, rel)}
      className={cx(chip, 'transition-colors hover:border-line-strong hover:bg-hover')}
    >
      {content}
    </button>
  )
}

export const UserMessage = memo(function UserMessage({ block }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl bg-subtle px-4 py-2.5" title={formatDateTime(block.at)}>
        {block.text ? <FoldedText text={block.text} className="text-md text-fg" /> : null}
        {block.inputs?.length ? (
          <div className={cx('flex flex-wrap gap-1.5', block.text && 'mt-2')}>
            {block.inputs.map((input) => (
              <InputChip key={`${input.label}:${input.path}`} input={input} />
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
})

/** A message sent from this window that the transcript has not recorded yet. */
export function OutboxMessage({ text, attachments }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl bg-subtle px-4 py-2.5 opacity-70">
        {text ? <FoldedText text={text} className="text-md text-fg" /> : null}
        {attachments?.length ? (
          <div className={cx('flex flex-wrap gap-1.5', text && 'mt-2')}>
            {attachments.map((attachment) => {
              const chip = attachmentChip(attachment)
              const Icon = chip.icon || FileText
              return (
                <span
                  key={chip.key}
                  title={chip.title || chip.label}
                  className="inline-flex h-7 max-w-[300px] items-center gap-1.5 rounded-lg border border-line bg-canvas px-2 text-xs text-fg"
                >
                  <Icon size={13} strokeWidth={1.75} className="shrink-0 text-muted" />
                  <span className="truncate">{chip.label}</span>
                </span>
              )
            })}
          </div>
        ) : null}
      </div>
    </div>
  )
}

// --------------------------------------------------------------- dataset
const DATASET_KINDS = { movies: 'Movies', micrographs: 'Micrographs', particles: 'Particles', existing_project: 'Existing CryoSPARC project' }

function number(value) {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return ''
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 })
}

function withUnit(value, unit) {
  const text = number(value)
  return text ? `${text} ${unit}` : ''
}

let guidesRequest = null
function loadGuides() {
  guidesRequest ||= api.guides().catch((err) => {
    guidesRequest = null
    throw err
  })
  return guidesRequest
}

function datasetRows(manifest, session, guideLabel) {
  const dataset = manifest.dataset || {}
  const acquisition = manifest.acquisition || {}
  const sample = manifest.sample || {}
  const project = manifest.project || {}
  const config = session?.config || {}
  const projectUid = config.project_uid || session?.harness?.project_uid || project.project_uid
  const workspaceUid = config.workspace_uid || session?.harness?.workspace_uid || project.workspace_uid
  const eer = dataset.eer?.enabled ? `${dataset.eer.fractions} fractions, ${dataset.eer.upsampling}× upsampling` : ''
  return [
    ['Data', DATASET_KINDS[dataset.kind] || dataset.kind],
    ['Path', dataset.path, true],
    ['Particle metadata', dataset.particle_metadata_path, true],
    ['Gain reference', dataset.gain_reference_path, true],
    ['Defect file', dataset.defect_file_path, true],
    ['EER', eer],
    ['Pixel size', withUnit(acquisition.pixel_size_A, 'Å')],
    ['Voltage', withUnit(acquisition.accelerating_voltage_kV, 'kV')],
    ['Spherical aberration', withUnit(acquisition.spherical_aberration_mm, 'mm')],
    ['Total dose', withUnit(acquisition.total_dose_e_per_A2, 'e⁻/Å²')],
    ['Negative stain', acquisition.negative_stain ? 'Yes' : ''],
    ['Phase plate', acquisition.phase_plate ? 'Yes' : ''],
    ['Sample', sample.description],
    ['Particle diameter', withUnit(sample.particle_diameter_A, 'Å')],
    ['Molecular mass', withUnit(sample.molecular_mass_kDa, 'kDa')],
    ['Symmetry', sample.expected_symmetry],
    ['CryoSPARC project', [projectUid, workspaceUid].filter(Boolean).join(' / ')],
    ['Project folder', project.parent_directory, true],
    ['Processing guide', guideLabel],
  ].filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== '')
}

function datasetSummary(manifest) {
  const kind = DATASET_KINDS[manifest.dataset?.kind] || ''
  const parts = [
    kind,
    withUnit(manifest.acquisition?.pixel_size_A, 'Å/px'),
    withUnit(manifest.acquisition?.accelerating_voltage_kV, 'kV'),
    manifest.sample?.expected_symmetry && manifest.sample.expected_symmetry !== 'C1' ? `${manifest.sample.expected_symmetry} symmetry` : '',
  ].filter(Boolean)
  return parts.join(', ')
}

/** The reconstruction agent's first prompt, shown as the user's goal with the dataset folded underneath. */
export const DatasetMessage = memo(function DatasetMessage({ block, session }) {
  const manifest = session?.config?.manifest || session?.harness?.manifest || null
  const guideId = session?.config?.processing_guide
  const [open, setOpen] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [guideLabel, setGuideLabel] = useState('')

  useEffect(() => {
    if (!open || !guideId) return undefined
    let cancelled = false
    loadGuides()
      .then((data) => {
        if (cancelled) return
        const guide = (data.guides || []).find((entry) => entry.id === guideId)
        setGuideLabel(guide?.label || guide?.title || guideId)
      })
      .catch(() => !cancelled && setGuideLabel(guideId))
    return () => {
      cancelled = true
    }
  }, [open, guideId])

  const rows = useMemo(() => (manifest ? datasetRows(manifest, session, guideLabel) : []), [manifest, session, guideLabel])
  const goal = manifest?.user_goal?.trim()

  return (
    <div className="flex justify-end">
      <div className="w-full max-w-[85%] rounded-2xl bg-subtle px-4 py-2.5" title={formatDateTime(block.at)}>
        {goal ? <FoldedText text={goal} className="text-md text-fg" /> : null}
        <div className={cx(goal && 'mt-2 border-t border-line pt-1.5')}>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="-mx-1.5 flex h-7 w-[calc(100%+12px)] items-center gap-2 rounded-md px-1.5 text-left text-sm text-muted hover:text-fg"
          >
            <Database size={14} strokeWidth={1.75} className="shrink-0" />
            <span className="font-medium">{manifest ? 'Dataset' : 'Session prompt'}</span>
            {manifest ? <span className="min-w-0 flex-1 truncate text-faint">{datasetSummary(manifest)}</span> : <span className="flex-1" />}
            <ChevronRight size={14} strokeWidth={1.75} className={cx('shrink-0 transition-transform', open && 'rotate-90')} />
          </button>
          {open ? (
            <div className="pb-1.5 pt-1">
              {rows.length ? (
                <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
                  {rows.map(([label, value, mono]) => (
                    <div key={label} className="contents">
                      <dt className="text-muted">{label}</dt>
                      <dd className={cx('min-w-0 break-words text-fg', mono && 'break-all font-mono text-[12.5px]')}>{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {manifest ? (
                <button type="button" onClick={() => setShowPrompt((value) => !value)} className="mt-2 text-sm font-medium text-muted hover:text-fg">
                  {showPrompt ? 'Hide the prompt sent to the agent' : 'Show the prompt sent to the agent'}
                </button>
              ) : null}
              {showPrompt || !manifest ? (
                <CodeBlock code={block.text} language="markdown" wrap maxLines={40} tone="canvas" className="mt-2" />
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
})

/** A sub-agent's task, at the top of its nested transcript. */
export const TaskMessage = memo(function TaskMessage({ block }) {
  return (
    <div className="rounded-xl border border-line bg-inset px-3 py-2">
      <FoldedText text={block.text} lines={5} className="text-sm text-muted" />
    </div>
  )
})

// ----------------------------------------------------------------- prose
/** The agent's words. `final` marks the closing answer of a turn, which gets copy and time actions. */
export const Prose = memo(function Prose({ block, final = false, streaming = false }) {
  const [copied, copy] = useCopy()
  return (
    <div className="group/prose">
      <Markdown>{block.text}</Markdown>
      {final && !streaming ? (
        <div className="-ml-1 mt-1 flex h-6 items-center gap-1 opacity-0 transition-opacity group-hover/prose:opacity-100 focus-within:opacity-100">
          <IconButton icon={copied ? Check : Copy} label={copied ? 'Copied' : 'Copy'} size="xs" onClick={() => copy(block.text)} />
          {block.at ? <span className="text-xs text-faint">{formatTime(block.at)}</span> : null}
        </div>
      ) : null}
    </div>
  )
})

// --------------------------------------------------------------- divider
const COMPACTION_INTRO = /^\[CONTEXT COMPACTION - REFERENCE ONLY\][^\n]*\n(?:[^\n]+\n)*\n?/

function compactionBody(text) {
  const body = String(text || '').replace(COMPACTION_INTRO, '').trim()
  try {
    return { code: JSON.stringify(JSON.parse(body), null, 2), language: 'json' }
  } catch {
    return { code: body, language: null }
  }
}

export const Divider = memo(function Divider({ block }) {
  const [open, setOpen] = useState(false)
  const compaction = block.variant === 'compaction'
  const Icon = compaction ? Minimize2 : History
  const label = compaction ? 'Earlier context summarized' : 'Session resumed'
  const body = useMemo(
    () => (open ? (compaction ? compactionBody(block.text) : { code: String(block.text || '').replace(/^\[SESSION RESUME\]\s*/, ''), language: null }) : null),
    [open, compaction, block.text],
  )
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        title={formatDateTime(block.at)}
        className="group/divider flex w-full items-center gap-3 text-xs text-faint hover:text-muted"
      >
        <span className="h-px flex-1 bg-line" />
        <span className="flex items-center gap-1.5">
          <Icon size={13} strokeWidth={1.75} />
          {label}
          <ChevronRight size={12} strokeWidth={1.75} className={cx('transition-transform', open && 'rotate-90')} />
        </span>
        <span className="h-px flex-1 bg-line" />
      </button>
      {body ? <CodeBlock code={body.code} language={body.language} wrap maxLines={30} className="mt-3" /> : null}
    </div>
  )
})

// ---------------------------------------------------------------- notice
export const Notice = memo(function Notice({ text, tone = 'error' }) {
  return (
    <div
      className={cx(
        'flex items-start gap-2.5 rounded-xl px-3.5 py-2.5 text-sm',
        tone === 'error' ? 'bg-danger-soft text-danger' : 'bg-warning-soft text-warning',
      )}
    >
      <CircleAlert size={15} strokeWidth={1.75} className="mt-0.5 shrink-0" />
      <span className="min-w-0 whitespace-pre-wrap break-words">{text}</span>
    </div>
  )
})
