// Mol* in the workbench. The viewer starts the first time the panel is shown
// with a size, then stays alive while hidden so a scene survives tab
// switches. Maps and models arrive as requests through the store
// (showInViewer) and become keyed layers listed over the canvas.
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { Box, Camera, ChartSpline, ChevronDown, CircleAlert, Eye, EyeOff, Globe, Layers, Scan, SlidersHorizontal, Trash2, X } from 'lucide-react'
import { useApp } from '../../store/app'
import { cx } from '../../lib/cx'
import {
  Button,
  ConfirmDialog,
  EmptyState,
  Field,
  IconButton,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Segmented,
  Spinner,
  Tip,
} from '../../components/ui'
import { DaqDock } from '../daq/DaqDock'
import { DAQ_MODEL_KEY } from '../daq/actions'
import { useDaq } from '../daq/store'
import { MAP_COLORS, MAP_NEUTRAL, colorToHex, createController, createViewer, entryRequest, parseEntryId, readCssVariable } from './molstar'
import { publishLayers } from './scene'
import './viewer.css'

const EMPTY_SNAPSHOT = { layers: [], activity: null, controls: false }
const subscribeNothing = () => () => {}
const emptySnapshot = () => EMPTY_SNAPSHOT

function errorToast(err) {
  return { title: err.title || err.message || 'The viewer hit an error', description: err.detail || undefined, tone: 'error' }
}

/** The start-up error as a sentence, with the WebGL advice only when WebGL is the cause. */
function startFailure(message) {
  const text = String(message || '').trim() || 'Reload the page to try again.'
  const sentence = `${text.charAt(0).toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`
  return /webgl|context/i.test(text) ? `${sentence} Check that WebGL is turned on in this browser, then reload the page.` : sentence
}

export function ViewerPanel({ active }) {
  const frameRef = useRef(null)
  const hostRef = useRef(null)
  const viewerRef = useRef(null)
  const controllerRef = useRef(null)
  const startedRef = useRef(false)
  const unmountedRef = useRef(false)
  const [controller, setController] = useState(null)
  const [phase, setPhase] = useState('idle')
  const [failure, setFailure] = useState('')
  const [hasSize, setHasSize] = useState(false)
  const [entryOpen, setEntryOpen] = useState(false)
  const theme = useApp((state) => state.resolvedTheme)
  const requests = useApp((state) => state.viewerRequests)
  const toast = useApp((state) => state.toast)
  const setWorkbenchTab = useApp((state) => state.setWorkbenchTab)

  const snapshot = useSyncExternalStore(controller ? controller.subscribe : subscribeNothing, controller ? controller.getSnapshot : emptySnapshot)
  const { layers, activity, controls } = snapshot

  useEffect(() => {
    unmountedRef.current = false
    return () => {
      unmountedRef.current = true
      controllerRef.current?.dispose()
      controllerRef.current = null
      try {
        viewerRef.current?.dispose()
      } catch {
        // Mol* may already be gone with its host element
      }
      viewerRef.current = null
      publishLayers([])
    }
  }, [])

  // Track the frame's size: start Mol* once it has one, and keep the canvas fitted.
  useEffect(() => {
    const element = frameRef.current
    let frame = 0
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setHasSize(width > 0 && height > 0)
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => viewerRef.current?.handleResize())
    })
    observer.observe(element)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [])

  useEffect(() => {
    if (startedRef.current || !active || !hasSize) return
    startedRef.current = true
    setPhase('starting')
    createViewer(hostRef.current, { background: readCssVariable('--viewer-bg') || undefined })
      .then((viewer) => {
        if (unmountedRef.current) {
          viewer.dispose()
          return
        }
        viewerRef.current = viewer
        const created = createController(viewer.plugin, window.molstar.lib)
        controllerRef.current = created
        setController(created)
        setPhase('ready')
      })
      .catch((err) => {
        if (unmountedRef.current) return
        setFailure(err?.message || String(err))
        setPhase('error')
      })
  }, [active, hasSize])

  useEffect(() => {
    controller?.setBackground(readCssVariable('--viewer-bg'))
  }, [controller, theme])

  // Requests wait in the store until the viewer is ready, then run in order.
  useEffect(() => {
    if (!controller || !requests.length) return
    for (const request of useApp.getState().takeViewerRequests()) {
      controller.run(request).catch((err) => toast(errorToast(err)))
    }
  }, [controller, requests, toast])

  useEffect(() => {
    publishLayers(layers)
  }, [layers])

  const daqModelReady = layers.some((layer) => layer.key === DAQ_MODEL_KEY && layer.status === 'ready')
  const run = (promise) => Promise.resolve(promise).catch((err) => toast(errorToast(err)))

  return (
    <div className="@container flex h-full min-h-0 flex-col">
      {controls ? (
        <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line pl-3 pr-1.5">
          <SlidersHorizontal size={14} strokeWidth={1.75} className="text-muted" />
          <span className="text-sm text-muted">Mol* controls</span>
          <Button size="sm" variant="ghost" className="ml-auto" onClick={() => controller.setControls(false)}>
            Done
          </Button>
        </div>
      ) : null}
      <div ref={frameRef} className="relative isolate min-h-0 flex-1 overflow-hidden bg-[var(--viewer-bg)]">
        <div ref={hostRef} className="molstar-host" data-chrome={controls ? 'full' : 'simple'} />
        {phase === 'ready' && !controls ? (
          <>
            <LayerList layers={layers} controller={controller} onError={(err) => toast(errorToast(err))} />
            <Toolbar
              layers={layers}
              controller={controller}
              entryOpen={entryOpen}
              setEntryOpen={setEntryOpen}
              run={run}
            />
            {!layers.length && !activity ? (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center px-8">
                <p className="pointer-events-auto max-w-xs text-center text-sm text-muted">
                  Open a map from <InlineLink onClick={() => setWorkbenchTab('maps')}>Maps</InlineLink>, a model from{' '}
                  <InlineLink onClick={() => setWorkbenchTab('files')}>Files</InlineLink>, or a{' '}
                  <InlineLink onClick={() => setEntryOpen(true)}>PDB or EMDB entry</InlineLink>.
                </p>
              </div>
            ) : null}
            {activity ? (
              <div
                role="status"
                className="pointer-events-none absolute bottom-3 left-3 z-10 flex max-w-[calc(100%-24px)] items-center gap-2 rounded-full border border-line bg-elevated/90 py-1 pl-2.5 pr-3 text-xs text-muted shadow-popover backdrop-blur-sm"
              >
                <Spinner size={12} />
                <span className="truncate">{activity}</span>
              </div>
            ) : null}
          </>
        ) : null}
        {(phase === 'starting' || phase === 'idle') && active && hasSize ? (
          <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted">
            <Spinner />
            Starting the viewer
          </div>
        ) : null}
        {phase === 'error' ? (
          <div className="absolute inset-0 bg-canvas">
            <EmptyState icon={CircleAlert} title="The 3D viewer could not start">
              {startFailure(failure)}
            </EmptyState>
          </div>
        ) : null}
      </div>
      {daqModelReady ? <DaqDock /> : null}
    </div>
  )
}

function InlineLink({ onClick, children }) {
  return (
    <button type="button" onClick={onClick} className="font-medium text-fg underline decoration-line-strong underline-offset-2 hover:decoration-fg">
      {children}
    </button>
  )
}

// ------------------------------------------------------------- toolbar
function Toolbar({ layers, controller, entryOpen, setEntryOpen, run }) {
  const [confirm, setConfirm] = useState(false)
  const empty = !layers.length
  const imageName = layers.find((layer) => layer.visible)?.label || 'cryopilot-view'
  return (
    <>
      <div className="absolute right-3 top-3 z-10 flex items-center gap-0.5 rounded-xl border border-line bg-elevated/90 p-0.5 shadow-popover backdrop-blur-sm">
        <OpenEntry open={entryOpen} onOpenChange={setEntryOpen} />
        <IconButton icon={Scan} label="Reset view" size="sm" disabled={empty} onClick={() => controller.resetCamera()} />
        <IconButton icon={Camera} label="Save image" size="sm" disabled={empty} onClick={() => run(controller.screenshot(imageName))} />
        <IconButton icon={Trash2} label="Clear viewer" size="sm" disabled={empty} onClick={() => setConfirm(true)} />
        <span aria-hidden className="mx-0.5 h-4 w-px bg-line" />
        <IconButton icon={SlidersHorizontal} label="Mol* controls" size="sm" onClick={() => controller.setControls(true)} />
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Clear the viewer?"
        description="Every map and model leaves the viewer. Their files are not touched."
        confirmLabel="Clear"
        onConfirm={() => {
          setConfirm(false)
          run(controller.clear())
        }}
      />
    </>
  )
}

function OpenEntry({ open, onOpenChange }) {
  const showInViewer = useApp((state) => state.showInViewer)
  const [text, setText] = useState('')
  const [error, setError] = useState('')

  const submit = (event) => {
    event.preventDefault()
    const entry = parseEntryId(text)
    if (!entry) {
      setError('Enter a PDB ID such as 8G3K or an EMDB ID such as EMD-29700.')
      return
    }
    showInViewer(entryRequest(entry))
    setText('')
    setError('')
    onOpenChange(false)
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) setError('')
      }}
    >
      <Tip label="Open a PDB or EMDB entry">
        <PopoverTrigger asChild>
          <IconButton icon={Globe} label="Open a PDB or EMDB entry" size="sm" tooltip={false} active={open} />
        </PopoverTrigger>
      </Tip>
      <PopoverContent align="end" className="w-72 p-3">
        <form onSubmit={submit} className="space-y-3">
          <Field label="PDB or EMDB ID" error={error}>
            {({ id }) => (
              <Input
                id={id}
                autoFocus
                mono
                value={text}
                invalid={Boolean(error)}
                placeholder="8G3K or EMD-29700"
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => {
                  setText(event.target.value)
                  if (error) setError('')
                }}
              />
            )}
          </Field>
          <div className="flex justify-end">
            <Button type="submit" variant="primary" size="sm" disabled={!text.trim()}>
              Open
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}

// -------------------------------------------------------------- layers
function LayerList({ layers, controller, onError }) {
  const [open, setOpen] = useState(true)
  if (!layers.length) return null
  // Beside the toolbar while that leaves room for the level slider; under it in a narrow panel.
  return (
    <div className="absolute left-3 top-3 z-10 flex max-h-[calc(100%-24px)] w-[276px] max-w-[calc(100%-196px)] flex-col overflow-hidden rounded-xl border border-line bg-elevated/90 shadow-popover backdrop-blur-sm @max-[480px]:top-[54px] @max-[480px]:max-h-[calc(100%-66px)] @max-[480px]:max-w-[calc(100%-24px)]">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex h-8 shrink-0 items-center gap-2 px-2.5 text-sm text-fg hover:bg-hover/60"
      >
        <Layers size={14} strokeWidth={1.75} className="text-muted" />
        <span className="font-medium">Layers</span>
        <span className="text-xs text-faint">{layers.length}</span>
        <ChevronDown size={14} strokeWidth={1.75} className={cx('ml-auto text-muted transition-transform', !open && '-rotate-90')} />
      </button>
      {open ? (
        <ul className="min-h-0 overflow-y-auto border-t border-line p-1">
          {layers.map((layer) => (
            <LayerRow key={layer.key} layer={layer} controller={controller} onError={onError} />
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function LayerRow({ layer, controller, onError }) {
  const dockHidden = useDaq((state) => state.dockHidden)
  const hideDock = useDaq((state) => state.hideDock)
  const ready = layer.status === 'ready'
  const safely = (promise) => Promise.resolve(promise).catch(onError)

  return (
    <li className="group rounded-lg px-1 py-0.5">
      <div className="flex h-7 items-center gap-1">
        <IconButton
          icon={layer.visible ? Eye : EyeOff}
          label={layer.visible ? 'Hide' : 'Show'}
          aria-label={`${layer.visible ? 'Hide' : 'Show'} ${layer.label}`}
          size="xs"
          disabled={!ready}
          onClick={() => safely(controller.setVisible(layer.key, !layer.visible))}
        />
        {layer.kind === 'volume' ? (
          <ColorSwatch layer={layer} controller={controller} />
        ) : (
          <span className="flex h-6 w-6 shrink-0 items-center justify-center text-muted">
            <Box size={13} strokeWidth={1.75} />
          </span>
        )}
        <span className={cx('min-w-0 flex-1 truncate text-sm', layer.visible ? 'text-fg' : 'text-faint')} title={layer.label}>
          {layer.label}
        </span>
        {layer.status === 'loading' ? <Spinner size={13} className="mx-1 shrink-0" /> : null}
        {layer.key === DAQ_MODEL_KEY && ready && dockHidden ? (
          <IconButton icon={ChartSpline} label="Show the DAQ profile" size="xs" onClick={() => hideDock(false)} />
        ) : null}
        <IconButton
          icon={X}
          label="Remove"
          aria-label={`Remove ${layer.label}`}
          size="xs"
          className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
          onClick={() => safely(controller.remove(layer.key))}
        />
      </div>
      {layer.kind === 'volume' && ready ? <VolumeControls layer={layer} controller={controller} /> : null}
      {layer.kind === 'structure' && layer.daq && ready ? (
        <div className="flex items-center gap-2 pb-1 pl-8 pr-1 pt-0.5">
          <Segmented
            size="sm"
            ariaLabel="Colour"
            value={layer.colorMode}
            onChange={(mode) => safely(controller.setStructureColor(layer.key, mode))}
            options={[
              { value: 'daq', label: 'DAQ' },
              { value: 'chain', label: 'Chain' },
            ]}
          />
          {layer.colorMode === 'daq' ? <DaqLegend domain={layer.daqDomain} /> : null}
        </div>
      ) : null}
    </li>
  )
}

/** The model's DAQ colour scale: red below zero, blue above. */
function DaqLegend({ domain }) {
  const [low, high] = domain
  return (
    <div role="img" className="min-w-0 flex-1" aria-label={`DAQ colour scale from ${low} (red) to ${high} (blue)`}>
      <div
        className="h-1.5 rounded-full"
        style={{ background: 'linear-gradient(90deg, var(--daq-neg), var(--daq-model-mid), var(--daq-pos))' }}
      />
      <div className="mt-0.5 flex justify-between text-2xs tabular-nums text-muted">
        <span>{low < 0 ? `−${Math.abs(low)}` : low}</span>
        <span>0</span>
        <span>{high}</span>
      </div>
    </div>
  )
}

function formatSigma(value) {
  if (!Number.isFinite(value)) return ''
  return Math.abs(value * 10 - Math.round(value * 10)) < 1e-6 ? value.toFixed(1) : value.toFixed(2)
}

function VolumeControls({ layer, controller }) {
  const id = useId()
  const [low, high] = layer.range || [0, 10]
  const absolute = layer.stats ? layer.stats.mean + layer.sigma * layer.stats.sigma : null
  return (
    <div className="grid grid-cols-[46px_minmax(0,1fr)_54px] items-center gap-x-2 pb-1 pl-8 pr-1">
      <label htmlFor={`${id}-level`} className="text-xs text-muted">
        Level
      </label>
      <input
        id={`${id}-level`}
        type="range"
        className="viewer-range w-full"
        min={low}
        max={high}
        step={0.1}
        value={layer.sigma}
        aria-valuetext={`${formatSigma(layer.sigma)} sigma`}
        onChange={(event) => controller.setVolume(layer.key, { sigma: Number(event.target.value) })}
      />
      <SigmaInput
        value={layer.sigma}
        title={absolute === null ? undefined : `Absolute level ${absolute.toPrecision(3)}`}
        onCommit={(sigma) => controller.setVolume(layer.key, { sigma })}
      />
      <label htmlFor={`${id}-opacity`} className="text-xs text-muted">
        Opacity
      </label>
      <input
        id={`${id}-opacity`}
        type="range"
        className="viewer-range w-full"
        min={0.05}
        max={1}
        step={0.05}
        value={layer.opacity}
        aria-valuetext={`${Math.round(layer.opacity * 100)} percent`}
        onChange={(event) => controller.setVolume(layer.key, { opacity: Number(event.target.value) })}
      />
      <span className="pr-1 text-right text-xs tabular-nums text-muted">{Math.round(layer.opacity * 100)}%</span>
    </div>
  )
}

/** The level as an editable number; Enter or leaving the field applies it. */
function SigmaInput({ value, onCommit, title }) {
  const [draft, setDraft] = useState(null)
  const commit = () => {
    if (draft === null) return
    const parsed = parseFloat(draft.replace('−', '-'))
    if (Number.isFinite(parsed)) onCommit(parsed)
    setDraft(null)
  }
  return (
    <label
      title={title}
      className="flex h-6 items-center justify-end gap-0.5 rounded-md border border-transparent px-1 text-xs tabular-nums text-fg hover:border-line focus-within:border-accent"
    >
      <input
        value={draft ?? formatSigma(value)}
        inputMode="decimal"
        aria-label="Level in sigma"
        className="w-9 bg-transparent text-right outline-none"
        onFocus={(event) => event.target.select()}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            commit()
            event.currentTarget.blur()
          } else if (event.key === 'Escape') {
            setDraft(null)
            event.currentTarget.blur()
          }
        }}
      />
      <span className="text-muted">σ</span>
    </label>
  )
}

function ColorSwatch({ layer, controller }) {
  const [open, setOpen] = useState(false)
  const options = [...MAP_COLORS, MAP_NEUTRAL]
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Colour of ${layer.label}`}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md hover:bg-hover"
        >
          <span className="h-3 w-3 rounded-full ring-1 ring-inset ring-fg/15" style={{ backgroundColor: colorToHex(layer.color) }} />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="p-1.5">
        <div role="radiogroup" aria-label="Map colour" className="grid grid-cols-4 gap-1">
          {options.map((option) => {
            const selected = option.value === layer.color
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                aria-label={option.name}
                title={option.name}
                onClick={() => {
                  controller.setVolume(layer.key, { color: option.value })
                  setOpen(false)
                }}
                className="flex h-7 w-7 items-center justify-center rounded-md hover:bg-hover"
              >
                <span
                  className={cx('h-4 w-4 rounded-full', selected && 'ring-2 ring-accent ring-offset-2 ring-offset-elevated')}
                  style={{ backgroundColor: colorToHex(option.value) }}
                />
              </button>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}
