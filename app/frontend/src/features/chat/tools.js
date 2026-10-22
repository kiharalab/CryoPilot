// How a tool step reads in the conversation: an icon, a short verb phrase,
// and its target, for the reconstruction agent's REPL tools and the modeling
// agent's tools. Also finds the models and maps a step produced so the row
// can offer to show them in the viewer.
import {
  Activity,
  Box,
  ChartColumn,
  CloudUpload,
  CodeXml,
  Crosshair,
  Download,
  FilePen,
  FileText,
  FlipHorizontal2,
  Gauge,
  Hourglass,
  Image,
  Layers,
  ListChecks,
  RefreshCw,
  Scale,
  ScanSearch,
  Scissors,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  SquareTerminal,
  Wrench,
} from 'lucide-react'
import { api } from '../../lib/api'
import { basename } from '../../lib/format'
import { useApp } from '../../store/app'
import { viewerRequestForFile } from '../files/fileTypes'

const base = (path) => (path ? basename(String(path)) : '')

function firstLine(text, limit = 160) {
  const line = String(text || '')
    .split('\n')
    .map((entry) => entry.trim())
    .find(Boolean)
  return line ? line.slice(0, limit) : ''
}

/** Reads the intent of a REPL cell: its leading comment, else a leading print, else its first line. */
export function summarizeCode(code) {
  const lines = String(code || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  const head = lines.slice(0, 3)
  const commentLine = head.find((line) => line.startsWith('#') && !line.startsWith('#!'))
  const comment = commentLine ? commentLine.replace(/^#+\s*/, '').trim() : ''
  let printed = ''
  for (const line of lines.slice(0, 5)) {
    const match = /^print\(\s*f?(['"])(.+?)\1/.exec(line)
    // Only plain sentences: skip f-string placeholders and code-like text.
    if (match && match[2].includes(' ') && match[2].length >= 12 && !/[{}()[\]=\\]/.test(match[2])) {
      printed = match[2].replace(/[\s:.-]+$/, '').trim()
      break
    }
  }
  return {
    comment: comment.length > 3 ? comment.slice(0, 220) : '',
    printed,
    firstLine: lines[0] ? lines[0].slice(0, 160) : '',
  }
}

function jobUids(text) {
  const found = new Set()
  for (const match of String(text || '').slice(0, 600).matchAll(/\bJ\d+\b/g)) found.add(match[0])
  return [...found]
}

// `text` describes a finished step; `runningText` the same step while it runs.
const MODELING_TOOLS = {
  run_daq: { icon: Activity, text: 'Scored with DAQ', runningText: 'Scoring with DAQ', target: (a) => base(a.model_path) },
  render_pdb: { icon: Box, text: 'Rendered', runningText: 'Rendering', target: (a) => base(a.pdb_path) },
  flip_map: { icon: FlipHorizontal2, text: 'Flipped the map hand', runningText: 'Flipping the map hand', target: (a) => base(a.map_path) },
  align_model_to_map: { icon: Crosshair, text: 'Fitted into the map', runningText: 'Fitting into the map', target: (a) => base(a.model_path) },
  search_pdb_homologs: {
    icon: Search,
    text: 'Searched the PDB for homologs',
    runningText: 'Searching the PDB for homologs',
    target: (a) => base(a.protein_fasta_path),
  },
  download_pdb_homolog: { icon: Download, text: 'Downloaded homolog', runningText: 'Downloading homolog', target: (a) => a.entity_id },
  run_qscore: { icon: Gauge, text: 'Computed Q-scores', runningText: 'Computing Q-scores', target: (a) => base(a.model_path) },
  compare_map_hands: {
    icon: Scale,
    text: 'Compared map hands',
    runningText: 'Comparing map hands',
    target: (a) => (a.metric === 'qscore' ? 'by Q-score' : 'by DAQ'),
  },
  render_map_views: { icon: Layers, text: 'Rendered contour views', runningText: 'Rendering contour views', target: (a) => base(a.map_path) },
  render_map_model: { icon: ScanSearch, text: 'Rendered model in density', runningText: 'Rendering model in density', target: (a) => base(a.model_path) },
  residue_support: { icon: ListChecks, text: 'Checked residue support', runningText: 'Checking residue support', target: (a) => base(a.model_path) },
  edit_model: { icon: Scissors, text: 'Edited model', runningText: 'Editing model', target: (a) => base(a.output_path) },
  compare_daq_stages: { icon: ChartColumn, text: 'Compared DAQ between versions', runningText: 'Comparing DAQ between versions' },
  validate_model_phenix: { icon: ShieldCheck, text: 'Validated with Phenix', runningText: 'Validating with Phenix', target: (a) => base(a.model_path) },
  refine_phenix: { icon: SlidersHorizontal, text: 'Refined with Phenix', runningText: 'Refining with Phenix', target: (a) => base(a.model_path) },
  refine_rosetta: { icon: SlidersHorizontal, text: 'Relaxed with Rosetta', runningText: 'Relaxing with Rosetta', target: (a) => base(a.model_path) },
  submit_cryozeta: { icon: CloudUpload, text: 'Submitted to CryoZeta', runningText: 'Submitting to CryoZeta', target: (a) => base(a.map_path) },
  check_cryozeta_job: { icon: RefreshCw, text: 'Checked CryoZeta job', runningText: 'Checking CryoZeta job', target: (a) => a.job_id },
  wait_for_cryozeta: {
    icon: Hourglass,
    text: 'Waited for CryoZeta',
    runningText: 'Waiting for CryoZeta',
    hint: 'CryoZeta jobs take one to several hours. The agent checks the job every 30 minutes.',
    target: (a) => a.job_id,
  },
  download_cryozeta_result: {
    icon: Download,
    text: 'Downloaded CryoZeta model',
    runningText: 'Downloading CryoZeta model',
    target: (a) => a.job_id,
  },
  read_file: { icon: FileText, text: 'Read', runningText: 'Reading', target: (a) => base(a.path) },
  write_file: { icon: FilePen, text: 'Wrote', runningText: 'Writing', target: (a) => base(a.path) },
  bash: { icon: SquareTerminal, text: 'Ran', runningText: 'Running', target: (a) => firstLine(a.command), mono: true },
  grep: { icon: Search, text: 'Searched for', runningText: 'Searching for', target: (a) => a.pattern, mono: true },
  view_image: { icon: Image, text: 'Viewed', runningText: 'Viewing', target: (a) => base(a.path) },
}

function humanize(name) {
  const text = String(name || 'tool').replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function firstStringArgument(args) {
  if (!args || typeof args !== 'object') return ''
  const value = Object.values(args).find((entry) => typeof entry === 'string' && entry.trim())
  return value ? firstLine(value, 120) : ''
}

/**
 * {icon, text, target, mono, hint} for a step. `text` is the verb phrase (or
 * the whole description for REPL cells), `target` the object it acted on.
 */
export function describeStep(step, { running = false } = {}) {
  const args = step.args && typeof step.args === 'object' ? step.args : {}
  if (step.agent === 'reconstruction') {
    if (step.name === 'run_code' || step.name === 'run_and_wait') {
      const waits = step.name === 'run_and_wait'
      const { comment, printed, firstLine: line } = summarizeCode(args.code)
      const jobs = waits ? jobUids(`${args.condition || ''}\n${args.code || ''}`) : []
      const described = comment || printed || (jobs.length ? `Waited for ${jobs.slice(0, 3).join(', ')}` : '')
      if (described) return { icon: waits ? Hourglass : CodeXml, text: described.charAt(0).toUpperCase() + described.slice(1) }
      const text = waits ? (running ? 'Running and waiting' : 'Ran and waited') : running ? 'Running Python' : 'Ran Python'
      return { icon: waits ? Hourglass : CodeXml, text, target: line, mono: true }
    }
    if (step.name === 'view_image') {
      return { icon: Image, text: running ? 'Viewing' : 'Viewed', target: base(args.image_path), mono: true }
    }
    return { icon: Wrench, text: humanize(step.name), target: firstStringArgument(args), mono: true }
  }
  const known = MODELING_TOOLS[step.name]
  if (!known) return { icon: Wrench, text: humanize(step.name), target: firstStringArgument(args), mono: true }
  const target = known.target ? String(known.target(args) || '') : ''
  return {
    icon: known.icon,
    text: running && known.runningText ? known.runningText : known.text,
    target,
    // File names and commands read better in mono; job ids and phrases do not.
    mono: Boolean(known.mono || /\.\w{1,6}$/.test(target)),
    hint: running ? known.hint : undefined,
  }
}

// ------------------------------------------------------------------ paths
const STRUCTURE_RE = /\.(pdb|ent|cif|mmcif)$/i
const VOLUME_RE = /\.(mrc|map|ccp4)$/i
const VIEWABLE_IN_TEXT = /[^\s"'(),[\]{}<>`]+?\.(?:pdb|ent|cif|mmcif|mrc|map|ccp4)(?![\w]|\.\w)/gi
const INPUT_KEYS = ['model_path', 'map_path', 'pdb_path', 'path', 'protein_fasta_path', 'original_scores_path', 'flipped_scores_path']
/** Tools whose inputs are worth opening in 3D when they wrote no model or map of their own. */
const VISUAL_TOOLS = new Set(['render_pdb', 'render_map_views', 'render_map_model'])
const READ_ONLY_TOOLS = new Set(['read_file', 'grep', 'view_image', 'check_cryozeta_job', 'wait_for_cryozeta'])

/**
 * The modeling agent's paths are relative to its root ("workspaces/<slug>/...")
 * or absolute inside it. Returns the path relative to the project's modeling
 * workspace, or null when the file lies outside it.
 */
export function workspaceRelative(path, workspace) {
  if (!path || !workspace) return null
  const marker = `${String(workspace).replace(/^\.?\/+|\/+$/g, '')}/`
  const clean = String(path).replace(/^\.\//, '')
  if (clean.startsWith(marker)) return clean.slice(marker.length) || null
  const index = clean.indexOf(`/${marker}`)
  if (index === -1) return null
  return clean.slice(index + marker.length + 1) || null
}

export function viewerKind(path) {
  if (STRUCTURE_RE.test(path)) return 'structure'
  if (VOLUME_RE.test(path)) return 'volume'
  return null
}

/** Models and maps a modeling step wrote (or, for render tools, looked at), workspace-relative. */
export function viewerTargets(step, workspace) {
  if (step.agent !== 'modeling' || READ_ONLY_TOOLS.has(step.name) || !workspace) return []
  const args = step.args && typeof step.args === 'object' ? step.args : {}
  const inputs = new Set(
    INPUT_KEYS.map((key) => args[key])
      .filter((value) => typeof value === 'string')
      .map((value) => workspaceRelative(value, workspace) || value),
  )
  const found = []
  const add = (path) => {
    const rel = workspaceRelative(path, workspace)
    if (!rel || !viewerKind(rel) || found.includes(rel)) return
    found.push(rel)
  }
  const outputs = []
  if (typeof args.output_path === 'string') outputs.push(args.output_path)
  for (const match of String(step.result?.text || '').matchAll(VIEWABLE_IN_TEXT)) outputs.push(match[0])
  for (const path of outputs) {
    const rel = workspaceRelative(path, workspace)
    if (rel && !inputs.has(rel)) add(path)
  }
  if (!found.length && VISUAL_TOOLS.has(step.name)) {
    for (const key of ['model_path', 'pdb_path', 'map_path']) if (typeof args[key] === 'string') add(args[key])
  }
  return found.slice(0, 4)
}

/**
 * The DAQ score table a run_daq step wrote, relative to the modeling workspace
 * (the path the DAQ panel lists it under), or null when it lies outside it.
 * The tool prints its result as JSON with absolute paths; the driver may cut a
 * long result short, so the output folder in the arguments is the fallback.
 */
export function daqTablePath(step, workspace) {
  const match = /"scoreTable":\s*("(?:[^"\\]|\\.)*")/.exec(String(step.result?.text || ''))
  if (match) {
    try {
      const rel = workspaceRelative(JSON.parse(match[1]), workspace)
      if (rel) return rel
    } catch {
      // not a JSON string after all; use the arguments
    }
  }
  const args = step.args && typeof step.args === 'object' ? step.args : {}
  const folder = typeof args.output_dir === 'string' ? workspaceRelative(args.output_dir.replace(/\/+$/, ''), workspace) : null
  if (!folder) return null
  const candidate = Number(args.candidate_index) > 0 ? Number(args.candidate_index) : 1
  return `${folder}/candidate_${String(candidate).padStart(3, '0')}/daq_scores.csv`
}

/** Opens a model or map from the project's modeling workspace in Mol*, keyed like the Files tab keys it. */
export function showFileInViewer(projectId, rel) {
  if (!projectId || !viewerKind(rel)) return
  useApp.getState().showInViewer(viewerRequestForFile(api.fileUrl(projectId, 'modeling', rel), 'modeling', rel, basename(rel)))
}
