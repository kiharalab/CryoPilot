// Settings controls: a settings row, the model field with suggestions, the
// reasoning-effort picker, and a hook that saves a change and updates setup.
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { useApp } from '../../store/app'
import { Input, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Segmented } from '../../components/ui'
import { EFFORTS } from '../new/AgentChips'
import { CLAUDE_MODELS, loadAccountModels, suggestedModels } from '../setup/services'

/** Saves a settings change and puts the new setup in the store. Throws after toasting on failure. */
export function useSaveSettings() {
  const setSetup = useApp((state) => state.setSetup)
  const toast = useApp((state) => state.toast)
  return useCallback(
    async (body, { errorTitle = 'Could not save the setting' } = {}) => {
      try {
        const next = await api.saveSettings(body)
        setSetup(next)
        return next
      } catch (err) {
        toast({ title: errorTitle, description: err.message, tone: 'error' })
        throw err
      }
    },
    [setSetup, toast],
  )
}

/** A heading for a group of rows inside a settings section. */
export function SettingsGroup({ title, description, children, className }) {
  return (
    <section className={cx('mt-8 first:mt-0', className)}>
      {title ? <h3 className="text-sm font-semibold text-fg">{title}</h3> : null}
      {description ? <p className="mt-0.5 text-sm text-muted">{description}</p> : null}
      <div className={title || description ? 'mt-3' : ''}>{children}</div>
    </section>
  )
}

/** Label and description on the left, the control on the right (or below with `stacked`). */
export function SettingsRow({ title, description, htmlFor, error, stacked = false, children }) {
  return (
    <div
      className={cx(
        'border-b border-line py-3.5 last:border-b-0',
        stacked ? 'flex flex-col gap-3' : 'flex flex-wrap items-start justify-between gap-x-6 gap-y-3',
      )}
    >
      <div className="min-w-[200px] flex-1">
        <label htmlFor={htmlFor} className="block text-sm font-medium text-fg">
          {title}
        </label>
        {description ? <p className="mt-0.5 text-sm text-muted">{description}</p> : null}
        {error ? <p className="mt-1 text-xs text-danger">{error}</p> : null}
      </div>
      <div className={cx(stacked ? 'min-w-0' : 'shrink-0 pt-px')}>{children}</div>
    </div>
  )
}

/**
 * Free-text model name with a menu of suggestions. Commits on Enter, on
 * blur, and when a suggestion is picked; an empty field restores the value.
 * `onCommit(model)` may return false (or a promise of false) when saving failed.
 * `claude` lists Claude models too (the reconstruction agent runs on them).
 */
export function ModelInput({ id, value, onCommit, invalid, className, claude = false }) {
  const [text, setText] = useState(value || '')
  const [accountModels, setAccountModels] = useState(null)
  const openaiConfigured = useApp((state) => Boolean(state.setup?.openai?.configured))
  const lastSent = useRef(value || '')

  useEffect(() => {
    setText(value || '')
    lastSent.current = value || ''
  }, [value])

  const commit = (next = text) => {
    const model = next.trim()
    if (!model) {
      setText(value || '')
      return
    }
    if (model === lastSent.current) return
    lastSent.current = model
    Promise.resolve(onCommit(model)).then((saved) => {
      // A rejected name can be sent again after the user looks at the error.
      if (saved === false) lastSent.current = value || ''
    })
  }

  const models = suggestedModels(accountModels)
  const pick = (model) => {
    setText(model)
    commit(model)
  }

  return (
    <div className={cx('relative', className)}>
      <Input
        id={id}
        value={text}
        mono
        invalid={invalid}
        spellCheck={false}
        autoComplete="off"
        placeholder="gpt-5.5"
        onChange={(event) => setText(event.target.value)}
        onBlur={() => commit()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            commit()
          }
          if (event.key === 'Escape') setText(value || '')
        }}
        className="pr-9"
      />
      <Menu
        onOpenChange={(open) => {
          if (open && accountModels === null && openaiConfigured) loadAccountModels().then(setAccountModels)
        }}
      >
        <MenuTrigger asChild>
          <button
            type="button"
            aria-label="Suggested models"
            className="absolute right-1 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg data-[state=open]:bg-hover"
          >
            <ChevronDown size={14} strokeWidth={1.75} />
          </button>
        </MenuTrigger>
        <MenuContent align="end" className="w-[240px]">
          <MenuLabel>OpenAI</MenuLabel>
          {models.map((model) => (
            <MenuItem key={model} checked={model === value} onSelect={() => pick(model)}>
              <span className="font-mono text-[12.5px]">{model}</span>
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuLabel>Anthropic</MenuLabel>
          {claude ? (
            CLAUDE_MODELS.map((model) => (
              <MenuItem key={model} checked={model === value} onSelect={() => pick(model)}>
                <span className="font-mono text-[12.5px]">{model}</span>
              </MenuItem>
            ))
          ) : (
            <MenuItem disabled hint="Coming soon">
              Claude models
            </MenuItem>
          )}
        </MenuContent>
      </Menu>
    </div>
  )
}

/** Reasoning effort as a segmented control; `options` defaults to the efforts both agents accept. */
export function EffortPicker({ value, onChange, options = EFFORTS, ariaLabel = 'Reasoning effort' }) {
  return <Segmented value={value} onChange={onChange} options={options} size="sm" ariaLabel={ariaLabel} />
}
