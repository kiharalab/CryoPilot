// A path field that completes against the server's file system, like a
// shell: type a prefix and pick a folder to keep going, or a file to finish.
// Globs (/data/movies/*.tif) show how many files they match.
import { forwardRef, useEffect, useId, useRef, useState } from 'react'
import { File, Folder } from 'lucide-react'
import { api } from '../../lib/api'
import { cx } from '../../lib/cx'
import { formatBytes, pluralize } from '../../lib/format'
import { Input, Popover, PopoverAnchor, PopoverContent } from '../../components/ui'

const DEBOUNCE_MS = 160
const RENDER_CAP = 200
const GLOB = /[*?[]/

/**
 * Props: value, onChange(text), dirsOnly, invalid, placeholder, id, autoFocus,
 * className, and any other <input> props.
 */
export const PathInput = forwardRef(function PathInput(
  { value, onChange, dirsOnly = false, invalid, placeholder, id, autoFocus, className, onKeyDown, onFocus, onBlur, ...props },
  ref,
) {
  const [focused, setFocused] = useState(false)
  const [open, setOpen] = useState(false)
  const [listing, setListing] = useState(null) // {entries, count, glob, error}
  const [active, setActive] = useState(-1)
  const anchorRef = useRef(null)
  const listRef = useRef(null)
  const requestRef = useRef(0)
  const listId = useId()
  const text = value || ''

  useEffect(() => {
    if (!focused) return undefined
    const query = text.trim()
    if (!query) {
      setListing(null)
      setOpen(false)
      return undefined
    }
    const request = ++requestRef.current
    const timer = setTimeout(async () => {
      let next
      try {
        const data = await api.fsList(query, { dirsOnly })
        const entries = data.entries || data.matches || []
        const glob = GLOB.test(query)
        const count = data.count ?? data.match_count ?? data.total ?? (glob ? entries.length : null)
        next = { entries, count, glob, error: '' }
      } catch (err) {
        next = { entries: [], count: null, glob: GLOB.test(query), error: err.message }
      }
      if (request !== requestRef.current) return
      setListing(next)
      setActive(-1)
      // Nothing to add when the only suggestion is what is already typed.
      const exact = next.entries.length === 1 && stripSlash(next.entries[0].path) === stripSlash(query)
      setOpen(Boolean(next.entries.length && !exact) || Boolean(next.error) || next.glob)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      // A reply that arrives after blur or another keystroke is stale.
      requestRef.current += 1
    }
  }, [text, dirsOnly, focused])

  useEffect(() => {
    if (active < 0) return
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const entries = listing?.entries || []
  const shown = entries.slice(0, RENDER_CAP)

  const accept = (entry) => {
    if (!entry) return
    if (entry.type === 'dir') {
      onChange?.(`${stripSlash(entry.path)}/`)
    } else {
      onChange?.(entry.path)
      setOpen(false)
    }
    setActive(-1)
  }

  const handleKeyDown = (event) => {
    if (open && shown.length) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setActive((current) => (current + 1) % shown.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setActive((current) => (current <= 0 ? shown.length - 1 : current - 1))
        return
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && active >= 0 && !event.nativeEvent.isComposing) {
        event.preventDefault()
        accept(shown[active])
        return
      }
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
      return
    }
    onKeyDown?.(event)
  }

  let status = ''
  if (listing?.error) status = listing.error
  else if (listing?.glob) status = listing.count ? `${pluralize(listing.count, 'file')} match` : 'No files match'
  else if (entries.length > shown.length) status = `Showing ${shown.length} of ${entries.length}; keep typing to narrow the list`

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <div ref={anchorRef} className={cx('min-w-0', className)}>
          <Input
            ref={ref}
            id={id}
            mono
            value={text}
            invalid={invalid}
            placeholder={placeholder}
            autoFocus={autoFocus}
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
            onChange={(event) => onChange?.(event.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={(event) => {
              setFocused(true)
              onFocus?.(event)
            }}
            onBlur={(event) => {
              setFocused(false)
              setOpen(false)
              onBlur?.(event)
            }}
            {...props}
          />
        </div>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        sideOffset={4}
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        onInteractOutside={(event) => {
          if (anchorRef.current?.contains(event.target)) event.preventDefault()
        }}
        // Keep focus in the input, including when the list's scrollbar is dragged.
        onMouseDown={(event) => event.preventDefault()}
        style={{ width: 'var(--radix-popover-trigger-width)' }}
        className="overflow-hidden"
      >
        {status ? (
          <p className={cx('px-3 py-2 text-xs', listing?.error ? 'text-warning' : 'text-muted', shown.length ? 'border-b border-line' : '')}>{status}</p>
        ) : null}
        {shown.length ? (
          <div ref={listRef} id={listId} role="listbox" aria-label="Suggestions" className="max-h-64 overflow-y-auto p-1">
            {shown.map((entry, index) => {
              const Icon = entry.type === 'dir' ? Folder : File
              return (
                <div
                  key={entry.path}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={index === active}
                  data-index={index}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseMove={() => index !== active && setActive(index)}
                  onClick={() => accept(entry)}
                  className={cx('flex h-7 cursor-default items-center gap-2 rounded-md px-2', index === active ? 'bg-hover' : '')}
                >
                  <Icon size={14} strokeWidth={1.75} className={cx('shrink-0', entry.type === 'dir' ? 'text-accent-text' : 'text-faint')} />
                  <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-fg">
                    {entry.name}
                    {entry.type === 'dir' ? '/' : ''}
                  </span>
                  {entry.type !== 'dir' && entry.size !== undefined && entry.size !== null ? (
                    <span className="shrink-0 text-xs tabular-nums text-faint">{formatBytes(entry.size)}</span>
                  ) : null}
                </div>
              )
            })}
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  )
})

function stripSlash(path) {
  return String(path || '').replace(/\/+$/, '') || '/'
}
