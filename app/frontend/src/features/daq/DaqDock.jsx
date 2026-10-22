// The DAQ profile docked under the 3D view while the scored model is shown,
// so the scores and the model are read together: hover a residue to read it,
// click to fly to it.
import { useEffect } from 'react'
import { ChartSpline, X } from 'lucide-react'
import { useApp } from '../../store/app'
import { IconButton, Skeleton } from '../../components/ui'
import { DaqProfile } from './DaqProfile'
import { profileDomain } from './profileCanvas'
import { METRICS, useDaq, useScores } from './store'
import { focusResidue, modelName } from './actions'

export function DaqDock() {
  const projectId = useDaq((state) => state.projectId)
  const shown = useDaq((state) => state.shown)
  const hidden = useDaq((state) => state.dockHidden)
  const cursor = useDaq((state) => state.cursor)
  const ensureScores = useDaq((state) => state.ensureScores)
  const hideDock = useDaq((state) => state.hideDock)
  const setWorkbenchTab = useApp((state) => state.setWorkbenchTab)
  const entry = useScores(shown?.result)

  useEffect(() => {
    if (shown && projectId) ensureScores(projectId, shown.result)
  }, [shown, projectId, ensureScores])

  if (!shown || hidden) return null
  const { result, metric } = shown
  const profile = entry?.status === 'ready' ? entry.profile : null

  return (
    <section aria-label="DAQ profile" className="relative shrink-0 border-t border-line bg-canvas px-1 pb-1.5">
      <div className="flex h-8 items-center gap-2 pl-2 pr-0.5">
        <span className="shrink-0 text-xs font-medium text-fg">DAQ</span>
        <span className="min-w-0 truncate text-xs text-muted" title={result.path}>
          {modelName(result)}
        </span>
        <span className="shrink-0 text-xs text-faint">{METRICS[metric]?.label}</span>
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <IconButton icon={ChartSpline} label="Open the DAQ panel" size="xs" side="top" onClick={() => setWorkbenchTab('daq')} />
          <IconButton icon={X} label="Hide the DAQ profile" size="xs" side="top" onClick={() => hideDock(true)} />
        </div>
      </div>
      {profile ? (
        profile.chains.length ? (
          <DaqProfile
            compact
            chains={profile.chains}
            metric={metric}
            domain={profileDomain(profile, metric)}
            height={64}
            cursor={cursor?.path === result.path ? cursor : null}
            onPick={(residue, chain) => focusResidue(projectId, result, metric, residue, chain)}
            label={`DAQ profile of ${modelName(result)}. Use the arrow keys to read residues and Enter to focus one.`}
          />
        ) : (
          <p className="px-2 pb-2 text-xs text-muted">This result has no scored residues.</p>
        )
      ) : entry?.status === 'error' ? (
        <p className="px-2 pb-2 text-xs text-danger">{entry.error}</p>
      ) : (
        <Skeleton className="mx-2 mb-1 h-14" />
      )}
    </section>
  )
}
