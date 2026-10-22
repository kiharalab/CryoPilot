// DAQ state shared by the DAQ panel and the profile docked under the viewer:
// the project's DAQ results, the per-residue scores of the ones looked at, the
// result shown on the model in Mol* (and its map), and the residue last picked.
import { create } from 'zustand'
import { api } from '../../lib/api'
import { toDate } from '../../lib/format'

/** The modeling agent judges fit by the mean AA score over 19 residues. */
export const DAQ_WINDOW = 19

export const METRICS = {
  aa_mean: { label: '19-residue mean', short: 'Mean' },
  aa: { label: 'Raw AA', short: 'Raw AA' },
}

export function residueValue(residue, metric) {
  const value = residue?.[metric]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function sortedResults(results) {
  return [...results].sort((a, b) => (toDate(b.modified)?.getTime() || 0) - (toDate(a.modified)?.getTime() || 0))
}

function cacheKey(result) {
  return `${result.path}@${result.modified ?? ''}`
}

/**
 * Turns the scores response into what the charts draw: residues grouped by
 * chain in file order, summary numbers, and the runs of residues whose
 * 19-residue mean falls below zero.
 */
export function buildProfile(data) {
  const residues = Array.isArray(data?.residues) ? data.residues : []
  const byChain = new Map()
  const declared = Array.isArray(data?.chains) ? data.chains.map((chain) => (typeof chain === 'string' ? chain : chain?.id ?? chain?.chain)) : []
  for (const id of declared) if (id !== undefined && id !== null) byChain.set(String(id), [])
  for (const residue of residues) {
    const id = String(residue.chain ?? '')
    if (!byChain.has(id)) byChain.set(id, [])
    byChain.get(id).push({ ...residue, resnum: Number(residue.resnum) })
  }

  let scored = 0
  let meanSum = 0
  let meanCount = 0
  let below = 0
  const extent = { aa_mean: 0, aa: 0 }
  const chains = []
  const regions = []
  for (const [id, list] of byChain) {
    if (!list.length) continue
    // Draw and search by residue number; the windowed mean was already computed in file order.
    list.sort((a, b) => a.resnum - b.resnum || String(a.icode || '').localeCompare(String(b.icode || '')))
    let chainBelow = 0
    let run = null
    const closeRun = () => {
      if (run) regions.push(run)
      run = null
    }
    list.forEach((residue, index) => {
      if (residue.scored) scored += 1
      for (const metric of Object.keys(extent)) {
        const value = residueValue(residue, metric)
        if (value !== null) extent[metric] = Math.max(extent[metric], Math.abs(value))
      }
      // Summary numbers and regions count scored residues only.
      const mean = residue.scored ? residueValue(residue, 'aa_mean') : null
      const previous = list[index - 1]
      const contiguous = previous && residue.resnum - previous.resnum <= 1
      if (mean === null) {
        closeRun()
        return
      }
      meanSum += mean
      meanCount += 1
      if (mean < 0) {
        below += 1
        chainBelow += 1
        if (run && contiguous) {
          run.end = residue.resnum
          run.length += 1
          if (mean < run.min) {
            run.min = mean
            run.lowest = residue
          }
        } else {
          closeRun()
          run = { chain: id, start: residue.resnum, end: residue.resnum, length: 1, min: mean, lowest: residue }
        }
      } else {
        closeRun()
      }
    })
    closeRun()
    chains.push({ id, residues: list, start: list[0].resnum, end: list[list.length - 1].resnum, below: chainBelow })
  }

  return {
    chains,
    regions,
    total: residues.length,
    scored,
    mean: meanCount ? meanSum / meanCount : null,
    belowFraction: meanCount ? below / meanCount : null,
    extent,
  }
}

/** The result the panel shows: the one picked, else the newest. */
export function currentResult(results, selected) {
  if (!results?.length) return null
  return results.find((result) => result.path === selected) || results[0]
}

const initialProject = {
  results: null,
  loading: false,
  error: null,
  selected: null,
  scores: {},
  shown: null,
  shownMap: null,
  cursor: null,
}

export const useDaq = create((set, get) => ({
  projectId: null,
  ...initialProject,
  /** 'aa_mean' | 'aa' */
  metric: 'aa_mean',
  dockHidden: false,

  /** Loads (or refreshes) the project's DAQ results, newest first. */
  async loadResults(projectId) {
    if (!projectId) return
    if (get().projectId !== projectId) set({ projectId, ...initialProject })
    set({ loading: true })
    try {
      const { results = [] } = await api.daqResults(projectId)
      if (get().projectId !== projectId) return
      set({ results: sortedResults(results), error: null })
    } catch (err) {
      if (get().projectId === projectId) set({ error: err.message || 'Could not list DAQ results.' })
    } finally {
      if (get().projectId === projectId) set({ loading: false })
    }
  },

  select(path) {
    set({ selected: path })
  },

  setMetric(metric) {
    set({ metric })
  },

  /** Scores for one result; cached per result and file time. */
  async ensureScores(projectId, result) {
    if (!projectId || !result) return null
    const key = cacheKey(result)
    const existing = get().scores[key]
    if (existing && existing.status !== 'error') return existing
    const patch = (entry) => set((state) => (state.projectId === projectId ? { scores: { ...state.scores, [key]: entry } } : {}))
    patch({ status: 'loading' })
    try {
      const data = await api.daqScores(projectId, result.path, { window: DAQ_WINDOW })
      const entry = { status: 'ready', profile: buildProfile(data) }
      patch(entry)
      return entry
    } catch (err) {
      const entry = { status: 'error', error: err.message || 'Could not read the DAQ scores.' }
      patch(entry)
      return entry
    }
  },

  /** Marks which result (and metric) colours the model in the viewer. */
  setShown(result, metric) {
    set({ shown: result ? { result, metric } : null, dockHidden: false })
  },

  /** Marks which map (workspace path) Show map put in the viewer. */
  setShownMap(path) {
    set({ shownMap: path || null })
  },

  setCursor(cursor) {
    set({ cursor })
  },

  hideDock(hidden = true) {
    set({ dockHidden: hidden })
  },
}))

/** Scores entry for a result, re-rendering when it loads. */
export function useScores(result) {
  return useDaq((state) => (result ? state.scores[cacheKey(result)] || null : null))
}
