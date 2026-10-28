// First-run setup: the license, OpenAI, CryoSPARC, CryoZeta, the processing
// guide, then a last look at the modeling toolchain. Each step saves as it
// goes, so leaving halfway keeps what was entered; finishing marks setup
// complete.
import { useState } from 'react'
import { Check, CircleCheck, Info } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { useApp } from '../../store/app'
import { Button, InlineError, Switch } from '../../components/ui'
import { BrandMark } from '../../layout/Brand'
import { GuidePicker } from '../settings/GuidePicker'
import { ToolchainStatus } from '../settings/ToolchainStatus'
import { LicenseStep } from './License'
import { SetupPage, StepFooter, StepLayout } from './SetupPage'
import { ResultLine, ServiceFields, initialValues, isConfigured, isDirty, saveAndTest, systemName, validate } from './services'

const STEPS = [
  { id: 'license', label: 'License' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'cryosparc', label: 'CryoSPARC' },
  { id: 'cryozeta', label: 'CryoZeta' },
  { id: 'guide', label: 'Guide' },
  { id: 'ready', label: 'Ready' },
]

export function Onboarding({ onDone }) {
  const reconstructionSupported = useApp((state) => Boolean(state.setup?.platform?.reconstruction_supported))
  const system = useApp((state) => state.setup?.platform?.system)
  // Coming back to setup after accepting the license starts at the next step.
  const licenseAccepted = useApp((state) => Boolean(state.setup?.license?.accepted))
  const [index, setIndex] = useState(licenseAccepted ? 1 : 0)
  const [reached, setReached] = useState(licenseAccepted ? 1 : 0)
  const [outcomes, setOutcomes] = useState(licenseAccepted ? { license: 'done' } : {}) // step id -> 'done' | 'skipped'
  const [results, setResults] = useState({}) // service -> {tone, text}

  const step = STEPS[index]
  const finishStep = (outcome) => {
    setOutcomes((current) => ({ ...current, [step.id]: outcome }))
    const next = Math.min(index + 1, STEPS.length - 1)
    setIndex(next)
    setReached((current) => Math.max(current, next))
  }
  const back = index > 0 ? () => setIndex(index - 1) : undefined
  // Each step is its own element below, so moving between steps remounts the form.
  const credentialProps = (service) => ({
    service,
    result: results[service],
    onResult: (result) => setResults((current) => ({ ...current, [service]: result })),
    onBack: back,
    onDone: () => finishStep('done'),
    onSkip: () => finishStep('skipped'),
  })

  return (
    // Wide enough for six step labels and the license text.
    <SetupPage width="max-w-[600px]">
      <div className="flex items-center gap-2.5">
        <BrandMark size={28} />
        <span className="text-base font-semibold tracking-[-0.01em]">Set up CryoPilot</span>
      </div>

      <Stepper current={index} reached={reached} outcomes={outcomes} onSelect={setIndex} />

      {step.id === 'license' ? <LicenseStep onDone={() => finishStep('done')} /> : null}

      {step.id === 'openai' ? (
        <CredentialStep
          {...credentialProps('openai')}
          title="Connect OpenAI"
          description="Both agents run on OpenAI models. They use GPT-5.5 by default, and any OpenAI model works. Support for Anthropic models is coming soon."
          footnote={
            <>
              Create a key at{' '}
              <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer noopener" className="text-accent-text hover:underline">
                platform.openai.com/api-keys
              </a>
              .
            </>
          }
        />
      ) : null}

      {step.id === 'cryosparc' ? (
        <CredentialStep
          {...credentialProps('cryosparc')}
          title="Sign in to CryoSPARC"
          description="The reconstruction agent creates and runs jobs in your CryoSPARC instance with this account, and the Maps panel reads job results from it."
          canSkip={!reconstructionSupported}
          notice={
            !reconstructionSupported
              ? `The reconstruction agent runs on the Linux computer where CryoSPARC is installed, not on this ${
                  systemName(system) ? `${systemName(system)} ` : ''
                }computer. Skip this step, or sign in if this computer can reach CryoSPARC, for example through an SSH tunnel.`
              : null
          }
        />
      ) : null}

      {step.id === 'cryozeta' ? (
        <CredentialStep
          {...credentialProps('cryozeta')}
          title="Sign in to CryoZeta"
          description="The modeling agent submits jobs to CryoZeta, the Kihara Lab EM server, when it builds a model from a sequence. Skip this if you start from existing models."
          canSkip
          footnote={
            <>
              Need an account? Visit{' '}
              <a href="https://em.kiharalab.org" target="_blank" rel="noreferrer noopener" className="text-accent-text hover:underline">
                em.kiharalab.org
              </a>
              .
            </>
          }
        />
      ) : null}

      {step.id === 'guide' ? <GuideStep onBack={back} onDone={() => finishStep('done')} /> : null}
      {step.id === 'ready' ? <ReadyStep onBack={back} onDone={onDone} /> : null}
    </SetupPage>
  )
}

// ------------------------------------------------------------------ stepper
const CIRCLE_STATES = {
  current: 'border border-accent bg-accent-soft text-accent-text',
  done: 'bg-accent text-accent-fg',
  skipped: 'border border-dashed border-line-strong text-faint',
  upcoming: 'border border-line text-faint',
}

function Stepper({ current, reached, outcomes, onSelect }) {
  return (
    <ol aria-label="Setup steps" className="mt-8 flex items-center gap-1.5">
      {STEPS.map((step, index) => {
        const state = index === current ? 'current' : outcomes[step.id] === 'done' ? 'done' : outcomes[step.id] === 'skipped' ? 'skipped' : 'upcoming'
        const reachable = index <= reached && index !== current
        const last = index === STEPS.length - 1
        return (
          <li key={step.id} className={cx('flex min-w-0 items-center gap-1.5', !last && 'flex-auto')}>
            <button
              type="button"
              disabled={!reachable}
              onClick={() => onSelect(index)}
              aria-current={index === current ? 'step' : undefined}
              aria-label={`${step.label}, step ${index + 1} of ${STEPS.length}${state === 'done' ? ', done' : state === 'skipped' ? ', skipped' : ''}`}
              className="group flex shrink-0 items-center gap-1.5 rounded-md disabled:cursor-default"
            >
              <span
                aria-hidden
                className={cx('flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-2xs font-medium transition-colors', CIRCLE_STATES[state])}
              >
                {state === 'done' ? <Check size={11} strokeWidth={2.75} /> : index + 1}
              </span>
              <span
                aria-hidden
                className={cx(
                  'text-xs',
                  index === current ? 'font-medium text-fg' : 'hidden text-muted group-enabled:group-hover:text-fg sm:inline',
                )}
              >
                {step.label}
              </span>
            </button>
            {!last ? <span aria-hidden className={cx('h-px min-w-3 flex-1', index < reached ? 'bg-accent/50' : 'bg-line')} /> : null}
          </li>
        )
      })}
    </ol>
  )
}

// -------------------------------------------------------------- credentials
/**
 * OpenAI, CryoSPARC, or CryoZeta. Continue saves what was entered and
 * tests the connection; a failed test can be retried or, once something is
 * saved, passed with "Continue anyway".
 */
function CredentialStep({ service, title, description, notice, footnote, canSkip = false, result, onResult, onBack, onDone, onSkip }) {
  const setup = useApp((state) => state.setup)
  const setSetup = useApp((state) => state.setSetup)
  const [values, setValues] = useState(() => initialValues(service, setup))
  const [errors, setErrors] = useState({})
  const [busy, setBusy] = useState(false)
  const [failedValues, setFailedValues] = useState(null)
  const [replacingKey, setReplacingKey] = useState(false)

  const configured = isConfigured(service, setup)
  const dirty = isDirty(service, values, setup)
  const passed = !dirty && configured && (result?.tone === 'success' || result?.tone === 'warning')
  const continueAnyway = failedValues === values && configured
  const showKeyCard = service === 'openai' && setup?.openai?.configured && !replacingKey

  const proceed = async () => {
    if (passed || continueAnyway) {
      onDone()
      return
    }
    const problems = validate(service, values, setup)
    setErrors(problems)
    if (Object.keys(problems).length) return
    setBusy(true)
    const outcome = await saveAndTest(service, values, setup)
    setBusy(false)
    if (outcome.setup) setSetup(outcome.setup)
    onResult(outcome.result)
    if (outcome.ok) {
      setValues(initialValues(service, outcome.setup || setup))
      onDone()
    } else {
      setFailedValues(values)
    }
  }

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        proceed()
      }}
      className="contents"
      noValidate
    >
      <StepLayout
        title={title}
        description={description}
        footer={
          <StepFooter
            onBack={onBack}
            onSkip={canSkip ? onSkip : undefined}
            primaryType="submit"
            primaryLabel={continueAnyway ? 'Continue anyway' : 'Continue'}
            busy={busy}
          />
        }
      >
        {notice ? (
          <div className="mb-5 flex items-start gap-2.5 rounded-xl bg-subtle px-3.5 py-3 text-sm text-muted">
            <Info size={15} strokeWidth={1.75} className="mt-0.5 shrink-0 text-faint" />
            <p>{notice}</p>
          </div>
        ) : null}

        {showKeyCard ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-line px-4 py-3">
            <CircleCheck size={16} strokeWidth={1.75} className="shrink-0 text-success" />
            <div className="min-w-[200px] flex-1">
              <p className="text-sm font-medium text-fg">
                {setup.openai.source === 'environment' ? 'Found OPENAI_API_KEY in the environment' : 'An API key is saved'}
              </p>
              <p className="text-sm text-muted">Continue checks that it works.</p>
            </div>
            <Button size="sm" variant="ghost" onClick={() => setReplacingKey(true)}>
              Use a different key
            </Button>
          </div>
        ) : (
          <ServiceFields
            service={service}
            values={values}
            onChange={(next) => {
              setValues(next)
              setErrors({})
            }}
            errors={errors}
            setup={setup}
            autoFocus
          />
        )}

        {result ? <ResultLine result={result} className="mt-4" /> : null}
        {footnote ? <p className="mt-4 text-sm text-muted">{footnote}</p> : null}
      </StepLayout>
    </form>
  )
}

// -------------------------------------------------------------------- guide
function GuideStep({ onBack, onDone }) {
  const guide = useApp((state) => state.setup?.processing_guide)
  const setSetup = useApp((state) => state.setSetup)
  const toast = useApp((state) => state.toast)
  const [saving, setSaving] = useState(false)

  const choose = async (id) => {
    setSaving(true)
    try {
      setSetup(await api.saveSettings({ processing_guide: id }))
    } catch (err) {
      toast({ title: 'Could not change the processing guide', description: err.message, tone: 'error' })
    } finally {
      setSaving(false)
    }
  }

  return (
    <StepLayout
      title="Choose a processing guide"
      description="The reconstruction agent follows this guide as it plans and checks each processing stage. You can switch guides in Settings, or for one session when you start it."
      footer={<StepFooter onBack={onBack} onPrimary={onDone} busy={saving} />}
    >
      <GuidePicker value={guide} onChange={choose} disabled={saving} />
    </StepLayout>
  )
}

// -------------------------------------------------------------------- ready
function ReadyStep({ onBack, onDone }) {
  const remember = useApp((state) => Boolean(state.setup?.remember_credentials))
  const setSetup = useApp((state) => state.setSetup)
  const toast = useApp((state) => state.toast)
  const [savingRemember, setSavingRemember] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const [error, setError] = useState('')

  const changeRemember = async (checked) => {
    setSavingRemember(true)
    try {
      setSetup(await api.saveSettings({ remember_credentials: checked }))
    } catch (err) {
      toast({ title: 'Could not change the setting', description: err.message, tone: 'error' })
    } finally {
      setSavingRemember(false)
    }
  }

  const finish = async () => {
    setFinishing(true)
    setError('')
    try {
      setSetup(await api.completeSetup())
      await onDone()
    } catch (err) {
      setError(err.message)
      setFinishing(false)
    }
  }

  return (
    <StepLayout
      title="Ready to start"
      description="The modeling agent runs these programs on this computer. Without an optional program, only the steps that need it are unavailable."
      footer={<StepFooter onBack={onBack} onPrimary={finish} busy={finishing} primaryLabel="Open CryoPilot" />}
    >
      <ToolchainStatus />
      <label htmlFor="onboarding-remember" className="mt-5 flex cursor-pointer items-start gap-4 rounded-xl border border-line px-4 py-3.5">
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-fg">Remember passwords on this computer</span>
          <span className="mt-0.5 block text-sm text-muted">
            Keeps them in ~/.cryopilot/app/credentials.json, readable only by your user account. When this is off, you sign in again each time
            CryoPilot starts.
          </span>
        </span>
        <Switch id="onboarding-remember" checked={remember} disabled={savingRemember} onCheckedChange={changeRemember} />
      </label>
      {error ? <InlineError className="mt-4">{error}</InlineError> : null}
    </StepLayout>
  )
}
