// The accounts the agents use (OpenAI, Anthropic, CryoSPARC, and CryoZeta, the
// Kihara Lab EM server): their form fields, validation, and save-then-test.
// Onboarding, SignIn, and Settings > Accounts all build on these.
import { useState } from 'react'
import { CircleCheck, CircleX, Eye, EyeOff, TriangleAlert } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { pluralize } from '../../lib/format'
import { Field, IconButton, Input } from '../../components/ui'

export const SERVICE_NAMES = { openai: 'OpenAI', anthropic: 'Anthropic', cryosparc: 'CryoSPARC', cryozeta: 'CryoZeta' }

/** True for the services whose only credential is an API key. */
export function isKeyService(service) {
  return service === 'openai' || service === 'anthropic'
}

/** The service whose API key runs a model: Claude models run on Anthropic. */
export function modelService(model) {
  return String(model || '').startsWith('claude-') ? 'anthropic' : 'openai'
}

/** Claude models the agents can run on, most capable first. */
export const CLAUDE_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5']

/** "CryoZeta, the Kihara Lab EM server": a service's name where a screen introduces it. */
export function ServiceTitle({ service }) {
  return (
    <>
      {SERVICE_NAMES[service]}
      {service === 'cryozeta' ? <span className="font-normal text-muted">, the Kihara Lab EM server</span> : null}
    </>
  )
}

/** "macOS" for Darwin; other platform.system() names read fine as they are. */
export function systemName(system) {
  return { Darwin: 'macOS' }[system] || system || ''
}

/** Services whose password or key must be entered again (mirrors needsSignIn in App.jsx). */
export function servicesNeedingSignIn(setup) {
  if (!setup?.onboarded) return []
  const services = []
  if (!setup.openai?.configured) services.push('openai')
  if (setup.platform?.reconstruction_supported && setup.cryosparc?.email && !setup.cryosparc?.password_set) services.push('cryosparc')
  if (setup.cryozeta?.username && !setup.cryozeta?.password_set) services.push('cryozeta')
  return services
}

export function initialValues(service, setup) {
  if (isKeyService(service)) return { api_key: '' }
  if (service === 'cryosparc') return { url: setup?.cryosparc?.url || '', email: setup?.cryosparc?.email || '', password: '' }
  return { base_url: setup?.cryozeta?.base_url || '', username: setup?.cryozeta?.username || '', password: '' }
}

/** True when the stored credentials are enough to run the connection test. */
export function isConfigured(service, setup) {
  if (isKeyService(service)) return Boolean(setup?.[service]?.configured)
  if (service === 'cryosparc') return Boolean(setup?.cryosparc?.email && setup?.cryosparc?.password_set)
  return Boolean(setup?.cryozeta?.username && setup?.cryozeta?.password_set)
}

/** True when the form holds something that is not saved yet. */
export function isDirty(service, values, setup) {
  if (isKeyService(service)) return Boolean(values.api_key.trim())
  if (service === 'cryosparc') {
    return values.url.trim() !== (setup?.cryosparc?.url || '') || values.email.trim() !== (setup?.cryosparc?.email || '') || Boolean(values.password)
  }
  return (
    values.base_url.trim() !== (setup?.cryozeta?.base_url || '') ||
    values.username.trim() !== (setup?.cryozeta?.username || '') ||
    Boolean(values.password)
  )
}

/** Per-field messages for what is missing; empty when the form can be saved and tested. */
export function validate(service, values, setup) {
  const errors = {}
  if (isKeyService(service)) {
    if (!values.api_key.trim() && !setup?.[service]?.configured) errors.api_key = `Enter your ${SERVICE_NAMES[service]} API key.`
    return errors
  }
  if (service === 'cryosparc') {
    if (!values.url.trim()) errors.url = 'Enter the address of your CryoSPARC instance.'
    if (!values.email.trim()) errors.email = 'Enter the email you sign in to CryoSPARC with.'
    if (!values.password && !setup?.cryosparc?.password_set) errors.password = 'Enter your CryoSPARC password.'
    return errors
  }
  if (!values.base_url.trim()) errors.base_url = 'Enter the CryoZeta server address.'
  if (!values.username.trim()) errors.username = 'Enter your CryoZeta username.'
  if (!values.password && !setup?.cryozeta?.password_set) errors.password = 'Enter your CryoZeta password.'
  return errors
}

function settingsBody(service, values) {
  if (isKeyService(service)) {
    const key = values.api_key.trim()
    return key ? { [service]: { api_key: key } } : null
  }
  if (service === 'cryosparc') {
    return { cryosparc: { url: values.url.trim(), email: values.email.trim(), ...(values.password ? { password: values.password } : {}) } }
  }
  return {
    cryozeta: { base_url: values.base_url.trim(), username: values.username.trim(), ...(values.password ? { password: values.password } : {}) },
  }
}

const TESTS = { openai: api.testOpenai, anthropic: api.testAnthropic, cryosparc: api.testCryosparc, cryozeta: api.testCryozeta }

/** Turns a test response into the line shown under the form. */
function describeTest(service, response, setup) {
  if (isKeyService(service)) {
    if (service === 'openai') rememberModels(response.models)
    const text = `Connected. ${pluralize(response.model_count || 0, 'model')} available to this key.`
    const model = setup?.defaults?.reconstruction_model || 'gpt-5.5'
    if (response.default_model_available === false && modelService(model) === service) {
      return { tone: 'warning', text: `${text} ${model} is not one of them, so choose another model in Settings.` }
    }
    return { tone: 'success', text }
  }
  if (service === 'cryosparc') {
    return { tone: 'success', text: `Signed in. ${pluralize(response.project_count || 0, 'project')} visible to this account.` }
  }
  return { tone: 'success', text: `Signed in as ${response.username}.` }
}

/** Runs the connection test on what is stored now. Never throws. */
export async function testService(service, setup) {
  try {
    const response = await TESTS[service]()
    const fresh = await api.setup().catch(() => null)
    return { ok: true, setup: fresh, result: describeTest(service, response, fresh || setup) }
  } catch (err) {
    return { ok: false, setup: null, result: { tone: 'error', text: err.message } }
  }
}

/**
 * Saves the form (an empty password keeps the stored one), then tests the
 * connection. Never throws: returns {ok, setup, result} where `setup` is the
 * newest setup payload when anything was saved (null otherwise) and `result`
 * is {tone, text} for ResultLine.
 */
export async function saveAndTest(service, values, setup) {
  let saved = null
  try {
    const body = settingsBody(service, values)
    if (body) saved = await api.saveSettings(body)
  } catch (err) {
    return { ok: false, setup: null, result: { tone: 'error', text: err.message } }
  }
  const tested = await testService(service, saved || setup)
  return { ...tested, setup: tested.setup || saved }
}

// ------------------------------------------------------------ OpenAI models
/** OpenAI models to offer: GPT-5.5, the default used in the paper, then the newest ones. */
export const OPENAI_MODELS = ['gpt-5.5', 'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra', 'gpt-5.6-sol', 'gpt-5.6-luna']

// Reasoning efforts the newest OpenAI models accept, from their model pages:
// none of them takes 'minimal', and GPT-6 Astra and GPT-6.1 Sol always reason.
// Other models, and Claude models (whose client maps the levels), take every level offered.
const ALWAYS_REASONS = ['low', 'medium', 'high', 'xhigh', 'max']
const MODEL_EFFORTS = {
  'gpt-6-astra': ALWAYS_REASONS,
  'gpt-6.1-sol': ALWAYS_REASONS,
  'gpt-6.1-terra': ['none', ...ALWAYS_REASONS],
  'gpt-5.6': ['none', ...ALWAYS_REASONS],
  'gpt-5.6-sol': ['none', ...ALWAYS_REASONS],
  'gpt-5.6-luna': ['none', ...ALWAYS_REASONS],
}

/** Whether `model` accepts reasoning `effort`. */
export function supportsEffort(model, effort) {
  const efforts = MODEL_EFFORTS[model]
  return !efforts || efforts.includes(effort)
}
let accountModelsPromise = null

function modelRank(id) {
  const match = /^gpt-(\d+)(?:\.(\d+))?(?:-(astra|pro|sol|terra|mini|luna|nano))?$/.exec(id)
  if (!match) return null
  const tier = { astra: 0, undefined: 1, pro: 2, sol: 3, terra: 4, mini: 5, luna: 6, nano: 7 }[match[3]]
  return { major: Number(match[1]), minor: Number(match[2] || 0), tier }
}

/** GPT-5 and later chat models from an OpenAI model list, newest first. */
function chatModels(models) {
  return (models || [])
    .map((id) => ({ id, rank: modelRank(id) }))
    .filter((entry) => entry.rank && entry.rank.major >= 5)
    .sort((a, b) => b.rank.major - a.rank.major || b.rank.minor - a.rank.minor || a.rank.tier - b.rank.tier)
    .map((entry) => entry.id)
}

function rememberModels(models) {
  const list = chatModels(models)
  if (list.length) accountModelsPromise = Promise.resolve(list)
}

/** Model names to suggest: the account's GPT models once known, else a short default list. */
export function suggestedModels(accountModels) {
  const merged = [...OPENAI_MODELS, ...(accountModels || [])]
  return [...new Set(merged)].slice(0, 10)
}

/** The account's GPT models, fetched once per page load. Resolves to [] when unavailable. */
export function loadAccountModels() {
  if (!accountModelsPromise) {
    accountModelsPromise = api
      .testOpenai()
      .then((response) => chatModels(response.models))
      .catch(() => {
        accountModelsPromise = null
        return []
      })
  }
  return accountModelsPromise
}

// ----------------------------------------------------------------- display
const RESULT_TONES = {
  success: { icon: CircleCheck, iconClass: 'text-success', textClass: 'text-fg' },
  warning: { icon: TriangleAlert, iconClass: 'text-warning', textClass: 'text-fg' },
  error: { icon: CircleX, iconClass: 'text-danger', textClass: 'text-danger' },
}

/** One line with the outcome of a save or a connection test. */
export function ResultLine({ result, className }) {
  if (!result) return null
  const tone = RESULT_TONES[result.tone] || RESULT_TONES.success
  const Icon = tone.icon
  return (
    <p role="status" className={cx('flex items-start gap-2 text-sm', tone.textClass, className)}>
      <Icon size={15} strokeWidth={1.75} className={cx('mt-0.5 shrink-0', tone.iconClass)} />
      <span className="min-w-0 break-words">{result.text}</span>
    </p>
  )
}

/** Password or API key input with a show/hide toggle. */
export function SecretInput({ id, value, onChange, invalid, placeholder, autoComplete = 'off', name, autoFocus, mono }) {
  const [visible, setVisible] = useState(false)
  return (
    <div className="relative">
      <Input
        id={id}
        name={name}
        type={visible ? 'text' : 'password'}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        invalid={invalid}
        placeholder={placeholder}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        spellCheck={false}
        mono={mono}
        className="pr-10"
      />
      <IconButton
        icon={visible ? EyeOff : Eye}
        label={visible ? 'Hide' : 'Show'}
        size="sm"
        iconSize={14}
        tooltip={false}
        onClick={() => setVisible((current) => !current)}
        className="absolute right-1 top-1/2 -translate-y-1/2"
      />
    </div>
  )
}

/** A URL shown as quiet text with a "Change" button that turns it into a field. */
function CollapsibleUrl({ label, value, error, onChange, placeholder }) {
  const [editing, setEditing] = useState(Boolean(error) || !value)
  if (editing || error) {
    return (
      <Field label={label} error={error}>
        {({ id }) => (
          <Input id={id} value={value} onChange={(event) => onChange(event.target.value)} invalid={Boolean(error)} placeholder={placeholder} mono spellCheck={false} />
        )}
      </Field>
    )
  }
  return (
    <p className="flex min-w-0 items-center gap-2 text-sm text-muted">
      <span className="min-w-0 truncate font-mono text-[12.5px]" title={value}>
        {value}
      </span>
      <button type="button" onClick={() => setEditing(true)} className="shrink-0 rounded text-sm font-medium text-accent-text hover:underline">
        Change
      </button>
    </p>
  )
}

/**
 * The fields for one service. `values` comes from initialValues(); `onChange`
 * receives the next values object. `compactUrl` shows the server address as
 * text with a Change button (used on the sign-in screen).
 */
export function ServiceFields({ service, values, onChange, errors = {}, setup, autoFocus = false, compactUrl = false }) {
  const set = (key) => (value) => onChange({ ...values, [key]: value })

  if (isKeyService(service)) {
    const saved = setup?.[service]?.configured
    return (
      <Field label="API key" error={errors.api_key} hint={saved ? 'Leave this blank to keep the key CryoPilot already has.' : undefined}>
        {({ id }) => (
          <SecretInput
            id={id}
            name={`${service}-api-key`}
            value={values.api_key}
            onChange={set('api_key')}
            invalid={Boolean(errors.api_key)}
            placeholder={saved ? 'Enter a new key to replace it' : service === 'anthropic' ? 'sk-ant-...' : 'sk-...'}
            autoFocus={autoFocus}
            mono
          />
        )}
      </Field>
    )
  }

  const cryosparc = service === 'cryosparc'
  const urlKey = cryosparc ? 'url' : 'base_url'
  const accountKey = cryosparc ? 'email' : 'username'
  const passwordSet = cryosparc ? setup?.cryosparc?.password_set : setup?.cryozeta?.password_set
  const urlLabel = cryosparc ? 'CryoSPARC URL' : 'Server URL'
  const urlPlaceholder = cryosparc ? 'http://localhost:61000' : 'https://em.kiharalab.org'

  return (
    <div className="flex flex-col gap-4">
      {compactUrl ? (
        <CollapsibleUrl label={urlLabel} value={values[urlKey]} error={errors[urlKey]} onChange={set(urlKey)} placeholder={urlPlaceholder} />
      ) : (
        <Field label={urlLabel} error={errors[urlKey]}>
          {({ id }) => (
            <Input
              id={id}
              value={values[urlKey]}
              onChange={(event) => set(urlKey)(event.target.value)}
              invalid={Boolean(errors[urlKey])}
              placeholder={urlPlaceholder}
              autoFocus={autoFocus}
              mono
              spellCheck={false}
            />
          )}
        </Field>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label={cryosparc ? 'Email' : 'Username'} error={errors[accountKey]}>
          {({ id }) => (
            <Input
              id={id}
              name={`${service}-${accountKey}`}
              type={cryosparc ? 'email' : 'text'}
              value={values[accountKey]}
              onChange={(event) => set(accountKey)(event.target.value)}
              invalid={Boolean(errors[accountKey])}
              placeholder={cryosparc ? 'you@university.edu' : 'Username'}
              autoComplete="username"
              autoFocus={autoFocus && compactUrl}
              spellCheck={false}
            />
          )}
        </Field>
        <Field label="Password" error={errors.password}>
          {({ id }) => (
            <SecretInput
              id={id}
              name={`${service}-password`}
              value={values.password}
              onChange={set('password')}
              invalid={Boolean(errors.password)}
              placeholder={passwordSet ? 'Saved; leave blank to keep' : 'Password'}
              autoComplete="current-password"
            />
          )}
        </Field>
      </div>
    </div>
  )
}
