// Mol* controller. The prebuilt viewer (window.molstar, loaded by index.html)
// does the rendering; this module drives it through viewer.plugin so the
// panel can treat maps and models as keyed layers: add, restyle, hide,
// focus, remove. Nothing here imports Mol* modules into the bundle; the few
// Mol* classes it needs come from `lib` (window.molstar.lib in the app).

/**
 * Map colours, assigned in this order to the maps of a scene. A colour stays
 * with its map; removing one map never repaints the others. Validated as a
 * categorical set against both viewer backgrounds.
 */
export const MAP_COLORS = [
  { name: 'Blue', value: 0x3987e5 },
  { name: 'Orange', value: 0xd95926 },
  { name: 'Teal', value: 0x199e70 },
  { name: 'Amber', value: 0xc98500 },
  { name: 'Pink', value: 0xd55181 },
  { name: 'Green', value: 0x008300 },
  { name: 'Violet', value: 0x9085e9 },
]
/** Used once every palette colour is taken, and for maps shown behind a coloured model. */
export const MAP_NEUTRAL = { name: 'Grey', value: 0x9aa1a9 }

/** B-factor range that the DAQ colouring spans: low scores red, high scores blue. */
export const DAQ_DOMAIN = [-1.5, 1.5]
const DAQ_FALLBACK = { low: 0xe34948, mid: 0xf0efec, high: 0x2a78d6 }

/** Mol* viewport buttons that appear with the full Mol* controls only. */
const VIEWPORT_FLAGS = ['ShowReset', 'ShowScreenshotControls', 'ShowControls', 'ShowSettings', 'ShowSelectionMode']

/** How a model's polymer is drawn: the Mol* representations for each style. */
export const MODEL_STYLES = {
  cartoon: ['cartoon'],
  sticks: ['ball-and-stick'],
  both: ['cartoon', 'ball-and-stick'],
}

const STRUCTURE_PARTS = [
  { component: 'polymer', type: 'cartoon', polymer: true },
  { component: 'ligand', type: 'ball-and-stick' },
  { component: 'non-standard', type: 'ball-and-stick' },
  { component: 'branched', type: 'carbohydrate' },
  { component: 'ion', type: 'ball-and-stick' },
  { component: 'lipid', type: 'ball-and-stick' },
]

/** Creates the Mol* viewer in `host` with its own chrome hidden. */
export async function createViewer(host, { background } = {}) {
  const molstar = window.molstar
  if (!molstar?.Viewer || !molstar.lib) {
    throw new Error('The Mol* bundle did not load. Rebuild the frontend and reload the page.')
  }
  const { PluginConfig } = molstar.lib.plugin
  return molstar.Viewer.create(host, {
    layoutIsExpanded: false,
    layoutShowControls: false,
    layoutShowRemoteState: false,
    layoutShowSequence: true,
    layoutShowLog: false,
    layoutShowLeftPanel: true,
    collapseLeftPanel: true,
    collapseRightPanel: false,
    viewportShowReset: false,
    viewportShowScreenshotControls: false,
    viewportShowControls: false,
    viewportShowSettings: false,
    viewportShowSelectionMode: false,
    viewportShowExpand: false,
    viewportShowToggleFullscreen: false,
    viewportShowAnimation: false,
    viewportShowTrajectoryControls: false,
    viewportBackgroundColor: background,
    volumeStreamingDisabled: true,
    pdbProvider: 'rcsb',
    emdbProvider: 'rcsb',
    config: [
      [PluginConfig.Viewport.ShowIllumination, false],
      [PluginConfig.Viewport.ShowXR, 'never'],
    ],
  })
}

/** "#f7f8f9" or a CSS custom property name -> 0xf7f8f9; null when unreadable. */
export function parseColor(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string') return null
  const text = value.startsWith('--') ? readCssVariable(value) : value.trim()
  const match = /^#?([0-9a-f]{6})$/i.exec(text || '')
  return match ? parseInt(match[1], 16) : null
}

export function readCssVariable(name) {
  if (typeof document === 'undefined' || !document.documentElement || typeof getComputedStyle !== 'function') return ''
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export function colorToHex(value) {
  return `#${(value >>> 0).toString(16).padStart(6, '0').slice(-6)}`
}

/** "8G3K" -> PDB entry, "EMD-29700" or "29700" -> EMDB entry, anything else -> null. */
export function parseEntryId(text) {
  const value = String(text || '').trim().toUpperCase()
  const emdb = /^(?:EMD[-_]?)(\d{4,6})$/.exec(value) || /^(\d{5,6})$/.exec(value)
  if (emdb) return { kind: 'emdb', id: `EMD-${emdb[1]}`, number: emdb[1] }
  if (/^[0-9][A-Z0-9]{3}$/.test(value)) return { kind: 'pdb', id: value }
  return null
}

/** The viewer request that opens a public PDB or EMDB entry. */
export function entryRequest(entry) {
  if (entry.kind === 'pdb') {
    return {
      kind: 'structure',
      key: `pdb:${entry.id}`,
      label: entry.id,
      url: `https://models.rcsb.org/${entry.id}.bcif`,
      format: 'mmcif',
      color: 'chain',
    }
  }
  return {
    kind: 'volume',
    key: `emdb:${entry.number}`,
    label: entry.id,
    url: `https://maps.rcsb.org/em/emd-${entry.number}/cell?detail=4`,
    format: 'dscif',
    entryId: entry.id,
  }
}

function fileName(request) {
  if (request.label) return request.label
  try {
    const url = new URL(request.url, 'http://localhost')
    const path = url.searchParams.get('path') || url.searchParams.get('name') || url.pathname
    return path.split('/').filter(Boolean).pop() || request.url
  } catch {
    return request.url
  }
}

/** The text the format is guessed from: the label plus the file path in the URL. */
function nameHints(request) {
  let path = ''
  try {
    const url = new URL(request.url, 'http://localhost')
    path = `${url.pathname} ${url.searchParams.get('path') || ''} ${url.searchParams.get('name') || ''}`
  } catch {
    path = request.url || ''
  }
  return `${request.label || ''} ${path}`.toLowerCase()
}

/** The format named by the request, else read from the name, else from the text: mmCIF starts with data_. */
function structureFormat(request, payload) {
  if (request.format) return request.format
  if (/\.(bcif|cif|mmcif)(\.gz)?\b/.test(nameHints(request))) return 'mmcif'
  if (typeof payload !== 'string') return 'mmcif'
  return /^\s*(#[^\n]*\n\s*)*data_/.test(payload.slice(0, 4096)) ? 'mmcif' : 'pdb'
}

/** An error the panel shows as a toast: `title` plus the reason as `detail`. */
function failure(title, cause) {
  const detail = cause?.message || (cause ? String(cause) : '')
  const error = new Error(detail ? `${title}: ${detail}` : title)
  error.title = title
  error.detail = detail
  return error
}

function round(value, digits = 1) {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value))
}

function defaultValues(params) {
  const values = {}
  for (const [name, param] of Object.entries(params || {})) {
    if (!param?.isOptional) values[name] = param?.defaultValue
  }
  return values
}

async function readBytes(url) {
  let response
  try {
    response = await fetch(url, { credentials: 'same-origin' })
  } catch (err) {
    const host = /^https?:/i.test(url) ? new URL(url).host : 'the CryoPilot server'
    throw new Error(`could not reach ${host} (${err.message})`)
  }
  if (!response.ok) {
    let message = `the server answered ${response.status}`
    try {
      const body = await response.clone().json()
      message = body.error || body.message || message
    } catch {
      // not JSON; keep the status line
    }
    throw new Error(message)
  }
  let bytes = new Uint8Array(await response.arrayBuffer())
  // Gzipped files (.mrc.gz, .pdb.gz) arrive compressed unless the server set Content-Encoding.
  if (bytes.length > 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))
    bytes = new Uint8Array(await new Response(stream).arrayBuffer())
  }
  return bytes
}

/**
 * Wraps a Mol* plugin. `lib` is window.molstar.lib (or the same shape built
 * from molstar/lib modules in tests).
 *
 * Layers are snapshots: {key, kind:'volume'|'structure', label, status, visible,
 *   volume:  color, sigma, opacity, stats:{min,max,mean,sigma}, range:[lo,hi]
 *   structure: colorMode:'chain'|'daq', style:'cartoon'|'sticks'|'both', daq (DAQ colouring available), daqDomain}
 */
export function createController(plugin, lib) {
  const { StateTransforms, PluginConfig } = lib.plugin
  const { Volume } = lib.volume
  const { StructureElement } = lib.structure

  const layers = new Map()
  const listeners = new Set()
  const subscriptions = []
  let snapshot = { layers: [], activity: null, controls: false }
  let activity = null
  let queue = Promise.resolve()
  let disposed = false
  // While the controller changes cell states itself, Mol*'s change events are not read back.
  let muted = 0

  // ------------------------------------------------------------- snapshot
  function publicView(layer) {
    const { refs, updating, dirty, applied, removing, opacityChosen, ...rest } = layer
    return { ...rest }
  }

  function emit() {
    if (disposed) return
    snapshot = {
      layers: [...layers.values()].map(publicView),
      activity,
      controls: Boolean(plugin.layout?.state?.showControls),
    }
    listeners.forEach((listener) => listener())
  }

  function setActivity(text) {
    activity = text
    emit()
  }

  // ---------------------------------------------------------------- state
  const data = () => plugin.state.data
  const cell = (ref) => (ref ? data().cells.get(ref) : undefined)

  async function deleteRefs(layer) {
    layer.removing = true
    const root = layer.refs?.root
    if (root && cell(root)) await plugin.build().delete(root).commit()
  }

  // A layer that replaces another loads beside it: the old one stays drawn until
  // the new one is ready, so a reload that fails (file moved, server error) keeps it.
  async function retireReplaced(previous) {
    if (previous) await deleteRefs(previous).catch((err) => console.error('Mol* could not remove a replaced layer', err))
  }

  /** Undoes a load that failed: its own cells go, and the layer it was replacing comes back. */
  async function abandonLoad(key, layer, previous) {
    if (layers.get(key) === layer) {
      if (previous) layers.set(key, previous)
      else layers.delete(key)
    }
    await deleteRefs(layer).catch(() => {})
  }

  function reprRefs(layer) {
    if (layer.kind === 'volume') return layer.refs.repr ? [layer.refs.repr] : []
    return layer.refs.reprs || []
  }

  function hasStructure() {
    return [...layers.values()].some((layer) => layer.kind === 'structure')
  }

  function nextMapColor() {
    const used = new Set([...layers.values()].filter((layer) => layer.kind === 'volume').map((layer) => layer.color))
    return (MAP_COLORS.find((color) => !used.has(color.value)) || MAP_NEUTRAL).value
  }

  function daqColors(request) {
    const given = request?.daqColors || {}
    return {
      low: parseColor(given.low) ?? parseColor('--daq-neg') ?? DAQ_FALLBACK.low,
      mid: parseColor(given.mid) ?? parseColor('--daq-model-mid') ?? DAQ_FALLBACK.mid,
      high: parseColor(given.high) ?? parseColor('--daq-pos') ?? DAQ_FALLBACK.high,
    }
  }

  function polymerColorTheme(layer) {
    if (layer.colorMode === 'daq') {
      const { low, mid, high } = layer.daqColors
      // The uncertainty theme reverses its list, so high comes first to put low scores in red.
      return { name: 'uncertainty', params: { domain: [...layer.daqDomain], list: { kind: 'interpolate', colors: [high, mid, low] } } }
    }
    return { name: 'chain-id', params: {} }
  }

  // ------------------------------------------------------------- volumes
  function recommendedSigma(volume) {
    try {
      const iso = volume._propertyData?.recommended_iso_value
      if (!iso) return null
      const value = iso.kind === 'relative' ? iso : Volume.adjustedIsoValue(volume, iso.absoluteValue, 'absolute')
      const relative = Volume.IsoValue.toRelative(value, volume.grid.stats).relativeValue
      return Number.isFinite(relative) ? round(relative, 2) : null
    } catch {
      return null
    }
  }

  /** The level that encloses about 1% of the voxels, in σ above the mean. */
  function automaticSigma(volume) {
    const { stats, cells } = volume.grid
    const values = cells?.data
    if (!values?.length || !stats.sigma) return 1.5
    const step = Math.max(1, Math.floor(values.length / 200000))
    const sample = new Float32Array(Math.ceil(values.length / step))
    let count = 0
    for (let index = 0; index < values.length; index += step) sample[count++] = values[index]
    const sorted = sample.subarray(0, count).sort()
    let level = sorted[Math.min(count - 1, Math.floor(count * 0.99))]
    // Masks and other near-binary maps: put the surface halfway to the top.
    if (level >= stats.max - Math.abs(stats.max) * 1e-6) level = (stats.mean + stats.max) / 2
    return round(clamp((level - stats.mean) / stats.sigma, -50, 50), 1)
  }

  function sigmaRange(stats, sigma) {
    if (!stats.sigma) return [0, 10]
    const low = (stats.min - stats.mean) / stats.sigma
    const high = (stats.max - stats.mean) / stats.sigma
    const start = high > 1.5 ? 0 : Math.floor(low * 10) / 10
    const end = Math.max(Math.ceil(Math.min(high, 20) * 10) / 10, start + 0.5)
    return [Math.min(start, sigma), Math.max(end, sigma)]
  }

  function volumeParams(layer, volume) {
    const { registry, themes } = plugin.representation.volume
    const provider = registry.get('isosurface')
    const colorProvider = themes.colorThemeRegistry.get('uniform')
    const sizeProvider = themes.sizeThemeRegistry.get(provider.defaultSizeTheme.name)
    return {
      type: {
        name: 'isosurface',
        params: { ...defaultValues(provider.getParams(themes, volume)), isoValue: Volume.IsoValue.relative(layer.sigma), alpha: layer.opacity },
      },
      colorTheme: { name: 'uniform', params: { ...defaultValues(colorProvider.getParams({ volume })), value: layer.color } },
      sizeTheme: {
        name: sizeProvider.name,
        params: { ...defaultValues(sizeProvider.getParams({ volume })), ...(provider.defaultSizeTheme.props || {}) },
      },
    }
  }

  async function addVolume(request) {
    const key = request.key || request.url
    const previous = layers.get(key)
    const label = fileName(request)
    const layer = {
      key,
      kind: 'volume',
      label,
      status: 'loading',
      visible: true,
      color: parseColor(request.color) ?? previous?.color ?? nextMapColor(),
      sigma: Number.isFinite(request.sigma) ? request.sigma : null,
      opacity: Number.isFinite(request.opacity) ? clamp(request.opacity, 0.05, 1) : previous?.opacityChosen ? previous.opacity : hasStructure() ? 0.5 : 1,
      opacityChosen: Number.isFinite(request.opacity) || Boolean(previous?.opacityChosen),
      stats: null,
      range: null,
      refs: {},
    }
    layers.set(key, layer)
    setActivity(`Opening ${label}`)
    try {
      const bytes = await readBytes(request.url)
      const raw = await plugin.builders.data.rawData({ data: bytes, label }, { state: { isGhost: true } })
      layer.refs.root = raw.ref
      const format = request.format || 'ccp4'
      const provider = plugin.dataFormats.get(format)
      if (!provider) throw new Error(`Mol* cannot read ${format} maps`)
      const parsed = await provider.parse(plugin, raw, request.entryId ? { entryId: request.entryId } : undefined)
      const selector = parsed.volume || parsed.volumes?.[0]
      const volume = selector?.cell?.obj?.data
      if (!volume?.grid) throw new Error('the file holds no readable map')
      const { min, max, mean, sigma } = volume.grid.stats
      layer.stats = { min, max, mean, sigma }
      if (layer.sigma === null) {
        // An absolute level (e.g. the contour DAQ used) beats the map's previous level, which beats a guess.
        // The previous level carries over in absolute density: a binned preview and the full map
        // have different statistics, so the same σ would draw a different surface.
        if (Number.isFinite(request.level) && sigma > 0) layer.sigma = round((request.level - mean) / sigma, 2)
        else if (previous?.stats && Number.isFinite(previous.sigma) && sigma > 0) {
          layer.sigma = round((previous.stats.mean + previous.sigma * previous.stats.sigma - mean) / sigma, 2)
        } else if (Number.isFinite(previous?.sigma)) layer.sigma = previous.sigma
        else layer.sigma = recommendedSigma(volume) ?? automaticSigma(volume)
      }
      layer.range = sigmaRange(layer.stats, layer.sigma)
      const repr = await plugin
        .build()
        .to(selector)
        .apply(StateTransforms.Representation.VolumeRepresentation3D, volumeParams(layer, volume))
        .commit()
      layer.refs.volume = selector.ref
      layer.refs.repr = repr.ref
      layer.applied = { sigma: layer.sigma, opacity: layer.opacity, color: layer.color }
      layer.status = 'ready'
    } catch (err) {
      await abandonLoad(key, layer, previous)
      throw failure(`Could not open ${label}`, err)
    } finally {
      setActivity(null)
    }
    await retireReplaced(previous)
    // Settings changed while the map loaded apply now.
    scheduleVolumeUpdate(layer)
    return publicView(layer)
  }

  function scheduleVolumeUpdate(layer) {
    if (layer.status !== 'ready' || !layer.refs.repr) return
    if (layer.updating) {
      layer.dirty = true
      return
    }
    layer.updating = true
    const apply = async () => {
      do {
        layer.dirty = false
        const target = { sigma: layer.sigma, opacity: layer.opacity, color: layer.color }
        const applied = layer.applied || {}
        if (target.sigma === applied.sigma && target.opacity === applied.opacity && target.color === applied.color) break
        const old = cell(layer.refs.repr)?.transform?.params
        if (!old) break
        const params = {
          ...old,
          type: { ...old.type, params: { ...old.type.params, isoValue: Volume.IsoValue.relative(target.sigma), alpha: target.opacity } },
          colorTheme:
            target.color === applied.color
              ? old.colorTheme
              : { name: 'uniform', params: { ...(old.colorTheme.name === 'uniform' ? old.colorTheme.params : {}), value: target.color } },
        }
        await plugin.build().to(layer.refs.repr).update(params).commit()
        layer.applied = target
      } while (layer.dirty && layers.get(layer.key) === layer)
    }
    apply()
      .catch((err) => console.error('Mol* volume update failed', err))
      .finally(() => {
        layer.updating = false
      })
  }

  /** Restyle a map: {sigma?, opacity?, color?}. Slider drags coalesce into one update at a time. */
  function setVolume(key, patch) {
    const layer = layers.get(key)
    if (!layer || layer.kind !== 'volume') return
    if (Number.isFinite(patch.sigma)) {
      layer.sigma = round(patch.sigma, 2)
      if (layer.range) layer.range = [Math.min(layer.range[0], layer.sigma), Math.max(layer.range[1], layer.sigma)]
    }
    if (Number.isFinite(patch.opacity)) {
      layer.opacity = round(clamp(patch.opacity, 0.05, 1), 2)
      layer.opacityChosen = true
    }
    const color = parseColor(patch.color)
    if (color !== null) layer.color = color
    emit()
    scheduleVolumeUpdate(layer)
  }

  // ----------------------------------------------------------- structures
  async function addStructure(request) {
    const key = request.key || request.url
    const previous = layers.get(key)
    const label = fileName(request)
    const daq = request.color === 'daq'
    const layer = {
      key,
      kind: 'structure',
      label,
      status: 'loading',
      visible: true,
      daq,
      colorMode: daq ? 'daq' : 'chain',
      style: MODEL_STYLES[request.style] ? request.style : MODEL_STYLES[previous?.style] ? previous.style : 'cartoon',
      daqDomain: Array.isArray(request.daqDomain) && request.daqDomain.length === 2 ? [...request.daqDomain] : [...DAQ_DOMAIN],
      daqColors: daqColors(request),
      refs: {},
    }
    layers.set(key, layer)
    setActivity(`Opening ${label}`)
    try {
      const bytes = await readBytes(request.url)
      const binary = /\.bcif\b/.test(nameHints(request)) || (bytes.length > 0 && bytes[0] >= 0x80)
      const payload = binary ? bytes : new TextDecoder().decode(bytes)
      const format = structureFormat(request, payload)
      const raw = await plugin.builders.data.rawData({ data: payload, label }, { state: { isGhost: true } })
      layer.refs.root = raw.ref
      const builder = plugin.builders.structure
      const trajectory = await builder.parseTrajectory(raw, format)
      const model = await builder.createModel(trajectory)
      const modelProperties = await builder.insertModelProperties(model)
      const structure = await builder.createStructure(modelProperties || model)
      const structureProperties = await builder.insertStructureProperties(structure)
      const target = structureProperties || structure
      layer.refs.structure = target.ref

      const components = []
      for (const part of STRUCTURE_PARTS) {
        const component = await builder.tryCreateComponentStatic(target, part.component)
        if (component) components.push({ part, component })
      }
      if (!components.length) throw new Error('the file holds no atoms Mol* can draw')
      const update = plugin.build()
      const reprs = []
      for (const { part, component } of components) {
        if (part.polymer) {
          layer.refs.polymerComponent = component.ref
          layer.refs.polymerByType = {}
          for (const type of MODEL_STYLES[layer.style]) layer.refs.polymerByType[type] = buildPolymer(update, layer, type)
          reprs.push(...Object.values(layer.refs.polymerByType))
          continue
        }
        const props = { type: part.type }
        if (part.type === 'ball-and-stick') props.color = 'element-symbol'
        const repr = builder.representation.buildRepresentation(update, component, props, { tag: `cryopilot-${part.component}` })
        reprs.push(repr.ref)
      }
      await update.commit()
      layer.refs.reprs = reprs
      layer.refs.polymers = Object.values(layer.refs.polymerByType || {})
      layer.status = 'ready'
    } catch (err) {
      await abandonLoad(key, layer, previous)
      throw failure(`Could not open ${label}`, err)
    } finally {
      setActivity(null)
    }
    await retireReplaced(previous)
    // A map shown on its own is opaque; once a model joins it, make it see-through
    // unless the user already chose an opacity.
    for (const other of layers.values()) {
      if (other.kind === 'volume' && !other.opacityChosen && other.opacity === 1) {
        other.opacity = 0.5
        scheduleVolumeUpdate(other)
      }
    }
    emit()
    return publicView(layer)
  }

  /** Adds one polymer representation (a Mol* type such as 'cartoon') to `update` and returns its ref. */
  function buildPolymer(update, layer, type) {
    const theme = polymerColorTheme(layer)
    const props = { type, color: theme.name, colorParams: theme.params }
    return plugin.builders.structure.representation.buildRepresentation(update, layer.refs.polymerComponent, props, {
      tag: `cryopilot-polymer-${type}`,
    }).ref
  }

  /**
   * Draws the polymer as cartoon, sticks, or both; ligands keep their sticks. A hidden model
   * takes the new style when it is shown again: Mol* moves the camera when representations
   * change in a scene with nothing visible.
   */
  async function setStructureStyle(key, style) {
    const layer = layers.get(key)
    if (!layer || layer.kind !== 'structure' || layer.style === style || !MODEL_STYLES[style]) return
    layer.style = style
    emit()
    if (layer.visible) await applyStyle(layer)
  }

  /** Brings the layer's polymer representations in line with its style, keeping those that stay. */
  async function applyStyle(layer) {
    if (layer.status !== 'ready' || !cell(layer.refs.polymerComponent)) return
    const wanted = MODEL_STYLES[layer.style]
    if (Object.keys(layer.refs.polymerByType || {}).sort().join() === [...wanted].sort().join()) return
    const byType = { ...(layer.refs.polymerByType || {}) }
    const update = plugin.build()
    for (const [type, ref] of Object.entries(byType)) {
      if (wanted.includes(type)) continue
      if (cell(ref)) update.delete(ref)
      delete byType[type]
    }
    for (const type of wanted) if (!byType[type]) byType[type] = buildPolymer(update, layer, type)
    await update.commit()
    const old = layer.refs.polymers || []
    layer.refs.polymerByType = byType
    layer.refs.polymers = Object.values(byType)
    layer.refs.reprs = [...(layer.refs.reprs || []).filter((ref) => !old.includes(ref)), ...layer.refs.polymers]
  }

  async function setStructureColor(key, mode) {
    const layer = layers.get(key)
    if (!layer || layer.kind !== 'structure' || layer.colorMode === mode) return
    if (mode === 'daq' && !layer.daq) return
    layer.colorMode = mode
    emit()
    if (layer.status !== 'ready') return
    const update = plugin.build()
    for (const ref of layer.refs.polymers || []) {
      const old = cell(ref)?.transform?.params
      if (old) update.to(ref).update({ ...old, colorTheme: polymerColorTheme(layer) })
    }
    await update.commit()
  }

  // ---------------------------------------------------------------- focus
  async function focus({ key, chain, resnum, icode }) {
    const layer =
      (key && layers.get(key)) || [...layers.values()].reverse().find((entry) => entry.kind === 'structure' && entry.status === 'ready')
    if (!layer || layer.kind !== 'structure' || layer.status !== 'ready') throw new Error('Open a model before focusing a residue.')
    const structure = cell(layer.refs.structure)?.obj?.data
    if (!structure) throw new Error(`${layer.label} is not loaded.`)
    const number = Number(resnum)
    const insertion = icode && String(icode).trim() ? { pdbx_PDB_ins_code: String(icode).trim() } : {}
    const candidates = [
      { auth_asym_id: String(chain), auth_seq_id: number, ...insertion },
      { label_asym_id: String(chain), auth_seq_id: number, ...insertion },
    ]
    let loci = null
    for (const schema of candidates) {
      const found = StructureElement.Schema.toLoci(structure, schema)
      if (!StructureElement.Loci.isEmpty(found)) {
        loci = found
        break
      }
    }
    if (!loci) throw new Error(`Residue ${chain} ${resnum} is not in ${layer.label}.`)
    if (!layer.visible) await setVisible(layer.key, true)
    plugin.managers.structure.focus.setFromLoci(loci)
    plugin.managers.camera.focusLoci(loci, { durationMs: 250, extraRadius: 4, minRadius: 8 })
  }

  // ----------------------------------------------------------- visibility
  async function setVisible(key, visible) {
    const layer = layers.get(key)
    if (!layer) return
    layer.visible = visible
    emit()
    if (!layer.refs.root || !cell(layer.refs.root)) return
    const cells = data().selectQ((q) => q.byRef(layer.refs.root).subtree())
    muted += 1
    try {
      for (const entry of cells) data().updateCellState(entry.transform.ref, { isHidden: !visible })
    } finally {
      muted -= 1
    }
    if (visible && layer.kind === 'structure') await applyStyle(layer)
  }

  // --------------------------------------------------------------- remove
  async function remove(key) {
    const layer = layers.get(key)
    if (!layer) return
    layers.delete(key)
    emit()
    await deleteRefs(layer)
  }

  async function clear() {
    layers.forEach((layer) => {
      layer.removing = true
    })
    layers.clear()
    emit()
    plugin.managers.structure.focus.clear()
    await plugin.clear()
  }

  // ---------------------------------------------------------------- queue
  /**
   * Runs a viewer request after the ones before it. Requests (store.showInViewer):
   *   {kind:'volume', url, label, key?, sigma?, level?, color?, opacity?, format?:'ccp4'|'dscif', entryId?}
   *     sigma is the level in σ above the mean; level is an absolute level used when sigma is absent.
   *   {kind:'structure', url, label, key?, format?:'pdb'|'mmcif', color?:'chain'|'daq', style?:'cartoon'|'sticks'|'both', daqDomain?, daqColors?}
   *     format defaults to the file name, then the text (mmCIF starts with data_); style defaults to the replaced layer's.
   *   {kind:'focus', key?, chain, resnum, icode?}   key defaults to the newest model
   *   {kind:'style', key, style:'cartoon'|'sticks'|'both'}
   *   {kind:'remove', key} | {kind:'clear'}
   * A request whose key is already shown replaces that layer once the new one loads; a replaced map
   * keeps its colour and its absolute level.
   */
  function run(request) {
    const task = () => {
      if (disposed) return null
      switch (request.kind) {
        case 'volume':
          return addVolume(request)
        case 'structure':
          return addStructure(request)
        case 'focus':
          return focus(request)
        case 'style':
          return setStructureStyle(request.key, request.style)
        case 'remove':
          return remove(request.key)
        case 'clear':
          return clear()
        default:
          throw new Error(`Unknown viewer request: ${request.kind}`)
      }
    }
    const result = queue.then(task)
    queue = result.catch(() => {})
    return result
  }

  // ---------------------------------------------------------------- scene
  function resetCamera() {
    plugin.managers.camera.reset()
  }

  async function screenshot(name = 'cryopilot-view') {
    const helper = plugin.helpers?.viewportScreenshot
    if (!helper) throw new Error('The viewer is not ready.')
    await helper.download(`${name.replace(/[^\w.-]+/g, '_')}.png`)
  }

  function setBackground(color) {
    const value = parseColor(color)
    if (value === null || !plugin.canvas3d) return
    plugin.canvas3d.setProps({ renderer: { ...plugin.canvas3d.props.renderer, backgroundColor: value } })
  }

  /** Mol*'s own panels and viewport buttons, for power users. */
  function setControls(shown) {
    plugin.layout.setProps({ showControls: Boolean(shown) })
    plugin.layout.events.updated.next(undefined)
  }

  function syncViewportFlags() {
    const shown = Boolean(plugin.layout?.state?.showControls)
    let changed = false
    for (const flag of VIEWPORT_FLAGS) {
      const item = PluginConfig.Viewport[flag]
      if (item && plugin.config.get(item) !== shown) {
        plugin.config.set(item, shown)
        changed = true
      }
    }
    // Ask Mol*'s viewport buttons to render again with the new flags.
    if (changed) plugin.layout.events.updated.next(undefined)
    if (snapshot.controls !== shown) emit()
  }

  /** Keeps layers in step with edits made through Mol*'s own controls. */
  function syncFromState() {
    if (muted) return
    let changed = false
    for (const layer of layers.values()) {
      if (layer.status !== 'ready' || layer.removing) continue
      if (layer.refs.root && !cell(layer.refs.root)) {
        layers.delete(layer.key)
        changed = true
        continue
      }
      const refs = reprRefs(layer).filter((ref) => cell(ref))
      if (refs.length) {
        const visible = refs.some((ref) => !cell(ref).state.isHidden)
        if (visible !== layer.visible) {
          layer.visible = visible
          changed = true
        }
      }
      if (layer.kind === 'volume' && !layer.updating && layer.stats) {
        const params = cell(layer.refs.repr)?.transform?.params
        const iso = params?.type?.params?.isoValue
        if (!iso) continue
        const sigma = round(Volume.IsoValue.toRelative(iso, layer.stats).relativeValue, 2)
        const opacity = params.type.params.alpha
        const color = params.colorTheme?.name === 'uniform' ? params.colorTheme.params.value : layer.color
        if (sigma !== layer.sigma || opacity !== layer.opacity || color !== layer.color) {
          Object.assign(layer, { sigma, opacity, color })
          layer.applied = { sigma, opacity, color }
          if (layer.range) layer.range = [Math.min(layer.range[0], sigma), Math.max(layer.range[1], sigma)]
          changed = true
        }
      }
    }
    if (changed) emit()
  }

  if (plugin.layout?.events?.updated) subscriptions.push(plugin.layout.events.updated.subscribe(syncViewportFlags))
  subscriptions.push(data().events.changed.subscribe(syncFromState))
  subscriptions.push(data().events.cell.stateUpdated.subscribe(syncFromState))
  if (plugin.layout?.events?.updated) syncViewportFlags()

  return {
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    getSnapshot: () => snapshot,
    run,
    setVolume,
    setVisible,
    setStructureColor,
    setStructureStyle: (key, style) => run({ kind: 'style', key, style }),
    remove: (key) => run({ kind: 'remove', key }),
    clear: () => run({ kind: 'clear' }),
    resetCamera,
    screenshot,
    setBackground,
    setControls,
    dispose() {
      disposed = true
      subscriptions.forEach((subscription) => subscription.unsubscribe())
      listeners.clear()
    },
  }
}
