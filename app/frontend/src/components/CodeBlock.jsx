// Highlighted, copyable code with optional collapsing for long blocks.
import { useMemo, useState } from 'react'
import { Check, ChevronDown, Copy } from 'lucide-react'
import { highlight } from '../lib/highlight'
import { cx } from '../lib/cx'

export function useCopy(timeout = 1400) {
  const [copied, setCopied] = useState(false)
  const copy = async (text) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), timeout)
    } catch {
      setCopied(false)
    }
  }
  return [copied, copy]
}

/**
 * code: string; language: highlight.js name ('python', 'bash', 'json', ...) or null.
 * maxLines: collapse after this many lines (0 = never). label: small caption at the top left.
 */
export function CodeBlock({ code, language, maxLines = 0, label, wrap = false, className, tone = 'inset' }) {
  const [expanded, setExpanded] = useState(false)
  const [copied, copy] = useCopy()
  const text = code || ''
  const lines = useMemo(() => text.split('\n'), [text])
  const collapsible = maxLines > 0 && lines.length > maxLines + 3
  const shown = collapsible && !expanded ? lines.slice(0, maxLines).join('\n') : text
  const html = useMemo(() => highlight(shown, language), [shown, language])

  return (
    <div className={cx('group/code relative min-w-0 overflow-hidden rounded-lg border border-line', tone === 'inset' ? 'bg-inset' : 'bg-canvas', className)}>
      {label ? (
        <div className="flex h-7 items-center border-b border-line px-3 text-xs text-muted">
          <span className="truncate">{label}</span>
        </div>
      ) : null}
      <button
        type="button"
        aria-label="Copy"
        onClick={() => copy(text)}
        className={cx(
          'absolute right-1.5 flex h-6 w-6 items-center justify-center rounded-md bg-elevated text-muted opacity-0 ring-1 ring-line transition-opacity hover:text-fg group-hover/code:opacity-100 focus-visible:opacity-100',
          label ? 'top-[34px]' : 'top-1.5',
        )}
      >
        {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
      </button>
      <pre
        className={cx(
          'm-0 overflow-x-auto px-3 py-2.5 font-mono text-[12.5px] leading-[1.6]',
          wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre',
        )}
      >
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
      {collapsible ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="flex h-7 w-full items-center justify-center gap-1 border-t border-line text-xs text-muted hover:bg-hover hover:text-fg"
        >
          <ChevronDown size={13} className={cx('transition-transform', expanded && 'rotate-180')} />
          {expanded ? 'Show less' : `Show all ${lines.length} lines`}
        </button>
      ) : null}
    </div>
  )
}
