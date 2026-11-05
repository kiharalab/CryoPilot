// Settings > Accounts: OpenAI, Anthropic, CryoSPARC, and CryoZeta, each with its
// status, a form to change it, a connection test, and sign out; then the
// "remember passwords" switch and signing this browser out of CryoPilot.
import { useEffect, useState } from 'react'
import { KeyRound, LogOut, Microscope, Server } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatRelative } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, ConfirmDialog, Switch } from '../../components/ui'
import {
  KEY_VARIABLES,
  ResultLine,
  ServiceFields,
  SERVICE_NAMES,
  ServiceTitle,
  initialValues,
  isConfigured,
  isKeyService,
  saveAndTest,
  systemName,
  testService,
  validate,
} from '../setup/services'
import { SettingsGroup, SettingsRow, useSaveSettings } from './controls'

const ACCOUNT_INFO = {
  openai: {
    icon: KeyRound,
    purpose: 'Both agents run on OpenAI models with this key.',
  },
  anthropic: {
    icon: KeyRound,
    purpose: 'Both agents run on Claude models with this key.',
  },
  cryosparc: {
    icon: Microscope,
    purpose: 'The reconstruction agent creates and runs jobs in CryoSPARC with this account. The Maps panel reads live job status from it.',
  },
  cryozeta: {
    icon: Server,
    purpose: 'The modeling agent submits jobs to CryoZeta when it builds a model from a sequence.',
  },
}


/** {tone: 'on'|'pending'|'off', label, detail} for the card header. */
function accountStatus(service, setup) {
  if (isKeyService(service)) {
    const key = setup?.[service] || {}
    if (!key.configured) return { tone: 'off', label: 'Not set', detail: ACCOUNT_INFO[service].purpose }
    return {
      tone: 'on',
      label: 'Connected',
      detail: key.source === 'environment' ? `Using ${KEY_VARIABLES[service]} from the environment CryoPilot started in.` : 'API key saved in CryoPilot.',
    }
  }
  const settings = (service === 'cryosparc' ? setup?.cryosparc : setup?.cryozeta) || {}
  const account = service === 'cryosparc' ? settings.email : settings.username
  const url = service === 'cryosparc' ? settings.url : settings.base_url
  const where = account ? `${account} on ${url}` : ACCOUNT_INFO[service].purpose
  if (!settings.password_set) return { tone: 'off', label: 'Signed out', detail: where }
  if (!settings.verified_at) return { tone: 'pending', label: 'Not tested', detail: where }
  return { tone: 'on', label: 'Signed in', detail: where, verifiedAt: settings.verified_at }
}

const BADGE_DOTS = { on: 'bg-success', pending: 'bg-warning', off: 'bg-line-strong' }

function StatusBadge({ status }) {
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted"
      title={status.verifiedAt ? `Connection tested ${formatRelative(status.verifiedAt)}` : undefined}
    >
      <span aria-hidden className={cx('h-1.5 w-1.5 rounded-full', BADGE_DOTS[status.tone])} />
      {status.label}
    </span>
  )
}

function AccountCard({ service }) {
  const setup = useApp((state) => state.setup)
  const setSetup = useApp((state) => state.setSetup)
  const toast = useApp((state) => state.toast)
  const configured = isConfigured(service, setup)
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState(() => initialValues(service, setup))
  const [errors, setErrors] = useState({})
  const [busy, setBusy] = useState(null) // 'save' | 'test' | 'remove'
  const [result, setResult] = useState(null)
  const [confirmRemove, setConfirmRemove] = useState(false)

  const info = ACCOUNT_INFO[service]
  const Icon = info.icon
  const status = accountStatus(service, setup)
  const showForm = editing || !configured

  const startEditing = () => {
    setValues(initialValues(service, setup))
    setErrors({})
    setResult(null)
    setEditing(true)
  }

  const cancel = () => {
    setValues(initialValues(service, setup))
    setErrors({})
    setEditing(false)
  }

  const save = async () => {
    const problems = validate(service, values, setup)
    setErrors(problems)
    if (Object.keys(problems).length) return
    setBusy('save')
    setResult(null)
    const outcome = await saveAndTest(service, values, setup)
    setBusy(null)
    if (outcome.setup) setSetup(outcome.setup)
    setResult(outcome.result)
    if (outcome.ok) {
      setEditing(false)
      setValues(initialValues(service, outcome.setup || setup))
    } else {
      // Keep the form open to fix it, even when the save made the account "configured".
      setEditing(true)
    }
  }

  const test = async () => {
    setBusy('test')
    setResult(null)
    const outcome = await testService(service, setup)
    setBusy(null)
    if (outcome.setup) setSetup(outcome.setup)
    setResult(outcome.result)
  }

  const remove = async () => {
    setBusy('remove')
    try {
      const body =
        isKeyService(service) ? { [service]: { clear: true } } : service === 'cryosparc' ? { cryosparc: { clear_password: true } } : { cryozeta: { clear_password: true } }
      const next = await api.saveSettings(body)
      setSetup(next)
      setValues(initialValues(service, next))
      setResult(null)
      setEditing(false)
      setConfirmRemove(false)
    } catch (err) {
      toast({ title: isKeyService(service) ? 'Could not remove the key' : 'Could not sign out', description: err.message, tone: 'error' })
    } finally {
      setBusy(null)
    }
  }

  const removeLabel = isKeyService(service) ? 'Remove key' : 'Sign out'

  return (
    <div className="rounded-xl border border-line">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-subtle text-muted">
          <Icon size={16} strokeWidth={1.75} />
        </span>
        <div className="min-w-[180px] flex-1">
          <div className="flex items-center gap-2.5">
            <p className="min-w-0 truncate text-sm font-medium text-fg">
              <ServiceTitle service={service} />
            </p>
            <StatusBadge status={status} />
          </div>
          <p className="mt-0.5 truncate text-sm text-muted" title={status.detail}>
            {status.detail}
          </p>
        </div>
        {!showForm ? (
          <div className="flex shrink-0 items-center gap-1">
            <Button size="sm" loading={busy === 'test'} disabled={Boolean(busy)} onClick={test}>
              Test connection
            </Button>
            <Button size="sm" variant="ghost" disabled={Boolean(busy)} onClick={startEditing}>
              {isKeyService(service) ? 'Replace key' : 'Edit'}
            </Button>
            <Button
              size="sm"
              variant="danger-ghost"
              loading={busy === 'remove'}
              disabled={Boolean(busy)}
              onClick={() => (isKeyService(service) ? setConfirmRemove(true) : remove())}
            >
              {removeLabel}
            </Button>
          </div>
        ) : null}
      </div>

      {showForm ? (
        <form
          className="border-t border-line px-4 py-4"
          onSubmit={(event) => {
            event.preventDefault()
            save()
          }}
        >
          {status.detail !== info.purpose ? <p className="mb-4 text-sm text-muted">{info.purpose}</p> : null}
          <ServiceFields
            service={service}
            values={values}
            onChange={(next) => {
              setValues(next)
              setErrors({})
            }}
            errors={errors}
            setup={setup}
          />
          <div className="mt-4 flex items-center justify-end gap-2">
            {configured ? (
              <Button variant="ghost" disabled={busy === 'save'} onClick={cancel}>
                Cancel
              </Button>
            ) : null}
            <Button type="submit" variant="primary" loading={busy === 'save'}>
              {configured ? 'Save' : isKeyService(service) ? 'Save key' : 'Sign in'}
            </Button>
          </div>
        </form>
      ) : null}

      {result ? (
        <div className="border-t border-line px-4 py-2.5">
          <ResultLine result={result} />
        </div>
      ) : null}

      {isKeyService(service) ? (
        <ConfirmDialog
          open={confirmRemove}
          onOpenChange={setConfirmRemove}
          title={`Remove the ${SERVICE_NAMES[service]} API key?`}
          description={
            (service === 'anthropic' ? 'Claude models cannot run until you add a key again.' : 'Agents cannot start until you add a key again.') +
            (setup?.[service]?.source === 'environment'
              ? ` CryoPilot reads ${KEY_VARIABLES[service]} from the environment again the next time it starts.`
              : '')
          }
          confirmLabel="Remove key"
          busy={busy === 'remove'}
          onConfirm={remove}
        />
      ) : null}
    </div>
  )
}

function RememberRow() {
  const remember = useApp((state) => Boolean(state.setup?.remember_credentials))
  const save = useSaveSettings()
  const [saving, setSaving] = useState(false)
  return (
    <SettingsRow
      title="Remember passwords on this computer"
      description="Keeps the API key and passwords in ~/.cryopilot/app/credentials.json, readable only by your user account. When this is off, you sign in again each time CryoPilot starts."
      htmlFor="settings-remember"
    >
      <Switch
        id="settings-remember"
        label="Remember passwords on this computer"
        checked={remember}
        disabled={saving}
        onCheckedChange={async (checked) => {
          setSaving(true)
          try {
            await save({ remember_credentials: checked })
          } catch {
            // toasted by useSaveSettings
          } finally {
            setSaving(false)
          }
        }}
      />
    </SettingsRow>
  )
}

function BrowserAccessRow() {
  const toast = useApp((state) => state.toast)
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .authStatus()
      .then((status) => {
        if (!cancelled) setEnabled(Boolean(status.enabled))
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  if (!enabled) return null
  return (
    <SettingsRow title="This browser" description="Signed in with the access link printed when CryoPilot started. After you sign out, the app asks for the link again.">
      <Button
        size="sm"
        icon={LogOut}
        loading={busy}
        onClick={async () => {
          setBusy(true)
          try {
            await api.logout()
            window.location.reload()
          } catch (err) {
            setBusy(false)
            toast({ title: 'Could not sign out', description: err.message, tone: 'error' })
          }
        }}
      >
        Sign out
      </Button>
    </SettingsRow>
  )
}

export function AccountsSection() {
  const reconstructionSupported = useApp((state) => Boolean(state.setup?.platform?.reconstruction_supported))
  const system = useApp((state) => state.setup?.platform?.system)
  return (
    <>
      <SettingsGroup>
        <div className="flex flex-col gap-3">
          <AccountCard service="openai" />
          <AccountCard service="anthropic" />
          <AccountCard service="cryosparc" />
          {!reconstructionSupported ? (
            <p className="-mt-1 px-1 text-xs text-muted">
              The reconstruction agent runs on the Linux computer where CryoSPARC is installed, not on this {systemName(system) ? `${systemName(system)} ` : ''}computer. Signing in
              here still lets the Maps panel read job results.
            </p>
          ) : null}
          <AccountCard service="cryozeta" />
        </div>
      </SettingsGroup>
      <SettingsGroup title="Security">
        <RememberRow />
        <BrowserAccessRow />
      </SettingsGroup>
    </>
  )
}
