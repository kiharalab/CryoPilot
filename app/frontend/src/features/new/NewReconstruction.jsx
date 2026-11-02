// New reconstruction: the dataset card (the same manifest the terminal
// questionnaire collects) above the goal composer. Enter in the composer
// checks the card, creates the session, and opens it. A session can also
// continue a CryoSPARC project that people or earlier sessions started.
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { FolderOpen, History } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative, toDate } from '../../lib/format'
import { useApp } from '../../store/app'
import { Composer } from '../../components/Composer'
import { Button, Field, InlineError, Input, Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger, Segmented, Switch } from '../../components/ui'
import { PathInput } from './PathInput'
import { ProjectPicker, findProject, useCryosparcProjects } from './ProjectPicker'
import { GuideChip, ModelChip, RECONSTRUCTION_EFFORTS } from './AgentChips'
import { SetupNotice, setupMessage } from './SetupNotice'
import { modelService } from '../setup/services'

const FORM_KEY = 'cryopilot-reconstruction-form'

const DEFAULT_FORM = {
  // 'data' imports a dataset into a new CryoSPARC project; 'project' continues an existing one.
  start_from: 'data',
  existing_project_uid: '',
  existing_project_title: '',
  dataset_kind: 'movies',
  dataset_path: '',
  particle_metadata_path: '',
  gain_reference_path: '',
  defect_file_path: '',
  eer_enabled: false,
  eer_fractions: '',
  eer_upsampling: '',
  pixel_size_A: '',
  accelerating_voltage_kV: '300',
  spherical_aberration_mm: '2.7',
  total_dose_e_per_A2: '',
  negative_stain: false,
  phase_plate: false,
  sample_description: '',
  particle_diameter_A: '',
  molecular_mass_kDa: '',
  expected_symmetry: 'C1',
}

// Order of the controls on the page, for focusing the first invalid one.
const FIELD_ORDER = [
  'existing_project_uid',
  'dataset_kind',
  'dataset_path',
  'particle_metadata_path',
  'gain_reference_path',
  'defect_file_path',
  'eer_fractions',
  'eer_upsampling',
  'pixel_size_A',
  'accelerating_voltage_kV',
  'spherical_aberration_mm',
  'total_dose_e_per_A2',
  'sample_description',
  'particle_diameter_A',
  'molecular_mass_kDa',
  'expected_symmetry',
  'project_title',
  'parent_directory',
]

const START_FROM = [
  { value: 'data', label: 'New data' },
  { value: 'project', label: 'Existing project' },
]

const DATASET_KINDS = [
  { value: 'movies', label: 'Movies' },
  { value: 'micrographs', label: 'Micrographs' },
  { value: 'particles', label: 'Particles' },
]

const DATASET_COPY = {
  movies: { label: 'Movies', placeholder: '/data/krios/2026-09-01/movies/*.tif' },
  micrographs: { label: 'Micrographs', placeholder: '/data/micrographs/*.mrc' },
  particles: { label: 'Particle stacks', placeholder: '/data/relion/Extract/job012/Movies/*.mrcs' },
}

// ------------------------------------------------------------------ helpers
/** Keeps only known keys, as strings (or booleans for the switches). */
function normalizeForm(values) {
  const form = {}
  for (const [key, fallback] of Object.entries(DEFAULT_FORM)) {
    const value = values?.[key]
    if (typeof fallback === 'boolean') form[key] = Boolean(value)
    else form[key] = value === undefined || value === null || value === '' ? fallback : String(value)
  }
  if (!DATASET_KINDS.some((kind) => kind.value === form.dataset_kind)) form.dataset_kind = DEFAULT_FORM.dataset_kind
  if (!START_FROM.some((option) => option.value === form.start_from)) form.start_from = DEFAULT_FORM.start_from
  return form
}

function formFromManifest(manifest) {
  const dataset = manifest?.dataset || {}
  const acquisition = manifest?.acquisition || {}
  const sample = manifest?.sample || {}
  return normalizeForm({
    dataset_kind: dataset.kind,
    dataset_path: dataset.path,
    particle_metadata_path: dataset.particle_metadata_path,
    gain_reference_path: dataset.gain_reference_path,
    defect_file_path: dataset.defect_file_path,
    eer_enabled: dataset.eer?.enabled,
    eer_fractions: dataset.eer?.fractions,
    eer_upsampling: dataset.eer?.upsampling,
    pixel_size_A: acquisition.pixel_size_A,
    accelerating_voltage_kV: acquisition.accelerating_voltage_kV,
    spherical_aberration_mm: acquisition.spherical_aberration_mm,
    total_dose_e_per_A2: acquisition.total_dose_e_per_A2,
    negative_stain: acquisition.negative_stain,
    phase_plate: acquisition.phase_plate,
    sample_description: sample.description,
    particle_diameter_A: sample.particle_diameter_A,
    molecular_mass_kDa: sample.molecular_mass_kDa,
    expected_symmetry: sample.expected_symmetry,
  })
}

function readRemembered(projectId) {
  try {
    const saved = JSON.parse(localStorage.getItem(`${FORM_KEY}:${projectId}`) || 'null')
    return saved && typeof saved === 'object' ? normalizeForm(saved) : null
  } catch {
    return null
  }
}

function remember(projectId, form) {
  try {
    localStorage.setItem(`${FORM_KEY}:${projectId}`, JSON.stringify(form))
  } catch {
    // private window; the values are simply not remembered
  }
}

/** The project's title, or "Title 2", "Title 3" when earlier sessions used it, so CryoSPARC folders stay distinct. */
function uniqueTitle(base, previous) {
  const used = new Set(previous.map((session) => session.config?.manifest?.project?.title).filter(Boolean))
  if (!used.has(base)) return base
  let suffix = 2
  while (used.has(`${base} ${suffix}`)) suffix += 1
  return `${base} ${suffix}`
}

/** The same checks as the server's build_manifest, so mistakes show before the round trip. */
function validate(form, { title, parent }) {
  const errors = {}
  const text = (key) => String(form[key] ?? '').trim()
  const required = (key) => {
    if (!text(key)) errors[key] = 'Required'
  }
  const number = (key, { inclusive = false, optional = false } = {}) => {
    const raw = text(key)
    if (!raw) {
      if (!optional) errors[key] = 'Required'
      return
    }
    const value = Number(raw)
    if (!Number.isFinite(value)) errors[key] = 'Enter a number'
    else if (inclusive ? value < 0 : value <= 0) errors[key] = inclusive ? 'Must be at least 0' : 'Must be greater than 0'
  }
  const count = (key) => {
    if (!/^\d+$/.test(text(key)) || Number(text(key)) <= 0) errors[key] = 'Enter a positive integer'
  }

  if (form.start_from === 'project') {
    if (!text('existing_project_uid')) errors.existing_project_uid = 'Choose a CryoSPARC project'
    else if (!/^P\d+$/i.test(text('existing_project_uid'))) errors.existing_project_uid = 'Enter a project UID such as P12'
    number('particle_diameter_A', { optional: true })
    number('molecular_mass_kDa', { optional: true })
    return errors
  }
  required('dataset_path')
  if (form.dataset_kind === 'particles') required('particle_metadata_path')
  if (form.dataset_kind === 'movies' && form.eer_enabled) {
    count('eer_fractions')
    count('eer_upsampling')
  }
  number('pixel_size_A')
  number('accelerating_voltage_kV')
  number('spherical_aberration_mm', { inclusive: true })
  number('total_dose_e_per_A2')
  number('particle_diameter_A')
  number('molecular_mass_kDa', { optional: true })
  if (!title.trim()) errors.project_title = 'Required'
  if (!parent.trim()) errors.parent_directory = 'Choose where CryoSPARC creates the project folder'
  return errors
}

function buildManifest(form, { title, parent, goal }) {
  const trim = (key) => String(form[key] ?? '').trim()
  if (form.start_from === 'project') {
    return {
      dataset_kind: 'existing_project',
      existing_project_uid: trim('existing_project_uid').toUpperCase(),
      existing_project_title: trim('existing_project_title'),
      sample_description: trim('sample_description'),
      particle_diameter_A: trim('particle_diameter_A') || null,
      molecular_mass_kDa: trim('molecular_mass_kDa') || null,
      expected_symmetry: trim('expected_symmetry') || 'C1',
      user_goal: goal.trim(),
    }
  }
  const movies = form.dataset_kind === 'movies'
  const particles = form.dataset_kind === 'particles'
  return {
    project_title: title.trim(),
    parent_directory: parent.trim(),
    dataset_kind: form.dataset_kind,
    dataset_path: trim('dataset_path'),
    particle_metadata_path: particles ? trim('particle_metadata_path') : null,
    gain_reference_path: movies ? trim('gain_reference_path') || null : null,
    defect_file_path: movies ? trim('defect_file_path') || null : null,
    eer_enabled: movies && form.eer_enabled,
    eer_fractions: movies && form.eer_enabled ? trim('eer_fractions') : null,
    eer_upsampling: movies && form.eer_enabled ? trim('eer_upsampling') : null,
    pixel_size_A: trim('pixel_size_A'),
    accelerating_voltage_kV: trim('accelerating_voltage_kV'),
    spherical_aberration_mm: trim('spherical_aberration_mm'),
    total_dose_e_per_A2: trim('total_dose_e_per_A2'),
    negative_stain: form.negative_stain,
    phase_plate: form.phase_plate,
    sample_description: trim('sample_description'),
    particle_diameter_A: trim('particle_diameter_A'),
    molecular_mass_kDa: trim('molecular_mass_kDa') || null,
    expected_symmetry: trim('expected_symmetry') || 'C1',
    user_goal: goal.trim(),
  }
}

/** Under the project field: the chosen project's title, or why there is no list to choose from. */
function projectHint(uid, { projects, error }, signedIn) {
  if (!signedIn) return 'Sign in to CryoSPARC in Settings to choose from your projects.'
  if (projects === null) return 'Loading your CryoSPARC projects…'
  if (error) return `Could not list your projects (${error}). Type the project UID.`
  const project = findProject(projects, uid)
  if (project) return project.title || project.uid
  return String(uid || '').trim() ? 'Not one of your projects, or not loaded yet.' : 'Choose a project, or type its UID.'
}

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

// --------------------------------------------------------------- component
export function NewReconstruction() {
  const project = useApp((state) => state.project)
  const setup = useApp((state) => state.setup)
  const patchSession = useApp((state) => state.patchSession)
  const openSession = useApp((state) => state.openSession)
  const refreshProject = useApp((state) => state.refreshProject)

  const previous = useMemo(
    () =>
      (project?.sessions || [])
        .filter((session) => session.kind === 'reconstruction' && session.config?.manifest?.dataset?.path)
        .sort((a, b) => (toDate(b.created_at)?.getTime() ?? 0) - (toDate(a.created_at)?.getTime() ?? 0)),
    [project?.sessions],
  )

  // Acquisition and sample values carry over within a project; the data itself
  // is chosen each time (or explicitly with "Reuse a previous dataset").
  const [form, setForm] = useState(() => {
    const carried = readRemembered(project.id) || (previous[0] ? formFromManifest(previous[0].config.manifest) : null)
    return carried ? { ...carried, dataset_path: '', particle_metadata_path: '' } : normalizeForm({})
  })
  const [goal, setGoal] = useState('')
  const [title, setTitle] = useState(() => uniqueTitle(project.title, previous))
  const [parent, setParent] = useState(project.parent_directory || '')
  const [editingProject, setEditingProject] = useState(!project.parent_directory)
  const [errors, setErrors] = useState({})
  const [submitError, setSubmitError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState(setup?.defaults?.reconstruction_model || 'gpt-5.5')
  const [effort, setEffort] = useState(setup?.defaults?.reconstruction_effort || 'xhigh')
  const [guides, setGuides] = useState([])
  const [guide, setGuide] = useState(setup?.processing_guide || 'original')
  const scrollRef = useRef(null)
  const composerRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    api
      .guides()
      .then((data) => !cancelled && setGuides(data.guides || []))
      .catch(() => !cancelled && setGuides([]))
    return () => {
      cancelled = true
    }
  }, [])

  const platformReady = Boolean(setup?.platform?.reconstruction_supported)
  const cryosparcReady = Boolean(setup?.cryosparc?.email && setup?.cryosparc?.password_set)
  const continuing = form.start_from === 'project'
  const cryosparcProjects = useCryosparcProjects(continuing && cryosparcReady)
  // The chosen model's API key: Claude models run on Anthropic.
  const keyService = modelService(model)
  const keyReady = Boolean(setup?.[keyService]?.configured)
  const ready = platformReady && cryosparcReady && keyReady
  // Shown above the composer, so the reason it cannot send stays in view while the cards scroll.
  const missingSetup = []
  if (platformReady && !cryosparcReady) missingSetup.push('cryosparc')
  if (platformReady && !keyReady) missingSetup.push(keyService)

  const update = (key, value) => {
    setForm((current) => ({ ...current, [key]: value }))
    setErrors((current) => (current[key] ? { ...current, [key]: undefined } : current))
  }
  const bind = (key) => ({
    value: form[key],
    invalid: Boolean(errors[key]),
    'data-field': key,
    onChange: (eventOrValue) => update(key, eventOrValue?.target ? eventOrValue.target.value : eventOrValue),
  })

  const focusFirstError = (fieldErrors) => {
    const key = FIELD_ORDER.find((name) => fieldErrors[name])
    if (!key) {
      if (fieldErrors.user_goal) composerRef.current?.focus()
      return
    }
    if (key === 'project_title' || key === 'parent_directory') setEditingProject(true)
    // Wait a frame so a section opened just now is in the DOM.
    requestAnimationFrame(() => {
      const element = scrollRef.current?.querySelector(`[data-field="${key}"]`)
      if (!element) return
      element.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
      element.focus({ preventScroll: true })
    })
  }

  const fillFrom = (session) => {
    setForm(formFromManifest(session.config.manifest))
    setErrors({})
  }

  const submit = async () => {
    if (busy || !ready) return
    const clientErrors = validate(form, { title, parent })
    if (Object.keys(clientErrors).length) {
      setErrors(clientErrors)
      setSubmitError(null)
      focusFirstError(clientErrors)
      return
    }
    setBusy(true)
    setErrors({})
    setSubmitError(null)
    const parentDirectory = parent.trim()
    try {
      if (!continuing && parentDirectory !== (project.parent_directory || '')) {
        await api.updateProject(project.id, { parent_directory: parentDirectory })
        refreshProject().catch(() => {})
      }
      const session = await api.createReconstruction(project.id, {
        manifest: buildManifest(form, { title, parent: parentDirectory, goal }),
        agent: { model, reasoning_effort: effort },
        processing_guide: guide,
      })
      remember(project.id, form)
      // The user may have switched projects while the agent was starting.
      if (useApp.getState().projectId !== project.id) {
        useApp.getState().toast({ title: `Session started in ${project.title}` })
        return
      }
      patchSession(session)
      openSession(session.id)
    } catch (err) {
      const fieldErrors = err.fields || {}
      const known = Object.keys(fieldErrors).filter((key) => FIELD_ORDER.includes(key) || key === 'user_goal')
      const other = Object.keys(fieldErrors).filter((key) => !known.includes(key))
      if (known.length) {
        setErrors(fieldErrors)
        focusFirstError(fieldErrors)
      }
      if (err.setup) setSubmitError({ setup: err.setup, message: err.message })
      else if (other.length) setSubmitError({ message: other.map((key) => `${key.replace(/_/g, ' ')}: ${fieldErrors[key]}`).join('. ') })
      else if (!known.length) setSubmitError({ message: err.message })
      setBusy(false)
    }
  }

  const kind = form.dataset_kind
  const copy = DATASET_COPY[kind]

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="@container mx-auto flex w-full max-w-[760px] flex-col gap-5 px-4 pb-6 pt-6 sm:px-6">
          <div>
            <h1 className="text-xl font-semibold tracking-[-0.015em] text-fg">Start a reconstruction</h1>
            <p className="mt-1 max-w-[620px] text-sm text-muted">
              {continuing
                ? 'The reconstruction agent continues a CryoSPARC project that you or an earlier session started. It surveys the jobs already there, then runs one job at a time toward your goal in a new workspace. It reports back when it is done or needs you.'
                : 'The reconstruction agent creates a CryoSPARC project for this dataset and runs one job at a time toward your goal. It reports back when it is done or needs you.'}
            </p>
            <Segmented
              ariaLabel="Start from"
              value={form.start_from}
              onChange={(value) => update('start_from', value)}
              options={START_FROM}
              className="mt-4"
            />
          </div>

          {!platformReady ? (
            <SetupNotice tone="neutral">
              The reconstruction agent runs on the Linux computer where CryoSPARC is installed. Start CryoPilot there to process data.
            </SetupNotice>
          ) : null}

          <div className="rounded-xl border border-line bg-elevated">
            {continuing ? (
              <Section title="CryoSPARC project">
                <Field
                  label="Project"
                  error={errors.existing_project_uid}
                  hint={projectHint(form.existing_project_uid, cryosparcProjects, cryosparcReady)}
                >
                  {({ id }) => (
                    <ProjectPicker
                      id={id}
                      value={form.existing_project_uid}
                      projects={cryosparcProjects.projects}
                      invalid={Boolean(errors.existing_project_uid)}
                      data-field="existing_project_uid"
                      onChange={(uid, projectTitle) => {
                        setForm((current) => ({ ...current, existing_project_uid: uid, existing_project_title: projectTitle }))
                        setErrors((current) => ({ ...current, existing_project_uid: undefined }))
                      }}
                    />
                  )}
                </Field>
              </Section>
            ) : null}
            {continuing ? null : (
              <>
                <Section
                  title="Dataset"
                  action={
                    previous.length ? (
                      <Menu>
                        <MenuTrigger asChild>
                          <Button size="xs" variant="ghost" icon={History}>
                            Reuse a previous dataset
                          </Button>
                        </MenuTrigger>
                        <MenuContent align="end" className="w-[300px]">
                          <MenuLabel>Fill the card from</MenuLabel>
                          <div className="max-h-[280px] overflow-y-auto">
                            {previous.slice(0, 30).map((session) => (
                              <MenuItem key={session.id} hint={formatRelative(session.created_at)} onSelect={() => fillFrom(session)}>
                                {session.title}
                              </MenuItem>
                            ))}
                          </div>
                        </MenuContent>
                      </Menu>
                    ) : null
                  }
                >
                  <div className="flex flex-col gap-4">
                    <Segmented
                      ariaLabel="Data type"
                      value={kind}
                      onChange={(value) => update('dataset_kind', value)}
                      options={DATASET_KINDS}
                      className="self-start"
                    />
                    <Field label={copy.label} error={errors.dataset_path} hint="A folder or a glob on the CryoSPARC computer">
                      {({ id }) => <PathInput id={id} placeholder={copy.placeholder} {...bind('dataset_path')} />}
                    </Field>
                    {kind === 'particles' ? (
                      <Field label="Particle metadata" error={errors.particle_metadata_path} hint="The RELION .star file that describes the stacks">
                        {({ id }) => <PathInput id={id} placeholder="/data/relion/Extract/job012/particles.star" {...bind('particle_metadata_path')} />}
                      </Field>
                    ) : null}
                    {kind === 'movies' ? (
                      <>
                        <div className="grid grid-cols-1 gap-4 @lg:grid-cols-2">
                          <Field label="Gain reference" optional error={errors.gain_reference_path}>
                            {({ id }) => <PathInput id={id} placeholder="/data/krios/gain.mrc" {...bind('gain_reference_path')} />}
                          </Field>
                          <Field label="Defect file" optional error={errors.defect_file_path}>
                            {({ id }) => <PathInput id={id} placeholder="/data/krios/defects.txt" {...bind('defect_file_path')} />}
                          </Field>
                        </div>
                        <SwitchField
                          label="EER movies"
                          description="Falcon 4 electron-event representation"
                          checked={form.eer_enabled}
                          onCheckedChange={(checked) => update('eer_enabled', checked)}
                        />
                        {form.eer_enabled ? (
                          <div className="grid grid-cols-2 gap-4">
                            <Field label="EER fractions" error={errors.eer_fractions}>
                              {({ id }) => <Input id={id} inputMode="numeric" autoComplete="off" placeholder="40" {...bind('eer_fractions')} />}
                            </Field>
                            <Field label="EER upsampling" error={errors.eer_upsampling}>
                              {({ id }) => <Input id={id} inputMode="numeric" autoComplete="off" placeholder="2" {...bind('eer_upsampling')} />}
                            </Field>
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </div>
                </Section>

                <Section title="Microscope">
                  <div className="grid grid-cols-2 gap-4 @xl:grid-cols-4">
                    <Field label="Pixel size" error={errors.pixel_size_A}>
                      {({ id }) => <UnitInput id={id} unit="Å" placeholder="0.83" {...bind('pixel_size_A')} />}
                    </Field>
                    <Field label="Voltage" error={errors.accelerating_voltage_kV}>
                      {({ id }) => <UnitInput id={id} unit="kV" placeholder="300" {...bind('accelerating_voltage_kV')} />}
                    </Field>
                    <Field label="Spherical aberration" error={errors.spherical_aberration_mm}>
                      {({ id }) => <UnitInput id={id} unit="mm" placeholder="2.7" {...bind('spherical_aberration_mm')} />}
                    </Field>
                    <Field label="Total dose" error={errors.total_dose_e_per_A2}>
                      {({ id }) => <UnitInput id={id} unit="e⁻/Å²" placeholder="50" {...bind('total_dose_e_per_A2')} />}
                    </Field>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-x-8 gap-y-3">
                    <SwitchField label="Negative stain" checked={form.negative_stain} onCheckedChange={(checked) => update('negative_stain', checked)} />
                    <SwitchField label="Phase plate" checked={form.phase_plate} onCheckedChange={(checked) => update('phase_plate', checked)} />
                  </div>
                </Section>
              </>
            )}

            <Section title="Sample">
              <div className="flex flex-col gap-4">
                <Field label="Description" optional error={errors.sample_description}>
                  {({ id }) => <Input id={id} autoComplete="off" placeholder="Human GABA-A receptor in nanodisc" {...bind('sample_description')} />}
                </Field>
                <div className="grid grid-cols-2 gap-4 @xl:grid-cols-3">
                  <Field label="Particle diameter" optional={continuing} error={errors.particle_diameter_A}>
                    {({ id }) => <UnitInput id={id} unit="Å" placeholder="180" {...bind('particle_diameter_A')} />}
                  </Field>
                  <Field label="Molecular mass" optional error={errors.molecular_mass_kDa}>
                    {({ id }) => <UnitInput id={id} unit="kDa" placeholder="450" {...bind('molecular_mass_kDa')} />}
                  </Field>
                  <Field label="Symmetry" error={errors.expected_symmetry} hint="C1 if unknown">
                    {({ id }) => <Input id={id} mono autoComplete="off" spellCheck={false} placeholder="C1" {...bind('expected_symmetry')} />}
                  </Field>
                </div>
              </div>
            </Section>

            {continuing ? (
              <div className="flex items-center gap-3 border-t border-line px-5 py-3 text-sm text-muted">
                <FolderOpen size={15} strokeWidth={1.75} className="shrink-0 text-faint" />
                <p className="min-w-0 flex-1">
                  Works in a new workspace in{' '}
                  {form.existing_project_uid.trim() ? (
                    <span className="font-mono text-[12.5px] text-fg">{form.existing_project_uid.trim().toUpperCase()}</span>
                  ) : (
                    'the chosen project'
                  )}
                  . The jobs already there stay as they are.
                </p>
              </div>
            ) : editingProject ? (
              <Section title="CryoSPARC project">
                <div className="grid grid-cols-1 gap-4 @lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
                  <Field label="Title" error={errors.project_title}>
                    {({ id }) => (
                      <Input
                        id={id}
                        autoComplete="off"
                        value={title}
                        invalid={Boolean(errors.project_title)}
                        data-field="project_title"
                        onChange={(event) => {
                          setTitle(event.target.value)
                          setErrors((current) => ({ ...current, project_title: undefined }))
                        }}
                      />
                    )}
                  </Field>
                  <Field
                    label="Location"
                    error={errors.parent_directory}
                    hint={project.parent_directory ? 'Saved for this project' : 'CryoSPARC creates the project folder here; saved for this project'}
                  >
                    {({ id }) => (
                      <PathInput
                        id={id}
                        dirsOnly
                        value={parent}
                        invalid={Boolean(errors.parent_directory)}
                        data-field="parent_directory"
                        placeholder="/data/cryosparc/projects"
                        onChange={(value) => {
                          setParent(value)
                          setErrors((current) => ({ ...current, parent_directory: undefined }))
                        }}
                      />
                    )}
                  </Field>
                </div>
              </Section>
            ) : (
              <div className="flex items-center gap-3 border-t border-line px-5 py-3 text-sm text-muted">
                <FolderOpen size={15} strokeWidth={1.75} className="shrink-0 text-faint" />
                <p className="min-w-0 flex-1 truncate" title={`${title} in ${parent}`}>
                  Creates the CryoSPARC project <span className="text-fg">{title}</span> in{' '}
                  <span className="font-mono text-[12.5px] text-fg">{parent}</span>
                </p>
                <Button size="xs" variant="ghost" onClick={() => setEditingProject(true)}>
                  Change
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="shrink-0 px-4 pb-4 pt-1 sm:px-6 sm:pb-5">
        <div className="mx-auto flex w-full max-w-[760px] flex-col gap-3">
          {missingSetup.map((service) => (
            <SetupNotice key={service} settingsSection="accounts">
              {setupMessage(service)}
            </SetupNotice>
          ))}
          {submitError?.setup && !missingSetup.includes(submitError.setup) ? (
            <SetupNotice settingsSection="accounts">{setupMessage(submitError.setup, submitError.message)}</SetupNotice>
          ) : submitError && !submitError.setup ? (
            <InlineError>{submitError.message}</InlineError>
          ) : null}
          <div>
            <Composer
              ref={composerRef}
              size="lg"
              minRows={2}
              value={goal}
              onChange={(text) => {
                setGoal(text)
                if (errors.user_goal) setErrors((current) => ({ ...current, user_goal: undefined }))
              }}
              onSubmit={submit}
              canSubmit={Boolean(goal.trim()) && ready}
              busy={busy}
              placeholder={continuing ? 'What should the reconstruction agent do in this project?' : 'What should the reconstruction agent achieve with this dataset?'}
              toolbarLeft={
                <>
                  <ModelChip
                    model={model}
                    effort={effort}
                    onModelChange={setModel}
                    onEffortChange={setEffort}
                    defaultModel={setup?.defaults?.reconstruction_model}
                    efforts={RECONSTRUCTION_EFFORTS}
                  />
                  <GuideChip guides={guides} value={guide} onChange={setGuide} defaultGuide={setup?.processing_guide} />
                </>
              }
            />
            {errors.user_goal ? <p className="mt-2 px-1 text-xs text-danger">Describe the goal: {errors.user_goal}</p> : null}
          </div>
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ pieces
function Section({ title, action, children }) {
  return (
    <section className="border-t border-line px-5 pb-5 pt-4 first:border-t-0">
      <div className="mb-3 flex min-h-6 items-center gap-2">
        <h2 className="min-w-0 flex-1 text-sm font-semibold text-fg">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  )
}

/** Number field with its unit as a quiet suffix inside the box. */
function UnitInput({ unit, ...props }) {
  return (
    <div className="relative">
      <Input inputMode="decimal" autoComplete="off" style={{ paddingRight: `calc(${unit.length}ch + 22px)` }} {...props} />
      <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-faint">{unit}</span>
    </div>
  )
}

function SwitchField({ label, description, checked, onCheckedChange }) {
  const id = useId()
  return (
    <div className="flex items-center gap-2.5">
      <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} />
      <label htmlFor={id} className={cx('text-sm text-fg', description && 'flex flex-wrap items-baseline gap-x-2')}>
        {label}
        {description ? <span className="text-xs text-muted">{description}</span> : null}
      </label>
    </div>
  )
}
