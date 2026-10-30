// Composer toolbar chips on the new-session screens: the model and its
// reasoning effort, and (for reconstruction) the processing guide. Each chip
// opens a small popover of choices; arrow keys move between them.
import { forwardRef, useState } from 'react'
import { BookOpen, Check, ChevronDown } from 'lucide-react'
import { cx } from '../../lib/cx'
import { pluralize } from '../../lib/format'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui'
import { CLAUDE_MODELS } from '../setup/services'

export const EFFORTS = [
  { value: 'minimal', label: 'Minimal' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra high' },
]

// The reconstruction agent also runs without reasoning (harness.py
// --reasoning-effort none); the modeling agent does not.
export const RECONSTRUCTION_EFFORTS = [{ value: 'none', label: 'None' }, ...EFFORTS]

const MODEL_SUGGESTIONS = ['gpt-5.5']

export function effortLabel(effort) {
  return RECONSTRUCTION_EFFORTS.find((option) => option.value === effort)?.label || effort
}

/** Arrow keys move focus between the choices in a chip's popover. */
function moveFocus(event) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  if (event.target instanceof HTMLInputElement) return
  const choices = [...event.currentTarget.querySelectorAll('[data-choice]:not([disabled])')]
  if (!choices.length) return
  event.preventDefault()
  const current = choices.indexOf(document.activeElement)
  const next = event.key === 'ArrowDown' ? (current + 1) % choices.length : (current - 1 + choices.length) % choices.length
  choices[next].focus()
}

export const ToolbarChip = forwardRef(function ToolbarChip({ icon: Icon, children, className, ...props }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      className={cx(
        'inline-flex h-8 min-w-0 items-center gap-1.5 rounded-lg px-2.5 text-sm text-muted transition-colors hover:bg-hover hover:text-fg data-[state=open]:bg-hover data-[state=open]:text-fg disabled:opacity-50',
        className,
      )}
      {...props}
    >
      {Icon ? <Icon size={14} strokeWidth={1.75} className="shrink-0" /> : null}
      {children}
      <ChevronDown size={13} strokeWidth={1.75} className="shrink-0 text-faint" />
    </button>
  )
})

function ChoiceHeader({ children }) {
  return <p className="px-2.5 pb-1 pt-2 text-xs font-medium text-faint">{children}</p>
}

function Choice({ selected, onSelect, disabled, children, className }) {
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={selected}
      data-choice
      disabled={disabled}
      onClick={onSelect}
      className={cx(
        'flex w-full items-center gap-2.5 rounded-lg px-2.5 text-left text-sm text-fg outline-none hover:bg-hover focus-visible:bg-hover focus-visible:outline-none disabled:opacity-40 disabled:hover:bg-transparent',
        className || 'h-8',
      )}
    >
      <span className="min-w-0 flex-1">{children}</span>
      {selected ? <Check size={14} className="shrink-0 self-center text-accent" /> : <span className="w-3.5 shrink-0" />}
    </button>
  )
}

/**
 * Model and reasoning effort for one session.
 * Props: model, effort, onModelChange(model), onEffortChange(effort), defaultModel,
 * efforts (the choices; EFFORTS unless given), claude (also offer Claude models).
 */
export function ModelChip({ model, effort, onModelChange, onEffortChange, defaultModel, efforts = EFFORTS, claude = false, disabled }) {
  const [open, setOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const suggestions = [...new Set([defaultModel, ...MODEL_SUGGESTIONS, ...(claude ? CLAUDE_MODELS : []), model].filter(Boolean))]
  const customLabel = claude ? 'Another OpenAI or Claude model' : 'Another OpenAI model'

  const applyCustom = () => {
    const next = custom.trim()
    if (!next) return
    onModelChange(next)
    setCustom('')
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setCustom('')
      }}
    >
      <PopoverTrigger asChild>
        <ToolbarChip disabled={disabled} aria-label={`Model ${model}, ${effortLabel(effort)} reasoning effort`}>
          <span className="truncate font-medium text-fg">{model}</span>
          <span className="shrink-0">{effortLabel(effort)}</span>
        </ToolbarChip>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={8} className="w-60 p-1" onKeyDown={moveFocus}>
        <div role="menu" aria-label="Model">
          <ChoiceHeader>Model</ChoiceHeader>
          {suggestions.map((name) => (
            <Choice key={name} selected={name === model} onSelect={() => onModelChange(name)}>
              <span className="block truncate font-mono text-[12.5px]">{name}</span>
            </Choice>
          ))}
          <div className="px-1 pb-1 pt-0.5">
            <input
              value={custom}
              onChange={(event) => setCustom(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  applyCustom()
                }
              }}
              placeholder={customLabel}
              aria-label={customLabel}
              spellCheck={false}
              autoCapitalize="off"
              className="h-8 w-full rounded-md border border-line bg-canvas px-2 font-mono text-[12.5px] text-fg placeholder:font-sans placeholder:text-faint focus:border-accent focus:outline-none"
            />
          </div>
        </div>
        <div className="mx-1 my-1 h-px bg-line" />
        <div role="menu" aria-label="Reasoning effort">
          <ChoiceHeader>Reasoning effort</ChoiceHeader>
          {efforts.map((option) => (
            <Choice key={option.value} selected={option.value === effort} onSelect={() => onEffortChange(option.value)}>
              {option.label}
            </Choice>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}

export const GUIDE_LABELS = { original: 'General guide', experimental: 'Self-evolution summary (experimental)' }

/**
 * Processing guide for one reconstruction session.
 * Props: guides (from api.guides()), value, onChange(id), defaultGuide.
 */
export function GuideChip({ guides, value, onChange, defaultGuide, disabled }) {
  const [open, setOpen] = useState(false)
  const current = guides.find((guide) => guide.id === value)
  const label = current?.label || GUIDE_LABELS[value] || value
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <ToolbarChip icon={BookOpen} disabled={disabled || !guides.length} aria-label={`Processing guide: ${label}`}>
          <span className="truncate">{label}</span>
        </ToolbarChip>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={8} className="w-[340px] max-w-[calc(100vw-24px)] p-1" onKeyDown={moveFocus}>
        <div role="menu" aria-label="Processing guide">
          <ChoiceHeader>Processing guide for this session</ChoiceHeader>
          {guides.map((guide) => (
            <Choice
              key={guide.id}
              selected={guide.id === value}
              disabled={guide.available === false}
              onSelect={() => {
                onChange(guide.id)
                setOpen(false)
              }}
              className="py-2"
            >
              <span className="flex items-baseline gap-2">
                <span className="min-w-0 truncate font-medium">{guide.label}</span>
                {guide.id === defaultGuide ? <span className="shrink-0 text-xs text-faint">Default</span> : null}
              </span>
              {guide.available === false ? (
                <span className="mt-0.5 block text-xs text-muted">The guide file is missing from this installation.</span>
              ) : guide.description ? (
                <span className="mt-0.5 line-clamp-2 block text-xs leading-relaxed text-muted">{guide.description}</span>
              ) : null}
              {guide.sections?.length ? <span className="mt-0.5 block text-xs text-faint">{pluralize(guide.sections.length, 'section')}</span> : null}
            </Choice>
          ))}
          <p className="px-2.5 pb-1.5 pt-1 text-xs text-faint">Change the default in Settings.</p>
        </div>
      </PopoverContent>
    </Popover>
  )
}
