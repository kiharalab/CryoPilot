// DAQ results of the project's modeling workspace: pick one (newest first),
// read the per-residue profile of every chain, put the scores on the model in
// the viewer, and jump to the regions that fit the map poorly.
import { useEffect, useMemo, useState } from 'react'
import { Box, Check, ChevronDown, FlaskConical, Layers, RefreshCw } from 'lucide-react'
import { useApp } from '../../store/app'
import { cx } from '../../lib/cx'
import { formatRelative } from '../../lib/format'
import {
  Button,
  EmptyState,
  IconButton,
  InlineError,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Segmented,
  Skeleton,
} from '../../components/ui'
import { useLayerReady } from '../viewer/scene'
import { DaqProfile } from './DaqProfile'
import { formatScore, profileDomain } from './profileCanvas'
import { METRICS, currentResult, useDaq, useScores } from './store'
import {
  DAQ_MAP_KEY,
  DAQ_MODEL_KEY,
  focusResidue,
  mapPathInWorkspace,
  modelName,
  resultFolder,
  resultForPath,
  sameResult,
  showMap,
  showOnModel,
} from './actions'

const POLL_MS = 30000
const REGION_LIMIT = 60

const integer = new Intl.NumberFormat()

export function DaqPanel({ active }) {
  const projectId = useApp((state) => state.projectId)
  const project = useApp((state) => state.project)
  const workbenchOpen = useApp((state) => state.workbenchOpen)
  const focusPath = useApp((state) => state.daqFocusPath)
  const clearDaqFocus = useApp((state) => state.clearDaqFocus)
  const storeProject = useDaq((state) => state.projectId)
  const results = useDaq((state) => state.results)
  const loading = useDaq((state) => state.loading)
  const error = useDaq((state) => state.error)
  const selected = useDaq((state) => state.selected)
  const metric = useDaq((state) => state.metric)
  const loadResults = useDaq((state) => state.loadResults)
  const select = useDaq((state) => state.select)
  const ensureScores = useDaq((state) => state.ensureScores)
  const shown = active && workbenchOpen

  useEffect(() => {
    if (!shown || !projectId) return undefined
    loadResults(projectId)
    const timer = setInterval(() => loadResults(projectId), POLL_MS)
    return () => clearInterval(timer)
  }, [shown, projectId, loadResults])

  // "Open DAQ" in the conversation names a scores file: list the results again and pick it.
  useEffect(() => {
    if (!focusPath) return undefined
    if (!projectId) {
      clearDaqFocus()
      return undefined
    }
    let cancelled = false
    loadResults(projectId).then(() => {
      if (cancelled) return
      const match = resultForPath(useDaq.getState().results, focusPath)
      // The newest result is followed automatically, as when it is picked from the list.
      if (match) select(match === useDaq.getState().results[0] ? null : match.path)
      clearDaqFocus()
    })
    return () => {
      cancelled = true
    }
  }, [focusPath, projectId, loadResults, select, clearDaqFocus])

  const fresh = storeProject === projectId
  const result = fresh ? currentResult(results, selected) : null
  const entry = useScores(result)

  useEffect(() => {
    if (shown && projectId && result) ensureScores(projectId, result)
    // The result object changes on every refresh; its path and time identify it.
  }, [shown, projectId, result?.path, result?.modified, ensureScores])

  if (!projectId) {
    return (
      <EmptyState icon={FlaskConical} title="No project open">
        Open or create a project to see its DAQ results.
      </EmptyState>
    )
  }

  if (!fresh || results === null) {
    if (error && fresh) return <ResultsError error={error} onRetry={() => loadResults(projectId)} />
    return <PanelSkeleton />
  }

  if (!results.length) {
    return (
      <EmptyState
        icon={FlaskConical}
        title="No DAQ results yet"
        action={
          <Button size="sm" icon={RefreshCw} loading={loading} onClick={() => loadResults(projectId)}>
            Check again
          </Button>
        }
      >
        DAQ results appear here after the modeling agent runs DAQ on a model.
      </EmptyState>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-2">
        <ResultPicker results={results} current={result} />
        <IconButton icon={RefreshCw} label="Refresh" size="sm" disabled={loading} onClick={() => loadResults(projectId)} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error ? <InlineError className="mx-4 mt-3">{error}</InlineError> : null}
        {entry?.status === 'ready' ? (
          <ResultView projectId={projectId} project={project} result={result} profile={entry.profile} metric={metric} />
        ) : entry?.status === 'error' ? (
          <div className="p-4">
            <InlineError>{entry.error}</InlineError>
            <Button size="sm" icon={RefreshCw} className="mt-3" onClick={() => ensureScores(projectId, result)}>
              Try again
            </Button>
          </div>
        ) : (
          <PanelSkeleton bare />
        )}
      </div>
    </div>
  )
}

function ResultsError({ error, onRetry }) {
  return (
    <div className="p-4">
      <InlineError>{error}</InlineError>
      <Button size="sm" icon={RefreshCw} className="mt-3" onClick={onRetry}>
        Try again
      </Button>
    </div>
  )
}

function PanelSkeleton({ bare = false }) {
  return (
    <div className={cx('space-y-4 p-4', !bare && 'pt-14')} aria-hidden>
      <div className="grid grid-cols-3 gap-2">
        <Skeleton className="h-16" />
        <Skeleton className="h-16" />
        <Skeleton className="h-16" />
      </div>
      <Skeleton className="h-7 w-2/3" />
      <Skeleton className="h-28" />
      <Skeleton className="h-28" />
    </div>
  )
}

// ------------------------------------------------------------- picker
function ResultPicker({ results, current }) {
  const select = useDaq((state) => state.select)
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left hover:bg-hover"
          aria-label="Choose a DAQ result"
        >
          <span className="min-w-0 truncate text-sm font-medium text-fg">{modelName(current)}</span>
          <span className="min-w-0 truncate text-xs text-muted">{resultFolder(current)}</span>
          <ChevronDown size={14} strokeWidth={1.75} className="ml-auto shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(420px,calc(100vw-32px))] p-1">
        <ul className="max-h-80 overflow-y-auto" role="listbox" aria-label="DAQ results">
          {results.map((result, index) => {
            const chosen = sameResult(result, current)
            return (
              <li key={`${result.path}@${result.modified}`}>
                <button
                  type="button"
                  role="option"
                  aria-selected={chosen}
                  onClick={() => {
                    // The newest result is followed automatically until another one is picked.
                    select(index === 0 ? null : result.path)
                    setOpen(false)
                  }}
                  className={cx('flex w-full items-center gap-3 rounded-lg px-2.5 py-1.5 text-left hover:bg-hover', chosen && 'bg-hover')}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-fg">{modelName(result)}</span>
                    <span className="block truncate text-xs text-muted">{resultFolder(result) || 'Workspace folder'}</span>
                  </span>
                  <span className="shrink-0 text-xs text-faint">{formatRelative(result.modified)}</span>
                  <Check size={14} className={cx('shrink-0 text-accent', chosen ? 'opacity-100' : 'opacity-0')} />
                </button>
              </li>
            )
          })}
        </ul>
      </PopoverContent>
    </Popover>
  )
}

// --------------------------------------------------------------- result
function ResultView({ projectId, project, result, profile, metric }) {
  const setMetric = useDaq((state) => state.setMetric)
  const shown = useDaq((state) => state.shown)
  const shownMap = useDaq((state) => state.shownMap)
  const cursor = useDaq((state) => state.cursor)
  const modelOnScreen = useLayerReady(DAQ_MODEL_KEY)
  const mapOnScreen = useLayerReady(DAQ_MAP_KEY)
  const mapPath = mapPathInWorkspace(result, project)
  const domain = profileDomain(profile, metric)
  // The buttons keep saying what they do; a check marks what the viewer already shows.
  const onModel = modelOnScreen && sameResult(shown?.result, result) && shown?.metric === metric
  const mapShown = mapOnScreen && Boolean(mapPath) && shownMap === mapPath
  const resultCursor = cursor?.path === result.path ? cursor : null
  const pick = (residue, chain) => focusResidue(projectId, result, metric, residue, chain, { reveal: true })
  // One stable list per chain, so a refresh or a pick does not redraw every chart.
  const chainLists = useMemo(() => profile.chains.map((chain) => [chain]), [profile])

  return (
    <div className="space-y-5 px-4 pb-6 pt-4">
      <dl className="grid grid-cols-3 gap-2">
        <Stat label="Residues scored" value={integer.format(profile.scored)} detail={`of ${integer.format(profile.total)}`} />
        <Stat label="Mean DAQ" value={formatScore(profile.mean)} />
        <Stat label="Below zero" value={profile.belowFraction === null ? '–' : `${(profile.belowFraction * 100).toFixed(1)}%`} />
      </dl>

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" icon={onModel ? Check : Box} aria-pressed={onModel} onClick={() => showOnModel(projectId, result, metric)}>
          Show on model
        </Button>
        {mapPath ? (
          <Button size="sm" icon={mapShown ? Check : Layers} aria-pressed={mapShown} onClick={() => showMap(projectId, result, mapPath)} title={mapPath}>
            Show map
          </Button>
        ) : null}
        <Segmented
          size="sm"
          className="ml-auto"
          ariaLabel="Score"
          value={metric}
          onChange={setMetric}
          options={Object.entries(METRICS).map(([value, entry]) => ({ value, label: entry.label }))}
        />
      </div>

      {profile.chains.length ? (
        <div className="space-y-4">
          {profile.chains.map((chain, index) => (
            <section key={chain.id} aria-label={`Chain ${chain.id}`}>
              <div className="mb-1 flex items-baseline gap-2">
                <h3 className="text-sm font-medium text-fg">Chain {chain.id || '–'}</h3>
                <span className="text-xs tabular-nums text-muted">
                  {chain.start}–{chain.end}
                </span>
                {chain.below ? <span className="ml-auto text-xs text-muted">{integer.format(chain.below)} below zero</span> : null}
              </div>
              <DaqProfile
                chains={chainLists[index]}
                metric={metric}
                domain={domain}
                height={112}
                cursor={resultCursor}
                onPick={pick}
                label={`${METRICS[metric].label} DAQ along chain ${chain.id}. Use the arrow keys to read residues and Enter to show one in the viewer.`}
              />
            </section>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted">This result has no residues.</p>
      )}

      <Regions regions={profile.regions} onPick={(region) => pick(region.lowest, region.chain)} />
    </div>
  )
}

function Stat({ label, value, detail }) {
  return (
    <div className="min-w-0 rounded-xl border border-line px-3 py-2.5">
      <dt className="truncate text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 flex items-baseline gap-1.5">
        <span className="text-lg font-semibold text-fg">{value}</span>
        {detail ? <span className="truncate text-xs text-muted">{detail}</span> : null}
      </dd>
    </div>
  )
}

/** Runs of residues whose 19-residue mean is below zero: where the model fits the map worst. */
function Regions({ regions, onPick }) {
  const [all, setAll] = useState(false)
  const sorted = [...regions].sort((a, b) => a.min - b.min)
  const visible = all ? sorted : sorted.slice(0, REGION_LIMIT)
  return (
    <section aria-label="Regions below zero">
      <div className="mb-1.5 flex items-baseline gap-2">
        <h3 className="text-sm font-medium text-fg">Regions below zero</h3>
        <span className="text-xs text-muted">{regions.length ? `${integer.format(regions.length)}, lowest first` : null}</span>
      </div>
      {regions.length ? (
        <>
          <ul className="overflow-hidden rounded-xl border border-line">
            {visible.map((region) => (
              <li key={`${region.chain}:${region.start}`} className="border-b border-line last:border-b-0">
                <button
                  type="button"
                  onClick={() => onPick(region)}
                  className="flex h-8 w-full items-center gap-3 px-3 text-left text-sm hover:bg-hover"
                  title="Show the lowest residue in the viewer"
                >
                  <span className="w-28 shrink-0 tabular-nums text-fg">
                    {region.chain} {region.start}
                    {region.end !== region.start ? `–${region.end}` : ''}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-xs text-muted">
                    {region.length === 1 ? '1 residue' : `${integer.format(region.length)} residues`}
                  </span>
                  <span className="shrink-0 tabular-nums text-fg">{formatScore(region.min)}</span>
                </button>
              </li>
            ))}
          </ul>
          {sorted.length > visible.length ? (
            <Button variant="ghost" size="sm" className="mt-1.5" onClick={() => setAll(true)}>
              Show all {integer.format(sorted.length)}
            </Button>
          ) : null}
        </>
      ) : (
        <p className="text-sm text-muted">No region falls below zero.</p>
      )}
    </section>
  )
}
