// Turns a session's raw transcript into the blocks the conversation shows:
// messages, agent prose, tool steps paired with their results (and images),
// runs of steps folded into one row, sub-agent runs, and dividers.
// Pure functions: no React, no fetching.
//
// Block shapes (every block has a unique `key`):
//   {type:'user', id, text, inputs, at}            a message from the user
//   {type:'dataset', id, text, at}                 the generated manifest prompt
//   {type:'task', id, text, at}                    a sub-agent's task
//   {type:'prose', id, text, at}                   the agent's answer or commentary
//   {type:'thought', id, text, title, at}          a reasoning summary
//   {type:'step', step}                            one tool call and its result
//   {type:'group', entries:[step|thought blocks], stepCount}
//   {type:'images', id, images, text}              an image with no matching call
//   {type:'subagents', calls}                      sub-agent runs not tied to a call
//   {type:'divider', variant:'compaction'|'resume', id, text, at}
//   {type:'notice', tone:'error', id, text, at}
import { api } from '../../lib/api'
import { basename } from '../../lib/format'

/** Blocks rendered at first; "Show earlier" adds this many more. */
export const PAGE_SIZE = 150
/** Runs of at least this many tool steps fold into one "N steps" row. */
const GROUP_MIN_STEPS = 3
/** Tools whose output is an image the user should see without expanding anything. */
const IMAGE_TOOLS = {
  reconstruction: new Set(['view_image']),
  modeling: new Set(['view_image', 'run_daq', 'render_pdb', 'render_map_views', 'render_map_model']),
}
const MODELING_TOOL_ERROR = 'An error occurred while running the tool'

function parseJson(text) {
  if (typeof text !== 'string') return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

// ------------------------------------------------------------------ steps
function newStep({ agent, key, id, callId, name, args, rawArgs, at, nested }) {
  return {
    key,
    id,
    callId: callId || null,
    agent,
    name: name || 'tool',
    args: args || {},
    rawArgs: rawArgs || '',
    nested: Boolean(nested),
    startedAt: at || null,
    endedAt: null,
    result: null,
    resultId: null,
    outcome: null, // null (no result yet) | 'ok' | 'error' | 'interrupted'
    images: [],
    imageId: null,
    subcalls: [],
  }
}

function reconstructionOutcome(node) {
  const extra = parseJson(node.extra)
  if (extra?.interrupted) return 'interrupted'
  if (node.ok === false || node.traceback || node.error) return 'error'
  return 'ok'
}

/** A step stays visible on its own (outside "N steps") when it shows images or runs sub-agents. */
function isNotable(step) {
  if (step.images.length || step.subcalls.length) return true
  if (IMAGE_TOOLS[step.agent]?.has(step.name)) return true
  return step.agent === 'reconstruction' && /\brlm\(/.test(step.args?.code || '')
}

// --------------------------------------------------------------- grouping
function groupAtoms(atoms) {
  const blocks = []
  let run = []
  const flush = () => {
    if (!run.length) return
    const stepCount = run.reduce((count, atom) => count + (atom.type === 'step' ? 1 : 0), 0)
    if (stepCount >= GROUP_MIN_STEPS) blocks.push({ type: 'group', key: `g-${run[0].key}`, entries: run, stepCount })
    else blocks.push(...run)
    run = []
  }
  for (const atom of atoms) {
    if (atom.type === 'step' && isNotable(atom.step)) {
      flush()
      blocks.push(atom)
    } else if (atom.type === 'step' || atom.type === 'thought') {
      run.push(atom)
    } else {
      flush()
      blocks.push(atom)
    }
  }
  flush()
  return blocks
}

/** "**Planning the run**\n\n..." -> "Planning the run"; else the first line, shortened. */
export function thoughtTitle(text) {
  const trimmed = String(text || '').trim()
  const bold = /^\*\*(.+?)\*\*/.exec(trimmed)
  if (bold) return bold[1].trim()
  const line = trimmed.split('\n')[0].replace(/^#+\s*/, '')
  return line.length > 90 ? `${line.slice(0, 88).trimEnd()}…` : line
}

/**
 * Modeling requests end with the inputs the server attached:
 * "Inputs:\n- Density map: workspaces/x/data/a.mrc (detail)". Returns the
 * message without that list and the parsed inputs.
 */
export function splitInputs(text) {
  const value = String(text || '')
  const match = /(?:^|\n\n)Inputs:\n((?:- .+(?:\n|$))+)$/.exec(value)
  if (!match) return { text: value, inputs: [] }
  const inputs = []
  for (const line of match[1].split('\n')) {
    if (!line.trim()) continue
    const parsed = /^- ([^:]+): (.+?)(?: \((.*)\))?\s*$/.exec(line.trim())
    if (!parsed) return { text: value, inputs: [] }
    inputs.push({ label: parsed[1].trim(), path: parsed[2].trim(), detail: parsed[3] || '' })
  }
  return { text: value.slice(0, match.index).trim(), inputs }
}

// --------------------------------------------------------- reconstruction
function subcallIndex(calls, parentCallId) {
  const index = new Map()
  const unmatched = []
  for (const call of calls || []) {
    if (parentCallId == null) {
      if (call.parent_call_id) continue
      const list = index.get(call.root_function_call_item_id) || []
      list.push(call)
      index.set(call.root_function_call_item_id, list)
    } else if (call.parent_call_id === parentCallId) {
      if (!call.invoking_call_id) {
        unmatched.push(call)
        continue
      }
      const list = index.get(call.invoking_call_id) || []
      list.push(call)
      index.set(call.invoking_call_id, list)
    }
  }
  return { index, unmatched }
}

/**
 * Reconstruction transcript (trace.py nodes) -> {blocks, userCount, lastUserAt}.
 * `calls` are the session's recursive calls. For a sub-agent's own transcript
 * pass `nested: true` and `parentCallId`: its bootstrap prompt is the task,
 * and its images come from api.recursiveItemImageUrl when the server has that route.
 */
export function buildReconstructionBlocks(items, { sessionId, calls = [], nested = false, parentCallId = null } = {}) {
  const atoms = []
  const byCallId = new Map()
  const steps = []
  let imageStep = null
  let userCount = 0
  let lastUserAt = null

  for (const node of items) {
    switch (node.kind) {
      case 'tool_call': {
        const args = { ...(node.arguments && typeof node.arguments === 'object' ? node.arguments : {}) }
        if (typeof node.code === 'string') args.code = node.code
        const step = newStep({
          agent: 'reconstruction',
          key: `s-${node.id}`,
          id: node.id,
          callId: node.call_id,
          name: node.name,
          args,
          rawArgs: node.text,
          at: node.created_at,
          nested,
        })
        step.truncatedCode = Boolean(node.truncated)
        if (node.call_id) byCallId.set(node.call_id, step)
        steps.push(step)
        atoms.push({ type: 'step', key: step.key, step })
        imageStep = node.name === 'view_image' ? step : imageStep
        break
      }
      case 'tool_result': {
        let step = node.call_id ? byCallId.get(node.call_id) : null
        if (!step || step.result) {
          step = newStep({ agent: 'reconstruction', key: `s-${node.id}`, id: node.id, callId: node.call_id, name: 'tool', at: node.created_at, nested })
          steps.push(step)
          atoms.push({ type: 'step', key: step.key, step })
        }
        step.result = node
        step.resultId = node.id
        step.endedAt = node.created_at
        step.outcome = reconstructionOutcome(node)
        break
      }
      case 'image': {
        // A sub-agent's images live in its own item table; they need the
        // recursive image route, when the server offers one.
        const imageUrl = nested ? api.recursiveItemImageUrl : api.itemImageUrl
        const images = imageUrl
          ? (node.images || []).map((image) => ({
              src: nested ? imageUrl(sessionId, parentCallId, node.id, image.index) : imageUrl(sessionId, node.id, image.index),
              caption: imageStep ? basename(imageStep.args.image_path || '') : 'Image',
              description: node.text || '',
            }))
          : []
        if (imageStep && !imageStep.imageId) {
          imageStep.imageId = node.id
          imageStep.images = images
          imageStep = null
        } else if (images.length) {
          atoms.push({ type: 'images', key: `i-${node.id}`, id: node.id, images, text: node.text || '' })
        }
        break
      }
      case 'user':
        imageStep = null
        if (node.bootstrap) {
          atoms.push({ type: nested ? 'task' : 'dataset', key: `u-${node.id}`, id: node.id, text: node.text || '', at: node.created_at })
        } else {
          userCount += 1
          atoms.push({ type: 'user', key: `u-${node.id}`, id: node.id, text: node.text || '', inputs: [], at: node.created_at })
        }
        if (!nested) lastUserAt = node.created_at
        break
      case 'assistant':
        imageStep = null
        if (node.text?.trim()) atoms.push({ type: 'prose', key: `a-${node.id}`, id: node.id, text: node.text, at: node.created_at })
        break
      case 'reasoning':
        // The reconstruction agent's reasoning is encrypted; only summaries are shown.
        if (node.text?.trim()) {
          atoms.push({ type: 'thought', key: `r-${node.id}`, id: node.id, text: node.text, title: thoughtTitle(node.text), at: node.created_at })
        }
        break
      case 'compaction':
      case 'resume':
        imageStep = null
        atoms.push({ type: 'divider', key: `d-${node.id}`, id: node.id, variant: node.kind, text: node.text || '', at: node.created_at })
        break
      default:
        // developer instructions and anything unrecognised stay out of the conversation
        break
    }
  }

  const { index, unmatched } = subcallIndex(calls, parentCallId)
  if (index.size) {
    for (const step of steps) {
      const attached = index.get(parentCallId == null ? step.id : step.callId)
      if (attached) step.subcalls = attached
    }
  }
  const blocks = groupAtoms(atoms)
  if (unmatched.length) blocks.push({ type: 'subagents', key: `c-${parentCallId}`, calls: unmatched })
  return { blocks, userCount, lastUserAt }
}

// --------------------------------------------------------------- modeling
/** Modeling events -> {blocks, userCount, lastUserAt}. */
export function buildModelingBlocks(events) {
  const atoms = []
  const byCallId = new Map()
  let userCount = 0
  let lastUserAt = null

  for (const event of events) {
    const payload = event.payload || {}
    switch (event.kind) {
      case 'user': {
        const { text, inputs } = splitInputs(payload.text)
        userCount += 1
        lastUserAt = event.created_at
        atoms.push({ type: 'user', key: `u-${event.id}`, id: event.id, text, inputs, at: event.created_at })
        break
      }
      case 'reasoning':
        if (payload.text?.trim()) {
          atoms.push({ type: 'thought', key: `r-${event.id}`, id: event.id, text: payload.text, title: thoughtTitle(payload.text), at: event.created_at })
        }
        break
      case 'tool_call': {
        const parsed = typeof payload.arguments === 'string' ? parseJson(payload.arguments) : payload.arguments
        const step = newStep({
          agent: 'modeling',
          key: `s-${event.id}`,
          id: event.id,
          callId: payload.call_id,
          name: payload.name,
          args: parsed && typeof parsed === 'object' ? parsed : {},
          rawArgs: typeof payload.arguments === 'string' && !parsed ? payload.arguments : '',
          at: event.created_at,
        })
        if (payload.call_id) byCallId.set(payload.call_id, step)
        atoms.push({ type: 'step', key: step.key, step })
        break
      }
      case 'tool_output': {
        let step = payload.call_id ? byCallId.get(payload.call_id) : null
        if (!step || step.result) {
          step = newStep({ agent: 'modeling', key: `s-${event.id}`, id: event.id, callId: payload.call_id, name: payload.name, at: event.created_at })
          atoms.push({ type: 'step', key: step.key, step })
        }
        const text = String(payload.text || '')
        step.result = { text, images: payload.images || [] }
        step.resultId = event.id
        step.endedAt = event.created_at
        step.outcome = text.startsWith(MODELING_TOOL_ERROR) ? 'error' : 'ok'
        step.images = (payload.images || []).map((src, index) => ({
          src,
          caption: payload.images.length > 1 ? `${step.name.replace(/_/g, ' ')} ${index + 1}` : step.name.replace(/_/g, ' '),
          description: '',
        }))
        break
      }
      case 'answer':
        if (payload.text?.trim()) atoms.push({ type: 'prose', key: `a-${event.id}`, id: event.id, text: payload.text, at: event.created_at })
        break
      case 'error':
        atoms.push({ type: 'notice', key: `n-${event.id}`, id: event.id, tone: 'error', text: payload.message || 'The agent stopped with an error.', at: event.created_at })
        break
      default:
        break
    }
  }
  return { blocks: groupAtoms(atoms), userCount, lastUserAt }
}

/** The step the agent is on: the newest step of the transcript, when it has no result yet. */
export function pendingStep(blocks) {
  const last = blocks[blocks.length - 1]
  if (last?.type === 'step') return last.step.result ? null : last.step
  if (last?.type === 'group') {
    const entry = last.entries[last.entries.length - 1]
    return entry?.type === 'step' && !entry.step.result ? entry.step : null
  }
  return null
}

// ------------------------------------------------------------- stability
function callSignature(call) {
  return `${call.id}.${call.status}.${call.completed_at || ''}.${call.metrics?.tool_calls ?? ''}`
}

function stepSignature(step) {
  return `${step.key}:${step.resultId ?? ''}:${step.imageId ?? ''}:${step.subcalls.map(callSignature).join(',')}`
}

function signature(block) {
  switch (block.type) {
    case 'step':
      return stepSignature(block.step)
    case 'group':
      return block.entries.map((entry) => (entry.type === 'step' ? stepSignature(entry.step) : entry.key)).join('|')
    case 'subagents':
      return block.calls.map(callSignature).join(',')
    default:
      return block.key
  }
}

/**
 * Reuses the previous block object when nothing in it changed, so memoised
 * block components skip re-rendering as a long transcript grows.
 * Returns {blocks, cache}; keep `cache` for the next call.
 */
export function stabilize(blocks, cache = new Map()) {
  const next = new Map()
  const stable = blocks.map((block) => {
    const sig = signature(block)
    const previous = cache.get(block.key)
    const chosen = previous && previous.sig === sig ? previous : { ...block, sig }
    next.set(block.key, chosen)
    return chosen
  })
  return { blocks: stable, cache: next }
}

/** First and last timestamps of a group's steps, for its duration. */
export function groupSpan(entries) {
  let start = null
  let end = null
  for (const entry of entries) {
    if (entry.type !== 'step') continue
    start = start ?? entry.step.startedAt
    end = entry.step.endedAt || end
  }
  return { start, end }
}
