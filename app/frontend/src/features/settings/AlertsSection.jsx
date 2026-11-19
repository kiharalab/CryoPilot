// Settings > Alerts: where alerts go and which ones to send.
// Settings > Spending: the spending limit new sessions start with.
import { useEffect, useState } from 'react'
import { api } from '../../lib/api'
import { formatRelative } from '../../lib/format'
import { useApp } from '../../store/app'
import { Button, Input, Segmented, Switch } from '../../components/ui'
import { SettingsGroup, SettingsRow, useSaveSettings } from './controls'

const EVENTS = [
  { id: 'finished', title: 'A run finishes', description: 'The agent ended its turn with a final message. Turns under 2 minutes do not send this alert.' },
  { id: 'question', title: 'The agent asks a question', description: 'The final message ends with a question or asks you to reply.' },
  { id: 'failed', title: 'A run fails', description: 'The agent reported an error, or its process stopped unexpectedly.' },
  { id: 'paused', title: 'A run pauses', description: 'The session reached its spending limit.' },
]
const SECURITY = [
  { value: 'starttls', label: 'STARTTLS' },
  { value: 'ssl', label: 'SSL' },
  { value: 'none', label: 'None' },
]

function useAlertsSaver() {
  const save = useSaveSettings()
  return (alerts) => save({ alerts: { ...alerts, app_url: window.location.origin } }, { errorTitle: 'Could not save the alert settings' })
}

function WebhookCard() {
  const webhook = useApp((state) => state.setup?.alerts?.webhook)
  const saveAlerts = useAlertsSaver()
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (changes) => {
    setBusy(true)
    try {
      await saveAlerts(changes)
      setUrl('')
    } catch {
      // toasted by useSaveSettings
    } finally {
      setBusy(false)
    }
  }
  return (
    <SettingsRow
      stacked
      title="Webhook"
      description={
        webhook?.configured
          ? `Sending to ${webhook.host}${webhook.source === 'environment' ? ', from the environment variable CRYOPILOT_ALERT_WEBHOOK_URL' : ''}.`
          : 'Paste the incoming webhook address from Slack, Discord, Mattermost, or Google Chat.'
      }
      htmlFor="alert-webhook"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id="alert-webhook"
          type="password"
          autoComplete="off"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder={webhook?.configured ? 'Enter a new address to replace it' : 'https://hooks.slack.com/services/…'}
          disabled={busy}
          className="min-w-0 flex-1"
        />
        <Button size="sm" variant="secondary" disabled={!url.trim()} loading={busy} onClick={() => submit({ webhook_url: url.trim() })}>
          Save
        </Button>
        {webhook?.configured ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => submit({ clear_webhook: true })}>
            Remove
          </Button>
        ) : null}
      </div>
    </SettingsRow>
  )
}

function EmailCard() {
  const email = useApp((state) => state.setup?.alerts?.email)
  const saveAlerts = useAlertsSaver()
  const [values, setValues] = useState(null)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (email) setValues({ to: email.to, from: email.from, host: email.host, port: String(email.port), security: email.security, username: email.username })
  }, [email])
  if (!values) return null

  const set = (key) => (event) => setValues((current) => ({ ...current, [key]: event.target.value }))
  const submit = async (changes) => {
    setBusy(true)
    try {
      await saveAlerts({ email: changes })
      setPassword('')
    } catch {
      // toasted by useSaveSettings
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsRow
      stacked
      title="Email"
      description={email.configured ? `Sending to ${email.to} through ${email.host}.` : 'Sends through your lab or university mail server. Ask its administrator for these values.'}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-xs text-muted sm:col-span-2">
          Send to (separate several with commas)
          <Input value={values.to} onChange={set('to')} placeholder="you@university.edu" disabled={busy} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Mail server
          <Input value={values.host} onChange={set('host')} placeholder="smtp.university.edu" disabled={busy} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Port
          <Input value={values.port} onChange={set('port')} inputMode="numeric" disabled={busy} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          User name (optional)
          <Input value={values.username} onChange={set('username')} autoComplete="off" disabled={busy} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Password (optional)
          <Input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={email.password_set ? 'Saved' : ''}
            disabled={busy}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          From (optional)
          <Input value={values.from} onChange={set('from')} placeholder={values.username.includes('@') ? values.username : 'cryopilot@this-computer'} disabled={busy} />
        </label>
        <div className="flex flex-col gap-1 text-xs text-muted">
          Connection
          <Segmented
            size="sm"
            ariaLabel="Mail server connection"
            value={values.security}
            options={SECURITY}
            onChange={(security) => setValues((current) => ({ ...current, security }))}
          />
        </div>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <Button size="sm" variant="secondary" loading={busy} onClick={() => submit({ ...values, ...(password ? { password } : {}) })}>
          Save
        </Button>
        {email.configured ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => submit({ clear: true })}>
            Remove
          </Button>
        ) : null}
      </div>
    </SettingsRow>
  )
}

function TestRow() {
  const alerts = useApp((state) => state.setup?.alerts)
  const refreshSetup = useApp((state) => state.refreshSetup)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState(null)
  const [error, setError] = useState('')
  const ready = alerts?.webhook?.configured || alerts?.email?.configured
  const test = async () => {
    setBusy(true)
    setError('')
    setResults(null)
    try {
      const response = await api.testAlerts()
      setResults(response.results)
      await refreshSetup()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }
  const last = alerts?.last
  return (
    <SettingsRow
      title="Send a test alert"
      description={
        last
          ? last.ok
            ? `The last alert went out by ${last.channel} ${formatRelative(last.at)}.`
            : `The last ${last.channel} alert failed ${formatRelative(last.at)}: ${last.error}`
          : 'Checks that an alert reaches you.'
      }
    >
      <div className="flex flex-col items-end gap-1">
        <Button size="sm" variant="secondary" disabled={!ready} loading={busy} onClick={test}>
          Send a test
        </Button>
        {error ? <p className="text-xs text-danger">{error}</p> : null}
        {results?.map((result) => (
          <p key={result.channel} className={`max-w-[320px] text-right text-xs ${result.ok ? 'text-success' : 'text-danger'}`}>
            {result.channel === 'webhook' ? 'Webhook' : 'Email'}: {result.ok ? 'sent' : result.error}
          </p>
        ))}
      </div>
    </SettingsRow>
  )
}

export function AlertsSection() {
  const alerts = useApp((state) => state.setup?.alerts)
  const remember = useApp((state) => Boolean(state.setup?.remember_credentials))
  const openSettings = useApp((state) => state.openSettings)
  const saveAlerts = useAlertsSaver()
  if (!alerts) return null
  const keptInMemory = !remember && ((alerts.webhook.configured && alerts.webhook.source === 'app') || alerts.email.password_set)
  const toggle = (changes) => saveAlerts(changes).catch(() => {})
  return (
    <>
      <p className="mb-6 text-sm text-muted">
        Hear about a run when you are away from CryoPilot. An alert says what happened, the session and project names, the time, and the cost, and links to this server.
        Nothing is sent until you add a destination.
      </p>
      <SettingsGroup title="Where to send alerts">
        <WebhookCard />
        <EmailCard />
        <TestRow />
      </SettingsGroup>
      {keptInMemory ? (
        <p className="mt-3 text-sm text-warning">
          The webhook address and mail password are forgotten when CryoPilot restarts, and alerts stop. To keep them, turn on{' '}
          <button type="button" className="underline" onClick={() => openSettings('accounts')}>
            Remember passwords on this computer
          </button>
          .
        </p>
      ) : null}
      <SettingsGroup title="When to send them">
        {EVENTS.map((event) => (
          <SettingsRow key={event.id} title={event.title} description={event.description} htmlFor={`alert-${event.id}`}>
            <Switch
              id={`alert-${event.id}`}
              label={event.title}
              checked={alerts.events[event.id]}
              onCheckedChange={(checked) => toggle({ events: { [event.id]: checked } })}
            />
          </SettingsRow>
        ))}
        <SettingsRow
          title="Include the agent's last message"
          description="Adds up to 500 characters of the agent's message or the error. That text may describe unpublished work, and it leaves this computer."
          htmlFor="alert-message"
        >
          <Switch id="alert-message" label="Include the agent's last message" checked={alerts.include_message} onCheckedChange={(checked) => toggle({ include_message: checked })} />
        </SettingsRow>
      </SettingsGroup>
    </>
  )
}

export function SpendingSection() {
  const saved = useApp((state) => state.setup?.defaults?.spending_limit_usd)
  const pricesAsOf = useApp((state) => state.setup?.prices_as_of)
  const save = useSaveSettings()
  const [value, setValue] = useState(saved ?? '')
  const [error, setError] = useState('')
  useEffect(() => setValue(saved ?? ''), [saved])
  const commit = async () => {
    const next = String(value).trim()
    if (next === String(saved ?? '')) return
    setError('')
    try {
      await save({ defaults: { spending_limit_usd: next === '' ? null : next } })
    } catch (err) {
      setError(err.message)
    }
  }
  return (
    <>
      <SettingsGroup>
        <SettingsRow
          title="Spending limit per session"
          description="New sessions start with this limit. A session pauses before its next model request once it reaches it, and you can raise it there. Leave it empty for no limit."
          htmlFor="settings-spending-limit"
          error={error}
        >
          <div className="flex items-center gap-1.5">
            <span className="text-sm text-muted">$</span>
            <Input
              id="settings-spending-limit"
              value={value}
              inputMode="decimal"
              placeholder="No limit"
              onChange={(event) => setValue(event.target.value)}
              onBlur={commit}
              onKeyDown={(event) => {
                if (event.key === 'Enter') commit()
              }}
              className="w-28"
            />
          </div>
        </SettingsRow>
      </SettingsGroup>
      <p className="mt-4 text-sm text-muted">
        Costs are estimates from each provider&apos;s list prices as of {pricesAsOf}. Your provider&apos;s invoice is the exact figure. A model with no listed price in this version shows tokens only,
        and the limit cannot stop it. A CryoSPARC job or tool that is already running finishes before the run pauses, and it costs nothing in tokens while it runs.
      </p>
    </>
  )
}
