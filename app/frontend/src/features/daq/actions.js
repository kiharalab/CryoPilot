// What the DAQ views ask of the rest of the app: put the scored model (and
// its map) in the viewer, and focus a residue there.
import { api } from '../../lib/api'
import { basename } from '../../lib/format'
import { useApp } from '../../store/app'
import { DAQ_DOMAIN, MAP_NEUTRAL, colorToHex } from '../viewer/molstar'
import { useViewerScene } from '../viewer/scene'
import { DAQ_WINDOW, useDaq } from './store'

export const DAQ_MODEL_KEY = 'daq-model'
export const DAQ_MAP_KEY = 'daq-map'

export function modelName(result) {
  return basename(result?.model_path || '') || 'Model'
}

/** The folder of a result's daq_scores.csv, relative to the modeling workspace. */
export function resultFolder(result) {
  const parts = String(result?.path || '').split('/')
  parts.pop()
  return parts.join('/')
}

/**
 * The result a scores path names (newest first in `results`): that file, else
 * the newest result in its folder, else the newest in the run's output folder
 * when the path names a candidate folder DAQ did not write.
 */
export function resultForPath(results, path) {
  if (!results?.length || !path) return null
  const exact = results.find((result) => result.path === path)
  if (exact) return exact
  const folder = resultFolder({ path })
  const inFolder = results.find((result) => resultFolder(result) === folder)
  if (inFolder) return inFolder
  // run_daq writes each scored model to a candidate_NNN folder under its output folder.
  const run = /^(.+)\/candidate_\d+$/.exec(folder)?.[1]
  return run ? results.find((result) => resultFolder(result).startsWith(`${run}/`)) || null : null
}

/** Same scores file at the same modification time. */
export function sameResult(a, b) {
  return Boolean(a && b && a.path === b.path && (a.modified ?? null) === (b.modified ?? null))
}

/**
 * The scored map as a path inside the modeling workspace, or null when the
 * metadata names a map somewhere else (it then cannot be served).
 */
export function mapPathInWorkspace(result, project) {
  const raw = result?.map_path
  if (!raw) return null
  const path = String(raw).replace(/\\/g, '/')
  let relative = null
  if (!path.startsWith('/')) {
    // Relative to the modeling agent root ("workspaces/<slug>/...") or already to the workspace.
    const match = /^workspaces\/[^/]+\/(.+)$/.exec(path)
    relative = match ? match[1] : path.replace(/^\.\//, '')
  } else {
    const root = String(project?.directories?.modeling || '').replace(/\/+$/, '')
    if (root && path.startsWith(`${root}/`)) relative = path.slice(root.length + 1)
    else {
      const slug = project?.slug || root.split('/').pop()
      const marker = slug ? `/workspaces/${slug}/` : null
      const index = marker ? path.indexOf(marker) : -1
      if (index >= 0) relative = path.slice(index + marker.length)
    }
  }
  if (!relative || relative.split('/').includes('..')) return null
  return relative
}

/** Shows the model with the chosen DAQ score in its B-factor column, coloured red to blue. */
export function showOnModel(projectId, result, metric) {
  useApp.getState().showInViewer({
    kind: 'structure',
    key: DAQ_MODEL_KEY,
    url: api.daqStructureUrl(projectId, result.path, { metric, window: DAQ_WINDOW }),
    label: `${modelName(result)} DAQ`,
    format: 'pdb',
    color: 'daq',
    daqDomain: DAQ_DOMAIN,
  })
  useDaq.getState().setShown(result, metric)
}

/** Shows the map DAQ scored against, in grey so the model colours read, at the contour DAQ used. */
export function showMap(projectId, result, relativePath) {
  const contour = Number(result?.contour)
  useApp.getState().showInViewer({
    kind: 'volume',
    key: DAQ_MAP_KEY,
    url: api.fileUrl(projectId, 'modeling', relativePath, { preview: true }),
    label: basename(relativePath),
    color: colorToHex(MAP_NEUTRAL.value),
    ...(Number.isFinite(contour) && contour > 0 ? { level: contour } : {}),
  })
  useDaq.getState().setShownMap(relativePath)
}

/**
 * Focuses a residue of a result in the viewer, loading the scored model first
 * when the viewer does not hold it. `reveal` brings the viewer tab forward.
 */
export function focusResidue(projectId, result, metric, residue, chain, { reveal = false } = {}) {
  const app = useApp.getState()
  const daq = useDaq.getState()
  daq.setCursor({ path: result.path, chain, resnum: residue.resnum, icode: residue.icode || '' })
  const loaded = useViewerScene.getState().layers.some((layer) => layer.key === DAQ_MODEL_KEY)
  if (!loaded || !sameResult(daq.shown?.result, result)) {
    showOnModel(projectId, result, sameResult(daq.shown?.result, result) ? daq.shown.metric : metric)
  }
  app.showInViewer({ kind: 'focus', key: DAQ_MODEL_KEY, chain, resnum: residue.resnum, icode: residue.icode || undefined })
  if (reveal) app.setWorkbenchTab('viewer')
}

