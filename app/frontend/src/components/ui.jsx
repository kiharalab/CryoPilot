// Shared UI primitives. Feature code builds on these instead of styling raw
// elements, so spacing, focus rings, and colours stay consistent.
import { createContext, forwardRef, useContext, useEffect, useId, useRef, useState } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as MenuPrimitive from '@radix-ui/react-dropdown-menu'
import * as PopoverPrimitive from '@radix-ui/react-popover'
import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { Check, LoaderCircle, X } from 'lucide-react'
import { cx } from '../lib/cx'
import { useApp } from '../store/app'

// ---------------------------------------------------------------- buttons
const BUTTON_VARIANTS = {
  primary: 'bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-45',
  secondary: 'border border-line bg-elevated text-fg hover:bg-hover disabled:text-faint',
  ghost: 'text-muted hover:bg-hover hover:text-fg disabled:text-faint disabled:hover:bg-transparent',
  danger: 'bg-danger text-danger-fg hover:opacity-90 disabled:opacity-45',
  'danger-ghost': 'text-danger hover:bg-danger-soft disabled:opacity-45',
}
const BUTTON_SIZES = {
  xs: 'h-6 gap-1 rounded-md px-2 text-xs',
  sm: 'h-7 gap-1.5 rounded-md px-2.5 text-sm',
  md: 'h-8 gap-2 rounded-lg px-3 text-sm',
  lg: 'h-10 gap-2 rounded-lg px-4 text-base',
}
const ICON_SIZES = { xs: 12, sm: 14, md: 15, lg: 16 }

export const Button = forwardRef(function Button(
  { variant = 'secondary', size = 'md', icon: Icon, iconRight: IconRight, loading = false, className, children, disabled, type = 'button', ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      className={cx(
        'inline-flex shrink-0 select-none items-center justify-center font-medium whitespace-nowrap transition-colors duration-100 disabled:cursor-not-allowed',
        BUTTON_VARIANTS[variant],
        BUTTON_SIZES[size],
        className,
      )}
      {...props}
    >
      {loading ? <LoaderCircle size={ICON_SIZES[size]} className="animate-spin" /> : Icon ? <Icon size={ICON_SIZES[size]} strokeWidth={1.75} /> : null}
      {children}
      {IconRight ? <IconRight size={ICON_SIZES[size]} strokeWidth={1.75} /> : null}
    </button>
  )
})

/** Square icon-only button. `label` is required: it is the tooltip and the accessible name. */
export const IconButton = forwardRef(function IconButton(
  { icon: Icon, label, size = 'md', active = false, tooltip = true, side = 'bottom', className, iconSize, ...props },
  ref,
) {
  const dimension = { xs: 'h-6 w-6 rounded-md', sm: 'h-7 w-7 rounded-md', md: 'h-8 w-8 rounded-lg', lg: 'h-9 w-9 rounded-lg' }[size]
  const button = (
    <button
      ref={ref}
      type="button"
      aria-label={label}
      className={cx(
        'inline-flex shrink-0 items-center justify-center transition-colors duration-100 disabled:cursor-not-allowed disabled:opacity-40',
        active ? 'bg-hover text-fg' : 'text-muted hover:bg-hover hover:text-fg',
        dimension,
        className,
      )}
      {...props}
    >
      <Icon size={iconSize || ICON_SIZES[size] + 1} strokeWidth={1.75} />
    </button>
  )
  return tooltip ? (
    <Tip label={label} side={side}>
      {button}
    </Tip>
  ) : (
    button
  )
})

// ----------------------------------------------------------------- inputs
const FIELD_BASE =
  'w-full rounded-lg border bg-canvas px-3 text-sm text-fg placeholder:text-faint transition-colors duration-100 focus:outline-none focus:border-accent focus:ring-3 focus:ring-accent/15 disabled:bg-subtle disabled:text-muted'

/** The enclosing Field's control id and the id of its hint or error, for aria-describedby. */
const FieldContext = createContext(null)

/** aria-describedby for a control: the Field's hint or error when the control is that Field's own. */
function useDescribedBy(id) {
  const field = useContext(FieldContext)
  return field && id === field.id ? field.describedBy : undefined
}

export const Input = forwardRef(function Input({ className, invalid, mono, ...props }, ref) {
  const describedBy = useDescribedBy(props.id)
  return (
    <input
      ref={ref}
      className={cx(FIELD_BASE, 'h-9', invalid ? 'border-danger' : 'border-line', mono && 'font-mono text-[12.5px]', className)}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      {...props}
    />
  )
})

export const Textarea = forwardRef(function Textarea({ className, invalid, ...props }, ref) {
  const describedBy = useDescribedBy(props.id)
  return (
    <textarea
      ref={ref}
      className={cx(FIELD_BASE, 'resize-none py-2 leading-relaxed', invalid ? 'border-danger' : 'border-line', className)}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      {...props}
    />
  )
})

export const Select = forwardRef(function Select({ className, invalid, children, ...props }, ref) {
  const describedBy = useDescribedBy(props.id)
  return (
    <select
      ref={ref}
      className={cx(FIELD_BASE, 'h-9 appearance-none bg-no-repeat pr-8', invalid ? 'border-danger' : 'border-line', className)}
      aria-describedby={describedBy}
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238a929c' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
        backgroundPosition: 'right 10px center',
      }}
      {...props}
    >
      {children}
    </select>
  )
})

/**
 * Label + control + hint/error. Pass the control as children; a render function
 * receives {id, describedBy}. Input, Textarea, and Select with that id point
 * aria-describedby at the hint or error by themselves.
 */
export function Field({ label, hint, error, optional, className, children, htmlFor }) {
  const generated = useId()
  const id = htmlFor || generated
  const describedBy = error || hint ? `${id}-message` : undefined
  return (
    <FieldContext.Provider value={{ id, describedBy }}>
      <div className={cx('flex min-w-0 flex-col gap-1.5', className)}>
        {label ? (
          <label htmlFor={id} className="flex items-baseline gap-1.5 text-sm font-medium text-fg">
            {label}
            {optional ? <span className="text-xs font-normal text-faint">Optional</span> : null}
          </label>
        ) : null}
        {typeof children === 'function' ? children({ id, describedBy }) : children}
        {error ? (
          <p id={describedBy} className="text-xs text-danger">
            {error}
          </p>
        ) : hint ? (
          <p id={describedBy} className="text-xs text-muted">
            {hint}
          </p>
        ) : null}
      </div>
    </FieldContext.Provider>
  )
}

export function Switch({ checked, onCheckedChange, disabled, label, id }) {
  return (
    <SwitchPrimitive.Root
      id={id}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className="relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full bg-line-strong transition-colors data-[state=checked]:bg-accent disabled:opacity-50"
    >
      <SwitchPrimitive.Thumb className="block h-3.5 w-3.5 translate-x-0.5 rounded-full bg-thumb shadow-sm transition-transform data-[state=checked]:translate-x-[15px]" />
    </SwitchPrimitive.Root>
  )
}

/** Small set of mutually exclusive options, e.g. theme or dataset kind. */
export function Segmented({ value, onChange, options, size = 'md', className, ariaLabel }) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className={cx('inline-flex rounded-lg bg-subtle p-0.5', className)}>
      {options.map((option) => {
        const selected = option.value === value
        const Icon = option.icon
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(option.value)}
            className={cx(
              'inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors',
              size === 'sm' ? 'h-6 px-2 text-xs' : 'h-7 px-3 text-sm',
              selected ? 'bg-elevated text-fg ring-1 ring-line' : 'text-muted hover:text-fg',
            )}
          >
            {Icon ? <Icon size={13} strokeWidth={1.75} /> : null}
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- tooltip
export function TooltipProvider({ children }) {
  return (
    <TooltipPrimitive.Provider delayDuration={350} skipDelayDuration={150}>
      {children}
    </TooltipPrimitive.Provider>
  )
}

export function Tip({ label, side = 'bottom', children, shortcut }) {
  if (!label) return children
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-[80] flex items-center gap-2 rounded-md bg-fg px-2 py-1 text-xs font-medium text-canvas shadow-popover"
        >
          {label}
          {shortcut ? <span className="text-faint">{shortcut}</span> : null}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}

// ----------------------------------------------------------------- dialog
export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close

/** Centered modal. `title` is required for accessibility; hide it visually with hideTitle. */
export function DialogContent({ title, description, hideTitle = false, className, children, width = 'max-w-lg', onClose, ...props }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-[60] bg-overlay" />
      <DialogPrimitive.Content
        className={cx(
          'fixed left-1/2 top-1/2 z-[60] flex max-h-[min(88vh,880px)] w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-line bg-elevated shadow-dialog focus:outline-none',
          width,
          className,
        )}
        {...props}
      >
        <div className={cx('flex items-start gap-3 px-5 pt-4', hideTitle && 'sr-only')}>
          <div className="min-w-0 flex-1">
            <DialogPrimitive.Title className="text-lg font-semibold tracking-[-0.01em]">{title}</DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="mt-0.5 text-sm text-muted">{description}</DialogPrimitive.Description>
            ) : (
              <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
            )}
          </div>
          <DialogPrimitive.Close asChild>
            <IconButton icon={X} label="Close" size="sm" tooltip={false} onClick={onClose} />
          </DialogPrimitive.Close>
        </div>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}

/** Confirmation dialog for destructive actions. */
export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel = 'Delete', onConfirm, busy }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title} description={description} width="max-w-md">
        <div className="flex justify-end gap-2 px-5 pb-4 pt-5">
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button variant="danger" loading={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------- menu
export const Menu = MenuPrimitive.Root
export const MenuTrigger = MenuPrimitive.Trigger

export function MenuContent({ children, align = 'start', side = 'bottom', className, sideOffset = 6, ...props }) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content
        align={align}
        side={side}
        sideOffset={sideOffset}
        collisionPadding={8}
        className={cx('z-[70] min-w-[200px] rounded-xl border border-line bg-elevated p-1 shadow-popover focus:outline-none', className)}
        {...props}
      >
        {children}
      </MenuPrimitive.Content>
    </MenuPrimitive.Portal>
  )
}

export function MenuItem({ icon: Icon, children, hint, danger, checked, className, ...props }) {
  return (
    <MenuPrimitive.Item
      className={cx(
        'flex h-8 cursor-default select-none items-center gap-2.5 rounded-lg px-2.5 text-sm outline-none data-[disabled]:opacity-40 data-[highlighted]:bg-hover',
        danger ? 'text-danger' : 'text-fg',
        className,
      )}
      {...props}
    >
      {Icon ? <Icon size={15} strokeWidth={1.75} className={danger ? '' : 'text-muted'} /> : null}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {hint ? <span className="text-xs text-faint">{hint}</span> : null}
      {checked ? <Check size={14} className="text-accent" /> : null}
    </MenuPrimitive.Item>
  )
}

export function MenuSeparator() {
  return <MenuPrimitive.Separator className="mx-1 my-1 h-px bg-line" />
}

export function MenuLabel({ children }) {
  return <MenuPrimitive.Label className="px-2.5 pb-1 pt-1.5 text-xs text-faint">{children}</MenuPrimitive.Label>
}

// ---------------------------------------------------------------- popover
export const Popover = PopoverPrimitive.Root
export const PopoverTrigger = PopoverPrimitive.Trigger
export const PopoverAnchor = PopoverPrimitive.Anchor

export function PopoverContent({ children, className, align = 'start', side = 'bottom', sideOffset = 6, ...props }) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        align={align}
        side={side}
        sideOffset={sideOffset}
        collisionPadding={8}
        className={cx('z-[70] rounded-xl border border-line bg-elevated shadow-popover focus:outline-none', className)}
        {...props}
      >
        {children}
      </PopoverPrimitive.Content>
    </PopoverPrimitive.Portal>
  )
}

// --------------------------------------------------------------- feedback
export function Spinner({ size = 14, className }) {
  return <LoaderCircle size={size} className={cx('animate-spin text-muted', className)} />
}

const DOT_TONES = {
  running: 'bg-accent animate-pulse-dot',
  starting: 'bg-accent animate-pulse-dot',
  connecting: 'bg-accent animate-pulse-dot',
  initializing: 'bg-accent animate-pulse-dot',
  idle: 'bg-success',
  completed: 'bg-success',
  error: 'bg-danger',
  failed: 'bg-danger',
  killed: 'bg-danger',
  interrupted: 'bg-warning',
  queued: 'bg-warning',
  launched: 'bg-accent animate-pulse-dot',
  waiting: 'bg-warning',
}

/** Small coloured dot for an agent or CryoSPARC job state; pulses while working. */
export function StatusDot({ state, className }) {
  return <span aria-hidden className={cx('inline-block h-1.5 w-1.5 shrink-0 rounded-full', DOT_TONES[state] || 'bg-line-strong', className)} />
}

export function Kbd({ children }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-line bg-subtle px-1 font-mono text-2xs text-muted">
      {children}
    </kbd>
  )
}

/** Shown where content will appear: says what is missing and offers the next step. */
export function EmptyState({ icon: Icon, title, children, action, className }) {
  return (
    <div className={cx('flex h-full min-h-40 flex-col items-center justify-center gap-2 px-6 py-10 text-center', className)}>
      {Icon ? <Icon size={22} strokeWidth={1.5} className="mb-1 text-faint" /> : null}
      {title ? <p className="text-sm font-medium text-fg">{title}</p> : null}
      {children ? <div className="max-w-sm text-sm text-muted">{children}</div> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}

export function InlineError({ children, className }) {
  if (!children) return null
  return <div className={cx('rounded-lg bg-danger-soft px-3 py-2 text-sm text-danger', className)}>{children}</div>
}

export function Skeleton({ className }) {
  return (
    <div
      className={cx('rounded-md bg-subtle', className)}
      style={{
        backgroundImage: 'linear-gradient(90deg, transparent 0%, var(--hover) 50%, transparent 100%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 2.2s linear infinite',
      }}
    />
  )
}

/** Tab strip used by the workbench and the bottom panel. */
export function TabStrip({ tabs, value, onChange, trailing, className }) {
  const scrollerRef = useRef(null)
  // Which edges hide tabs; those edges fade out.
  const [hidden, setHidden] = useState({ start: false, end: false })

  const measure = () => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const start = scroller.scrollLeft > 1
    const end = scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1
    setHidden((current) => (current.start === start && current.end === end ? current : { start, end }))
  }

  // Keep the selected tab in view and clear of the fades (24px, see .fade-start in
  // styles.css). The strip is scrolled directly: scrollIntoView would also
  // scroll the panels around it.
  useEffect(() => {
    const scroller = scrollerRef.current
    const tab = scroller?.querySelector('[aria-selected="true"]')
    if (tab && scroller.clientWidth) {
      const box = scroller.getBoundingClientRect()
      const tabBox = tab.getBoundingClientRect()
      if (tabBox.left < box.left + 24) scroller.scrollLeft -= box.left + 24 - tabBox.left
      else if (tabBox.right > box.right - 24) scroller.scrollLeft += tabBox.right - (box.right - 24)
    }
    measure()
  }, [value, tabs.length])

  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const observer = new ResizeObserver(measure)
    observer.observe(scroller)
    return () => observer.disconnect()
  }, [])

  return (
    <div className={cx('flex h-9 shrink-0 items-center gap-0.5 border-b border-line px-1.5', className)} role="tablist">
      <div
        ref={scrollerRef}
        onScroll={measure}
        onWheel={(event) => {
          // A plain mouse wheel scrolls the strip sideways.
          if (event.deltaY && !event.deltaX) event.currentTarget.scrollLeft += event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY
        }}
        className={cx(
          'flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]',
          hidden.start && 'fade-start',
          hidden.end && 'fade-end',
        )}
      >
        {tabs.map((tab) => {
          const selected = tab.value === value
          const Icon = tab.icon
          return (
            <div
              key={tab.value}
              role="tab"
              aria-selected={selected}
              tabIndex={0}
              onClick={() => onChange(tab.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') onChange(tab.value)
              }}
              onAuxClick={(event) => {
                if (event.button === 1 && tab.onClose) tab.onClose()
              }}
              title={tab.title || undefined}
              className={cx(
                'group flex h-7 max-w-[220px] shrink-0 cursor-default items-center gap-1.5 rounded-md px-2.5 text-sm transition-colors',
                selected ? 'bg-hover text-fg' : 'text-muted hover:text-fg',
              )}
            >
              {Icon ? <Icon size={14} strokeWidth={1.75} className="shrink-0" /> : null}
              <span className="truncate">{tab.label}</span>
              {tab.badge ? <span className="text-xs text-faint">{tab.badge}</span> : null}
              {tab.onClose ? (
                <button
                  type="button"
                  aria-label={`Close ${tab.label}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    tab.onClose()
                  }}
                  className={cx(
                    '-mr-1 flex h-4 w-4 items-center justify-center rounded hover:bg-line',
                    selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
                  )}
                >
                  <X size={11} />
                </button>
              ) : null}
            </div>
          )
        })}
      </div>
      {trailing ? <div className="flex shrink-0 items-center gap-0.5">{trailing}</div> : null}
    </div>
  )
}

// ----------------------------------------------------------------- toasts
const TOAST_TONES = {
  neutral: 'border-line',
  success: 'border-success/40',
  error: 'border-danger/50',
}

export function Toaster() {
  const toasts = useApp((state) => state.toasts)
  const dismiss = useApp((state) => state.dismissToast)
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[90] flex w-[360px] max-w-[calc(100vw-32px)] flex-col gap-2" aria-live="polite">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={cx('pointer-events-auto flex items-start gap-3 rounded-xl border bg-elevated px-4 py-3 shadow-popover', TOAST_TONES[toast.tone])}
        >
          <div className="min-w-0 flex-1">
            <p className={cx('text-sm font-medium', toast.tone === 'error' ? 'text-danger' : 'text-fg')}>{toast.title}</p>
            {toast.description ? <p className="mt-0.5 text-sm text-muted">{toast.description}</p> : null}
          </div>
          <button type="button" aria-label="Dismiss" onClick={() => dismiss(toast.id)} className="text-faint hover:text-fg">
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  )
}
