// Agent activity in the transcript: tool steps as quiet one-line rows that
// expand into code and output, runs of steps folded into "N steps", thought
// summaries, and sub-agent runs as nested transcripts.
import { memo, useEffect, useMemo, useState } from 'react'
import { Activity, Atom, Bot, ChevronRight, Layers, Lightbulb } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { basename, formatDuration, pluralize, toDate } from '../../lib/format'
import { languageForFile } from '../../lib/highlight'
import { useApp } from '../../store/app'
import { CodeBlock } from '../../components/CodeBlock'
import { Markdown } from '../../components/Markdown'
import { Button, Spinner } from '../../components/ui'
import { useCalls, useChat } from './context'
import { ImageGrid } from './ImageGrid'
import { DatasetMessage, Divider, Notice, Prose, TaskMessage, UserMessage } from './messages'
import { daqTablePath, describeStep, showFileInViewer, viewerKind, viewerTargets } from './tools'
import { buildReconstructionBlocks, groupSpan } from './transcriptModel'

const ROW = '-mx-2 flex h-7 w-[calc(100%+16px)] items-center gap-2 rounded-lg px-2 text-left text-sm transition-colors hover:bg-hover'
const NEST = 'ml-[13px] border-l border-line pl-4'

function seconds(start, end) {
  const from = toDate(start)
  const to = toDate(end)
  if (!from || !to) return null
  return Math.max(0, (to.getTime() - from.getTime()) / 1000)
}

function parseJson(text) {
  if (typeof text !== 'string') return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

/** Pretty JSON for objects and JSON strings; null for anything else. */
function prettyJson(value) {
  const parsed = typeof value === 'string' ? parseJson(value.trim()) : value
  if (!parsed || typeof parsed !== 'object') return null
  return JSON.stringify(parsed, null, 2)
}

// ============================================================== dispatcher
/**
 * Renders any transcript block. `live` marks the block the agent is working
 * on; `final` marks prose that closes a turn; `session` is passed to the
 * dataset block only.
 */
export const BlockView = memo(function BlockView({ block, live = false, final = false, session }) {
  switch (block.type) {
    case 'user':
      return <UserMessage block={block} />
    case 'dataset':
      return <DatasetMessage block={block} session={session} />
    case 'task':
      return <TaskMessage block={block} />
    case 'prose':
      return <Prose block={block} final={final} />
    case 'thought':
      return <ThoughtRow block={block} />
    case 'step':
      return <StepBlock step={block.step} live={live} />
    case 'group':
      return <GroupBlock block={block} live={live} />
    case 'images':
      return <ImageGrid images={block.images} />
    case 'subagents':
      return (
        <div className="flex flex-col gap-0.5">
          {block.calls.map((call) => (
            <SubAgent key={call.id} call={call} />
          ))}
        </div>
      )
    case 'divider':
      return <Divider block={block} />
    case 'notice':
      return <Notice text={block.text} tone={block.tone} />
    default:
      return null
  }
})

const ROW_TYPES = new Set(['step', 'group', 'thought', 'subagents', 'images'])
const USER_TYPES = new Set(['user', 'dataset'])

/** Vertical rhythm between two consecutive blocks. */
export function gapBefore(previous, block) {
  if (!previous) return ''
  if (USER_TYPES.has(block.type) || block.type === 'divider' || previous.type === 'divider') return 'mt-8'
  if (block.type === 'prose') return USER_TYPES.has(previous.type) ? 'mt-6' : 'mt-4'
  if (ROW_TYPES.has(block.type)) {
    if (ROW_TYPES.has(previous.type)) return 'mt-0.5'
    return USER_TYPES.has(previous.type) ? 'mt-6' : 'mt-3'
  }
  return 'mt-4'
}

/** Prose that closes a turn: nothing but a new message (or the end) follows it. */
export function isFinalProse(block, next) {
  return block.type === 'prose' && (!next || USER_TYPES.has(next.type) || next.type === 'divider')
}

/** A plain list of blocks without windowing, for nested transcripts. */
function BlockList({ blocks }) {
  return blocks.map((block, index) => (
    <div key={block.key} className={gapBefore(blocks[index - 1], block)}>
      <BlockView block={block} final={isFinalProse(block, blocks[index + 1])} />
    </div>
  ))
}

// ================================================================= thought
export const ThoughtRow = memo(function ThoughtRow({ block }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className={cx(ROW, 'group/row')}>
        <Lightbulb size={14} strokeWidth={1.75} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1 truncate text-muted group-hover/row:text-fg">{block.title || 'Thought'}</span>
        <ChevronRight
          size={13}
          strokeWidth={1.75}
          className={cx('shrink-0 text-faint transition-transform', open ? 'rotate-90' : 'opacity-0 group-hover/row:opacity-100')}
        />
      </button>
      {open ? (
        // Prose colours come from --fg; muting it here keeps thoughts quieter than answers.
        <div className={cx(NEST, 'my-1 py-1')} style={{ '--fg': 'var(--muted)' }}>
          <Markdown size="sm">{block.text}</Markdown>
        </div>
      ) : null}
    </div>
  )
})

// ==================================================================== step
function StepStatus({ step, pending }) {
  if (pending) return <Spinner size={13} className="shrink-0" />
  if (step.outcome === 'error') return <span className="shrink-0 text-xs text-danger">Failed</span>
  if (step.outcome === 'interrupted') return <span className="shrink-0 text-xs text-warning">Interrupted</span>
  const duration = seconds(step.startedAt, step.endedAt)
  if (duration !== null && duration >= 5) return <span className="shrink-0 text-xs tabular-nums text-faint">{formatDuration(duration)}</span>
  return null
}

/** Opens the DAQ tab on the result this step computed, not whichever result was picked last. */
function openDaqFor(step, workspace) {
  const path = daqTablePath(step, workspace)
  const app = useApp.getState()
  if (path) app.openDaq(path)
  else app.setWorkbenchTab('daq')
}

function StepActions({ step }) {
  const { projectId, workspace } = useChat()
  const targets = useMemo(() => (step.outcome === 'ok' ? viewerTargets(step, workspace) : []), [step, workspace])
  const daq = step.agent === 'modeling' && step.name === 'run_daq' && step.outcome === 'ok'
  if (!daq && !targets.length) return null
  return (
    <div className="ml-6 mt-0.5 flex flex-wrap items-center gap-1">
      {daq ? (
        <Button size="xs" variant="ghost" icon={Activity} onClick={() => openDaqFor(step, workspace)}>
          Open DAQ
        </Button>
      ) : null}
      {targets.map((rel) => (
        <Button
          key={rel}
          size="xs"
          variant="ghost"
          icon={viewerKind(rel) === 'structure' ? Atom : Layers}
          title={`Show ${rel} in the viewer`}
          onClick={() => showFileInViewer(projectId, rel)}
          className="max-w-[260px] font-normal"
        >
          <span className="min-w-0 truncate">{basename(rel)}</span>
        </Button>
      ))}
    </div>
  )
}

export const StepBlock = memo(function StepBlock({ step, live = false }) {
  const [open, setOpen] = useState(false)
  const pending = !step.result && live
  const { icon: Icon, text, target, mono, hint } = describeStep(step, { running: pending })
  const failed = step.outcome === 'error'
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className={cx(ROW, 'group/row')}>
        <Icon size={14} strokeWidth={1.75} className={cx('shrink-0', failed ? 'text-danger' : 'text-faint')} />
        <span className="min-w-0 flex-1 truncate">
          <span className="text-muted group-hover/row:text-fg">{text}</span>
          {target ? <span className={cx('ml-1.5 text-faint', mono && 'font-mono text-[12px]')}>{target}</span> : null}
        </span>
        <StepStatus step={step} pending={pending} />
        <ChevronRight
          size={13}
          strokeWidth={1.75}
          className={cx('shrink-0 text-faint transition-transform', open ? 'rotate-90' : 'opacity-0 group-hover/row:opacity-100')}
        />
      </button>
      {pending && hint ? <p className="ml-6 text-xs text-faint">{hint}</p> : null}
      <StepActions step={step} />
      {step.images.length ? <ImageGrid images={step.images} className="mb-1.5 ml-6 mt-1.5" /> : null}
      {open ? <StepDetails step={step} pending={pending} /> : null}
      {step.subcalls.length ? (
        <div className="ml-6 mt-0.5 flex flex-col gap-0.5">
          {step.subcalls.map((call) => (
            <SubAgent key={call.id} call={call} />
          ))}
        </div>
      ) : null}
    </div>
  )
})

// ------------------------------------------------------------ step details
const HIDDEN_EXTRA = new Set(['path', 'mime_type', 'bytes', 'message', 'interrupted', 'not_executed'])

/** Full code and output for a reconstruction step whose display copy was shortened. */
async function loadFullReconstruction(sessionId, step) {
  const [call, result] = await Promise.all([
    api.item(sessionId, step.id),
    step.resultId ? api.item(sessionId, step.resultId) : Promise.resolve(null),
  ])
  const args = parseJson(call?.payload?.arguments) || {}
  const output = parseJson(result?.payload?.output)
  return {
    code: typeof args.code === 'string' ? args.code : step.args.code,
    stdout: output ? String(output.stdout || '') : null,
    stderr: output ? String(output.stderr || '') : null,
    traceback: output ? String(output.traceback || '') : null,
    text: output ? null : typeof result?.payload?.output === 'string' ? result.payload.output : null,
  }
}

function ReconstructionDetails({ step, pending }) {
  const { sessionId } = useChat()
  const [full, setFull] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const result = step.result
  const args = step.args || {}
  const truncated = Boolean(step.truncatedCode || result?.truncated)

  const code = full?.code ?? args.code
  const stdout = full?.stdout ?? result?.stdout
  const stderr = full?.stderr ?? result?.stderr
  const traceback = full?.traceback ?? result?.traceback
  const text = full?.text ?? result?.text
  const extra = useMemo(() => {
    if (!result?.extra) return null
    const parsed = parseJson(result.extra)
    if (!parsed) return result.extra
    const shown = Object.fromEntries(Object.entries(parsed).filter(([key]) => !HIDDEN_EXTRA.has(key)))
    return Object.keys(shown).length ? JSON.stringify(shown, null, 2) : null
  }, [result])
  const otherArgs = useMemo(() => {
    const hidden = new Set(['code', ...(step.name === 'run_and_wait' ? ['condition', 'poll_seconds'] : []), ...(step.name === 'view_image' ? ['image_path', 'prompt'] : [])])
    const rest = Object.fromEntries(Object.entries(args).filter(([key, value]) => !hidden.has(key) && value !== '' && value !== null))
    return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : null
  }, [args, step.name])

  const loadFull = async () => {
    setLoading(true)
    setError('')
    try {
      setFull(await loadFullReconstruction(sessionId, step))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const hasOutput = Boolean(stdout || stderr || traceback || result?.error || extra || text)
  return (
    <div className={cx(NEST, 'my-1 flex flex-col gap-2 py-1')}>
      {step.name === 'view_image' ? (
        <div className="text-sm">
          {args.prompt ? <p className="text-muted">{args.prompt}</p> : null}
          {args.image_path ? <p className="mt-0.5 break-all font-mono text-[12px] text-faint">{args.image_path}</p> : null}
        </div>
      ) : null}
      {step.name === 'run_and_wait' && args.condition ? <CodeBlock code={args.condition} language="python" label="Waits until" wrap /> : null}
      {code ? <CodeBlock code={code} language="python" maxLines={30} /> : null}
      {otherArgs ? <CodeBlock code={otherArgs} language="json" wrap maxLines={16} /> : null}
      {step.rawArgs ? <CodeBlock code={step.rawArgs} wrap maxLines={16} /> : null}
      {stdout ? <CodeBlock code={stdout} label="Output" wrap maxLines={24} /> : null}
      {stderr ? <CodeBlock code={stderr} label="Errors" wrap maxLines={12} /> : null}
      {traceback ? <CodeBlock code={traceback} label="Traceback" wrap maxLines={16} className="border-danger/40" /> : null}
      {result?.error ? <p className="whitespace-pre-wrap break-words text-sm text-danger">{typeof result.error === 'string' ? result.error : JSON.stringify(result.error)}</p> : null}
      {extra ? <CodeBlock code={extra} language="json" label="Details" wrap maxLines={12} /> : null}
      {text ? <CodeBlock code={text} label="Output" wrap maxLines={24} /> : null}
      {!result ? (
        <p className="text-xs text-faint">{pending ? 'Running' : 'No result was recorded for this step.'}</p>
      ) : !hasOutput && step.name !== 'view_image' ? (
        <p className="text-xs text-faint">No output</p>
      ) : null}
      {truncated && !full && !step.nested ? (
        <div className="flex items-center gap-2">
          <Button size="xs" variant="secondary" loading={loading} onClick={loadFull}>
            Load full output
          </Button>
          {error ? <span className="text-xs text-danger">{error}</span> : null}
        </div>
      ) : null}
    </div>
  )
}

function modelingArguments(step) {
  const args = step.args || {}
  if (step.name === 'bash' && typeof args.command === 'string') {
    const where = args.working_directory && args.working_directory !== '.' ? args.working_directory : ''
    return [{ code: args.command, language: 'bash', label: where ? `In ${where}` : undefined }]
  }
  if (step.name === 'write_file' && typeof args.content === 'string') {
    return [{ code: args.content, language: languageForFile(args.path || ''), label: args.path, maxLines: 30 }]
  }
  if (step.name === 'edit_model' && typeof args.operations_json === 'string') {
    const operations = parseJson(args.operations_json)
    return [{ code: JSON.stringify({ ...args, operations_json: operations ?? args.operations_json }, null, 2), language: 'json' }]
  }
  if (Object.keys(args).length) return [{ code: JSON.stringify(args, null, 2), language: 'json' }]
  return step.rawArgs ? [{ code: step.rawArgs }] : []
}

function ModelingDetails({ step, pending }) {
  const blocks = useMemo(() => modelingArguments(step), [step])
  const text = step.result?.text || ''
  const json = useMemo(() => prettyJson(text), [text])
  const failed = step.outcome === 'error'
  return (
    <div className={cx(NEST, 'my-1 flex flex-col gap-2 py-1')}>
      {blocks.map((block, index) => (
        <CodeBlock key={index} code={block.code} language={block.language} label={block.label} wrap maxLines={block.maxLines || 16} />
      ))}
      {failed ? (
        <p className="whitespace-pre-wrap break-words text-sm text-danger">{text.replace(/^An error occurred while running the tool\. Please try again\. Error:\s*/, '')}</p>
      ) : text ? (
        <CodeBlock code={json || text} language={json ? 'json' : null} label="Output" wrap maxLines={24} />
      ) : null}
      {!step.result ? <p className="text-xs text-faint">{pending ? 'Running' : 'No result was recorded for this step.'}</p> : null}
      {step.result && !text && !step.images.length ? <p className="text-xs text-faint">No output</p> : null}
    </div>
  )
}

export function StepDetails({ step, pending }) {
  return step.agent === 'reconstruction' ? <ReconstructionDetails step={step} pending={pending} /> : <ModelingDetails step={step} pending={pending} />
}

// =================================================================== group
export const GroupBlock = memo(function GroupBlock({ block, live = false }) {
  const [open, setOpen] = useState(false)
  const { entries, stepCount } = block
  const failed = entries.filter((entry) => entry.type === 'step' && entry.step.outcome === 'error').length
  const { start, end } = groupSpan(entries)
  const duration = live ? null : seconds(start, end)
  const latest = entries[entries.length - 1]
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className={cx(ROW, 'group/row')}>
        {/* Failed steps inside the folded run show in its label, so they are found without opening it. */}
        <ChevronRight
          size={14}
          strokeWidth={1.75}
          className={cx('shrink-0 transition-transform', failed ? 'text-danger' : 'text-faint', open && 'rotate-90')}
        />
        <span className="min-w-0 flex-1 truncate">
          <span className="text-muted group-hover/row:text-fg">{pluralize(stepCount, 'step')}</span>
          {failed ? <span className="ml-1.5 text-danger">{failed} failed</span> : null}
        </span>
        {duration !== null && duration >= 5 ? <span className="shrink-0 text-xs tabular-nums text-faint">{formatDuration(duration)}</span> : null}
      </button>
      {open ? (
        <div className={cx(NEST, 'flex flex-col gap-0.5 pl-3')}>
          {entries.map((entry, index) =>
            entry.type === 'step' ? (
              <StepBlock key={entry.key} step={entry.step} live={live && index === entries.length - 1} />
            ) : (
              <ThoughtRow key={entry.key} block={entry} />
            ),
          )}
        </div>
      ) : live && latest ? (
        // While the agent works, the step under way stays visible below the folded run.
        <div className={cx(NEST, 'pl-3')}>
          {latest.type === 'step' ? <StepBlock step={latest.step} live /> : <ThoughtRow block={latest} />}
        </div>
      ) : null}
    </div>
  )
})

// =============================================================== sub-agent
const CALL_STATES = {
  failed: { label: 'Failed', className: 'text-danger' },
  interrupted: { label: 'Interrupted', className: 'text-warning' },
  budget_exhausted: { label: 'Out of budget', className: 'text-warning' },
}

function plainFirstLine(text) {
  const line = String(text || '')
    .split('\n')
    .map((entry) => entry.replace(/[*_`#>]/g, '').trim())
    .find(Boolean)
  return line || ''
}

const SUBAGENT_POLL_MS = 5000

/** A delegated sub-agent run: a folded row that opens into its own transcript. */
export const SubAgent = memo(function SubAgent({ call }) {
  const { sessionId } = useChat()
  const calls = useCalls()
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  const running = call.status === 'running'

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    let timer = 0
    const load = async () => {
      try {
        const data = await api.recursiveItems(sessionId, call.id)
        if (!cancelled) {
          setItems(data.items || [])
          setError('')
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
      if (!cancelled && running) timer = window.setTimeout(load, SUBAGENT_POLL_MS)
    }
    load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, running, sessionId, call.id])

  const built = useMemo(
    () => (items ? buildReconstructionBlocks(items, { sessionId, calls, nested: true, parentCallId: call.id }) : null),
    [items, sessionId, calls, call.id],
  )
  const state = CALL_STATES[call.status]
  const toolCalls = call.metrics?.tool_calls
  const duration = seconds(call.started_at, call.completed_at)
  const summary = running ? (toolCalls ? `${pluralize(toolCalls, 'tool call')} so far` : 'Starting') : plainFirstLine(call.final_output)
  const hasAnswer = built?.blocks.some((block) => block.type === 'prose')

  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className={cx(ROW, 'group/row')}>
        <Bot size={14} strokeWidth={1.75} className={cx('shrink-0', state?.className || 'text-faint')} />
        <span className="min-w-0 flex-1 truncate">
          <span className="text-muted group-hover/row:text-fg">Sub-agent</span>
          {summary ? <span className="ml-1.5 text-faint">{summary}</span> : null}
        </span>
        {running ? (
          <Spinner size={13} className="shrink-0" />
        ) : state ? (
          <span className={cx('shrink-0 text-xs', state.className)}>{state.label}</span>
        ) : duration !== null ? (
          <span className="shrink-0 text-xs tabular-nums text-faint">{formatDuration(duration)}</span>
        ) : null}
        <ChevronRight
          size={13}
          strokeWidth={1.75}
          className={cx('shrink-0 text-faint transition-transform', open ? 'rotate-90' : 'opacity-0 group-hover/row:opacity-100')}
        />
      </button>
      {open ? (
        <div className={cx(NEST, 'my-1 py-1')}>
          {error ? <p className="text-sm text-danger">{error}</p> : null}
          {!built && !error ? (
            <div className="flex items-center gap-2 text-sm text-muted">
              <Spinner size={13} /> Loading the sub-agent transcript
            </div>
          ) : null}
          {built ? <BlockList blocks={built.blocks} /> : null}
          {call.error ? (
            <div className="mt-3">
              <Notice text={typeof call.error === 'string' ? call.error : call.error.message || JSON.stringify(call.error)} />
            </div>
          ) : null}
          {built && !hasAnswer && call.final_output ? (
            <div className="mt-4">
              <Markdown>{call.final_output}</Markdown>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
})
