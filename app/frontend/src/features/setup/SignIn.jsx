// Shown on later starts when passwords were not remembered: asks again for
// the key and passwords that the server no longer has, then tests each one.
import { useState } from 'react'
import { KeyRound, Microscope, Server } from 'lucide-react'
import { api } from '../../lib/api'
import { useApp } from '../../store/app'
import { Button, Switch } from '../../components/ui'
import { BrandMark } from '../../layout/Brand'
import { SetupPage } from './SetupPage'
import { ResultLine, ServiceFields, ServiceTitle, initialValues, saveAndTest, servicesNeedingSignIn, validate } from './services'

const SERVICE_ICONS = { openai: KeyRound, anthropic: KeyRound, cryosparc: Microscope, cryozeta: Server }

export function SignIn({ onDone, onSkip }) {
  const setup = useApp((state) => state.setup)
  const toast = useApp((state) => state.toast)
  // Fixed when the screen opens, so a service stays on screen after it signs in.
  const [services] = useState(() => servicesNeedingSignIn(setup))
  const [values, setValues] = useState(() => Object.fromEntries(services.map((service) => [service, initialValues(service, setup)])))
  const [errors, setErrors] = useState({})
  const [results, setResults] = useState({})
  const [signedIn, setSignedIn] = useState({})
  const [busy, setBusy] = useState(false)
  const [remember, setRemember] = useState(Boolean(setup?.remember_credentials))
  const [savingRemember, setSavingRemember] = useState(false)

  const pending = services.filter((service) => !signedIn[service])
  const anyFailed = pending.some((service) => results[service]?.tone === 'error')

  const submit = async (event) => {
    event.preventDefault()
    const problems = Object.fromEntries(pending.map((service) => [service, validate(service, values[service], setup)]))
    setErrors(problems)
    if (pending.some((service) => Object.keys(problems[service]).length)) return
    setBusy(true)
    let allSignedIn = true
    let latest = setup
    for (const service of pending) {
      const outcome = await saveAndTest(service, values[service], latest)
      if (outcome.setup) latest = outcome.setup
      setResults((current) => ({ ...current, [service]: outcome.result }))
      if (outcome.ok) {
        setSignedIn((current) => ({ ...current, [service]: true }))
        setValues((current) => ({ ...current, [service]: initialValues(service, latest) }))
      } else {
        allSignedIn = false
      }
    }
    setBusy(false)
    // The store is updated only at the end: App leaves this screen as soon as
    // it sees the passwords, which would hide a failed test.
    if (allSignedIn) await onDone()
  }

  const skip = () => {
    onSkip()
    // Anything saved before skipping still counts; pick it up.
    useApp
      .getState()
      .refreshSetup()
      .catch(() => {})
  }

  const changeRemember = async (checked) => {
    setRemember(checked)
    setSavingRemember(true)
    try {
      await api.saveSettings({ remember_credentials: checked })
    } catch (err) {
      setRemember(!checked)
      toast({ title: 'Could not change the setting', description: err.message, tone: 'error' })
    } finally {
      setSavingRemember(false)
    }
  }

  if (!services.length) {
    return (
      <SetupPage width="max-w-[460px]" centered>
        <BrandMark size={32} />
        <p className="mt-6 text-base font-medium">You are signed in to everything CryoPilot needs.</p>
        <div className="mt-6">
          <Button variant="primary" onClick={onDone}>
            Continue
          </Button>
        </div>
      </SetupPage>
    )
  }

  return (
    <SetupPage width="max-w-[460px]" centered>
      <BrandMark size={32} />
      <h1 className="mt-6 text-xl font-semibold tracking-[-0.01em]">Sign in again</h1>
      <p className="mt-1.5 text-sm text-muted">
        Passwords are not remembered on this computer, so CryoPilot needs {services.length === 1 ? 'this one' : 'these'} again after a restart.
      </p>

      <form onSubmit={submit} className="mt-7 flex flex-col gap-4" noValidate>
        {services.map((service) => {
          const Icon = SERVICE_ICONS[service]
          return (
            <section key={service} className="rounded-xl border border-line px-4 py-4">
              <div className="mb-3.5 flex items-center gap-2.5">
                <Icon size={16} strokeWidth={1.75} className="shrink-0 text-muted" />
                <h2 className="min-w-0 text-sm font-medium text-fg">
                  <ServiceTitle service={service} />
                </h2>
              </div>
              {signedIn[service] ? null : (
                <ServiceFields
                  service={service}
                  values={values[service]}
                  onChange={(next) => {
                    setValues((current) => ({ ...current, [service]: next }))
                    setErrors((current) => ({ ...current, [service]: {} }))
                  }}
                  errors={errors[service]}
                  setup={setup}
                  autoFocus={service === services[0]}
                  compactUrl
                />
              )}
              {results[service] ? <ResultLine result={results[service]} className={signedIn[service] ? '' : 'mt-3.5'} /> : null}
            </section>
          )
        })}

        <label htmlFor="sign-in-remember" className="flex cursor-pointer items-center gap-4 px-1 py-1">
          <span className="min-w-0 flex-1 text-sm text-fg">Remember passwords on this computer</span>
          <Switch id="sign-in-remember" checked={remember} disabled={savingRemember} onCheckedChange={changeRemember} />
        </label>

        <div className="mt-2 flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={skip} disabled={busy}>
            Skip for now
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            {anyFailed ? 'Try again' : 'Sign in'}
          </Button>
        </div>
      </form>
    </SetupPage>
  )
}
