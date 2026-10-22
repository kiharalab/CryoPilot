// The message box at the bottom of every conversation and on the new-session
// screens. Enter sends, Shift+Enter adds a line. While the agent works the
// send button becomes a stop button.
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { ArrowUp, Square, X } from 'lucide-react'
import { cx } from '../lib/cx'
import { Tip } from './ui'

/**
 * Props:
 *   value, onChange(text)          controlled text
 *   onSubmit()                     Enter or the send button; not called when canSubmit is false
 *   canSubmit                      defaults to value.trim() !== ''
 *   running, onStop()              show the stop button instead of send
 *   busy                           sending in progress (spinner state, input still editable)
 *   attachments=[{key, label, icon?, title?}], onRemoveAttachment(key)
 *   toolbarLeft, toolbarRight      nodes in the bottom row (attach menu, model picker, ...)
 *   placeholder, autoFocus, minRows=1, maxRows=12, disabled, size: 'md' | 'lg'
 *   footer                          small text under the box
 */
export const Composer = forwardRef(function Composer(
  {
    value,
    onChange,
    onSubmit,
    canSubmit,
    running = false,
    onStop,
    busy = false,
    attachments = [],
    onRemoveAttachment,
    toolbarLeft,
    toolbarRight,
    placeholder = 'Message CryoPilot',
    autoFocus = false,
    minRows = 1,
    maxRows = 12,
    disabled = false,
    size = 'md',
    footer,
    className,
  },
  ref,
) {
  const textareaRef = useRef(null)
  useImperativeHandle(ref, () => ({ focus: () => textareaRef.current?.focus() }), [])
  const submittable = (canSubmit ?? Boolean(value?.trim())) && !busy && !disabled && !running

  useEffect(() => {
    const element = textareaRef.current
    if (!element) return
    const style = window.getComputedStyle(element)
    const lineHeight = parseFloat(style.lineHeight) || 22
    const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    element.style.height = 'auto'
    const height = Math.min(Math.max(element.scrollHeight, lineHeight * minRows + padding), lineHeight * maxRows + padding)
    element.style.height = `${height}px`
    element.style.overflowY = element.scrollHeight > height ? 'auto' : 'hidden'
  }, [value, minRows, maxRows])

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus()
  }, [autoFocus])

  const submit = () => {
    if (submittable) onSubmit?.()
  }

  return (
    <div className={cx('w-full', className)}>
      <div
        className={cx(
          'rounded-2xl border bg-elevated shadow-composer transition-colors',
          disabled ? 'border-line opacity-70' : 'border-line-strong/70 focus-within:border-line-strong',
        )}
      >
        {attachments.length ? (
          <div className="flex flex-wrap gap-1.5 px-3 pt-3">
            {attachments.map((attachment) => {
              const Icon = attachment.icon
              return (
                <span
                  key={attachment.key}
                  title={attachment.title || attachment.label}
                  className="inline-flex h-7 max-w-[280px] items-center gap-1.5 rounded-lg border border-line bg-subtle pl-2 pr-1 text-xs text-fg"
                >
                  {Icon ? <Icon size={13} strokeWidth={1.75} className="shrink-0 text-muted" /> : null}
                  <span className="truncate">{attachment.label}</span>
                  {onRemoveAttachment ? (
                    <button
                      type="button"
                      aria-label={`Remove ${attachment.label}`}
                      onClick={() => onRemoveAttachment(attachment.key)}
                      className="flex h-5 w-5 items-center justify-center rounded text-faint hover:bg-hover hover:text-fg"
                    >
                      <X size={12} />
                    </button>
                  ) : null}
                </span>
              )
            })}
          </div>
        ) : null}
        <textarea
          ref={textareaRef}
          value={value}
          rows={minRows}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(event) => onChange?.(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              submit()
            }
          }}
          className={cx(
            'block w-full resize-none bg-transparent px-4 text-fg placeholder:text-faint focus:outline-none',
            size === 'lg' ? 'pt-4 text-md leading-6' : 'pt-3 text-[14.5px] leading-[22px]',
          )}
        />
        <div className="flex items-center gap-1 px-2 pb-2 pt-1">
          <div className="flex min-w-0 flex-1 items-center gap-1">{toolbarLeft}</div>
          {toolbarRight ? <div className="flex items-center gap-1">{toolbarRight}</div> : null}
          {running ? (
            <Tip label="Stop the agent">
              <button
                type="button"
                aria-label="Stop the agent"
                onClick={onStop}
                className="ml-1 flex h-8 w-8 items-center justify-center rounded-full bg-fg text-canvas transition-opacity hover:opacity-85"
              >
                <Square size={11} fill="currentColor" strokeWidth={0} />
              </button>
            </Tip>
          ) : (
            <button
              type="button"
              aria-label="Send"
              onClick={submit}
              disabled={!submittable}
              className={cx(
                'ml-1 flex h-8 w-8 items-center justify-center rounded-full transition-colors',
                submittable ? 'bg-accent text-accent-fg hover:bg-accent-hover' : 'bg-subtle text-faint',
              )}
            >
              <ArrowUp size={16} strokeWidth={2.25} className={busy ? 'animate-pulse' : ''} />
            </button>
          )}
        </div>
      </div>
      {footer ? <div className="mt-2 px-1 text-center text-xs text-faint">{footer}</div> : null}
    </div>
  )
})
